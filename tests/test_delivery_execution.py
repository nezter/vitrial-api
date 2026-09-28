"""Delivery execution: a post-quotation execution pipeline.

The iOS client can build, authorize and sync a `delivery_execution` record, but
until this landed the server rejected it in three independent places — the
`EntityType` Literal, the canonical ownership dispatcher, and the lifecycle
dispatcher. The client is the source of truth for the transition graph
(`DeliveryExecution.allowedTransitions`), and these tests pin the server to it.
"""

from __future__ import annotations

import base64
import json
from typing import get_args

import pytest

from app.auth import Principal
from app.lifecycle import LifecycleRejected, validate_delivery_execution_lifecycle
from app.ownership import ENTITY_ORDER, mutation_sort_key
from app.schemas import Capability, EntityType, SyncRecord


def principal(*, user_id: str = "user-1", session_id: str = "session-1") -> Principal:
    return Principal(
        user_id=user_id,
        organization_id="org-1",
        membership_id="membership-1",
        session_id=session_id,
        authorization_revision=7,
        capabilities=frozenset({"sync", "delivery.manage"}),
        customer_ids=frozenset({"customer-1"}),
        project_ids=frozenset({"project-1"}),
        all_customers=False,
        all_projects=False,
    )


def provenance(actor: Principal) -> dict:
    return {
        "actorID": actor.user_id,
        "organizationID": actor.organization_id,
        "membershipID": actor.membership_id,
        "sessionID": actor.session_id,
        "authorizationRevision": actor.authorization_revision,
    }


def event(
    actor: Principal,
    event_id: str,
    *,
    from_status: str | None,
    to_status: str,
) -> dict:
    return {
        "id": event_id,
        "fromStatus": from_status,
        "toStatus": to_status,
        "occurredAt": "2026-09-28T00:00:00Z",
        "note": None,
        **provenance(actor),
    }


def payload(actor: Principal, *, status: str, events: list[dict]) -> dict:
    return {
        "id": "execution-1",
        "quotationID": "quotation-1",
        "quotationNumber": "Q-0001",
        "quotationRevision": 1,
        "projectID": "project-1",
        "customerID": "customer-1",
        "currencyCode": "USD",
        "quotedTotal": "12500.00",
        "pricingVersion": "price-3",
        "startedAt": "2026-09-20T00:00:00Z",
        "updatedAt": "2026-09-28T00:00:00Z",
        "status": status,
        "events": events,
    }


async def opening(actor: Principal) -> dict:
    """A freshly created execution in its initial state."""
    return payload(
        actor,
        status="engineeringReview",
        events=[event(actor, "event-1", from_status=None, to_status="engineeringReview")],
    )


# --- wire schema ----------------------------------------------------------


def test_capability_and_entity_type_are_part_of_the_vocabulary():
    # The client's ProjectCapability and entity vocabularies are strict supersets
    # of the server's. If these ever drift apart again the client's delivery
    # feature is dead on arrival, which is exactly what this test guards.
    assert "delivery.manage" in get_args(Capability)
    assert "delivery_execution" in get_args(EntityType)


def test_sync_record_accepts_a_delivery_execution():
    body = base64.b64encode(json.dumps({"status": "engineeringReview"}).encode()).decode()
    SyncRecord.model_validate(
        {
            "id": "record-1",
            "clientMutationID": "mutation-1",
            "entityType": "delivery_execution",
            "entityID": "execution-1",
            "updatedAt": "2026-09-28T00:00:00Z",
            "payload": body,
        }
    )


# --- batch ordering -------------------------------------------------------


def test_delivery_execution_is_authorized_after_its_quotation():
    # The client sends the approved quotation and the execution it opens in one
    # atomic batch, in arbitrary client order. Ordering here is what lets that
    # batch resolve regardless of the order the client happened to emit.
    assert ENTITY_ORDER["delivery_execution"] > ENTITY_ORDER["quotation"]
    assert mutation_sort_key("delivery_execution", 0) > mutation_sort_key("quotation", 99)


# --- lifecycle ------------------------------------------------------------


@pytest.mark.asyncio
async def test_opening_an_execution_at_the_initial_status_is_accepted():
    actor = principal()
    await validate_delivery_execution_lifecycle(
        None, await opening(actor), None, actor, entity_id="execution-1"
    )


@pytest.mark.asyncio
async def test_each_pipeline_stage_is_reachable_by_its_own_transition():
    actor = principal()
    await validate_delivery_execution_lifecycle(
        None,
        payload(
            actor,
            status="production",
            events=[
                event(actor, "e1", from_status=None, to_status="engineeringReview"),
                event(actor, "e2", from_status="engineeringReview", to_status="materialsRequired"),
                event(actor, "e3", from_status="materialsRequired", to_status="procurement"),
                event(actor, "e4", from_status="procurement", to_status="production"),
            ],
        ),
        None,
        actor,
        entity_id="execution-1",
    )


@pytest.mark.asyncio
async def test_skipping_a_stage_is_rejected():
    # The client's graph has no engineeringReview -> complete edge, so a batch
    # claiming one is refused rather than silently fast-forwarding the job.
    actor = principal()
    with pytest.raises(LifecycleRejected, match="transition is not permitted"):
        await validate_delivery_execution_lifecycle(
            None,
            payload(
                actor,
                status="complete",
                events=[
                    event(actor, "e1", from_status=None, to_status="engineeringReview"),
                    event(actor, "e2", from_status="engineeringReview", to_status="complete"),
                ],
            ),
            None,
            actor,
            entity_id="execution-1",
        )


@pytest.mark.asyncio
async def test_backwards_transition_is_rejected():
    actor = principal()
    with pytest.raises(LifecycleRejected, match="transition is not permitted"):
        await validate_delivery_execution_lifecycle(
            None,
            payload(
                actor,
                status="materialsRequired",
                events=[
                    event(actor, "e1", from_status=None, to_status="engineeringReview"),
                    event(actor, "e2", from_status="engineeringReview", to_status="materialsRequired"),
                    event(actor, "e3", from_status="materialsRequired", to_status="procurement"),
                    # procurement has no edge back to materialsRequired
                    event(actor, "e4", from_status="procurement", to_status="materialsRequired"),
                ],
            ),
            None,
            actor,
            entity_id="execution-1",
        )


@pytest.mark.asyncio
async def test_event_from_an_unreached_stage_is_rejected():
    # Distinct from a backwards edge: the named predecessor was never reached at
    # all, so the history is incoherent rather than merely out of order.
    actor = principal()
    with pytest.raises(LifecycleRejected, match="fromStatus is not reachable"):
        await validate_delivery_execution_lifecycle(
            None,
            payload(
                actor,
                status="materialsRequired",
                events=[
                    event(actor, "e1", from_status=None, to_status="engineeringReview"),
                    event(actor, "e2", from_status="procurement", to_status="materialsRequired"),
                ],
            ),
            None,
            actor,
            entity_id="execution-1",
        )


@pytest.mark.asyncio
async def test_a_completed_execution_is_terminal():
    actor = principal()
    history = [
        event(actor, "e1", from_status=None, to_status="engineeringReview"),
        event(actor, "e2", from_status="engineeringReview", to_status="materialsRequired"),
        event(actor, "e3", from_status="materialsRequired", to_status="procurement"),
        event(actor, "e4", from_status="procurement", to_status="production"),
        event(actor, "e5", from_status="production", to_status="readyForInstallation"),
        event(actor, "e6", from_status="readyForInstallation", to_status="scheduled"),
        event(actor, "e7", from_status="scheduled", to_status="installed"),
        event(actor, "e8", from_status="installed", to_status="complete"),
    ]
    await validate_delivery_execution_lifecycle(
        None, payload(actor, status="complete", events=history), None, actor, entity_id="execution-1"
    )
    # Re-entering a completed job would reopen work the client considers done.
    with pytest.raises(LifecycleRejected, match="transition is not permitted"):
        await validate_delivery_execution_lifecycle(
            None,
            payload(
                actor,
                status="installed",
                events=history + [event(actor, "e9", from_status="complete", to_status="installed")],
            ),
            None,
            actor,
            entity_id="execution-1",
        )


@pytest.mark.asyncio
async def test_note_only_update_of_the_current_stage_is_allowed():
    # Every non-terminal stage lists itself in allowedTransitions, which is how
    # the client records a note without advancing the job.
    actor = principal()
    await validate_delivery_execution_lifecycle(
        None,
        payload(
            actor,
            status="procurement",
            events=[
                event(actor, "e1", from_status=None, to_status="engineeringReview"),
                event(actor, "e2", from_status="engineeringReview", to_status="materialsRequired"),
                event(actor, "e3", from_status="materialsRequired", to_status="procurement"),
                event(actor, "e4", from_status="procurement", to_status="procurement"),
            ],
        ),
        None,
        actor,
        entity_id="execution-1",
    )


@pytest.mark.asyncio
async def test_status_must_be_reachable_from_the_recorded_history():
    actor = principal()
    with pytest.raises(LifecycleRejected, match="not reachable from lifecycle history"):
        await validate_delivery_execution_lifecycle(
            None,
            payload(
                actor,
                status="scheduled",
                events=[event(actor, "e1", from_status=None, to_status="engineeringReview")],
            ),
            None,
            actor,
            entity_id="execution-1",
        )


@pytest.mark.asyncio
async def test_unknown_status_is_rejected():
    actor = principal()
    with pytest.raises(LifecycleRejected, match="status is invalid"):
        await validate_delivery_execution_lifecycle(
            None, payload(actor, status="shipped", events=[]), None, actor, entity_id="execution-1"
        )


@pytest.mark.asyncio
async def test_history_cannot_be_rewritten():
    # The event log is append-only: a client may add to it, never edit it.
    actor = principal()
    current = await opening(actor)
    rewritten = json.loads(json.dumps(current))
    rewritten["events"][0]["toStatus"] = "materialsRequired"
    with pytest.raises(LifecycleRejected):
        await validate_delivery_execution_lifecycle(
            None, rewritten, current, actor, entity_id="execution-1"
        )


@pytest.mark.asyncio
async def test_event_provenance_must_be_complete_and_current():
    actor = principal()
    current = await opening(actor)

    incomplete = json.loads(json.dumps(current))
    del incomplete["events"][0]["sessionID"]
    with pytest.raises(LifecycleRejected, match="provenance is incomplete"):
        await validate_delivery_execution_lifecycle(
            None, incomplete, None, actor, entity_id="execution-1"
        )

    # An event attributed to a stale session cannot be the newest one.
    stale = json.loads(json.dumps(current))
    stale["events"].append(
        event(principal(session_id="session-old"), "e2", from_status="engineeringReview", to_status="materialsRequired")
    )
    stale["status"] = "materialsRequired"
    with pytest.raises(LifecycleRejected, match="does not match authenticated provenance"):
        await validate_delivery_execution_lifecycle(
            None, stale, current, actor, entity_id="execution-1"
        )


@pytest.mark.asyncio
async def test_commercial_handoff_is_immutable():
    # Re-pointing an execution at another quotation, or restating the totals it
    # was raised against, would silently rewrite what the customer agreed to buy.
    actor = principal()
    current = await opening(actor)
    for field, value in (
        ("quotationID", "quotation-2"),
        ("quotationRevision", 2),
        ("quotedTotal", "1.00"),
        ("currencyCode", "EUR"),
        ("customerID", "customer-9"),
    ):
        tampered = json.loads(json.dumps(current))
        tampered[field] = value
        with pytest.raises(LifecycleRejected, match="is immutable"):
            await validate_delivery_execution_lifecycle(
                None, tampered, current, actor, entity_id="execution-1"
            )


@pytest.mark.asyncio
async def test_operational_plans_stay_mutable():
    # Everything the client treats as mutable across the job's life.
    actor = principal()
    current = await opening(actor)
    advanced = json.loads(json.dumps(current))
    advanced["status"] = "materialsRequired"
    advanced["materialsPlan"] = {"revision": 1, "lines": []}
    advanced["events"].append(
        event(actor, "e2", from_status="engineeringReview", to_status="materialsRequired")
    )
    await validate_delivery_execution_lifecycle(
        None, advanced, current, actor, entity_id="execution-1"
    )
