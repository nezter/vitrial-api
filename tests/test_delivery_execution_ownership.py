"""Canonical ownership and authorization for `delivery_execution`.

A delivery execution is only meaningful against a quotation the customer
actually approved, so the server re-derives that fact from its own canonical
records rather than trusting the payload. These tests drive `authorize_record`
with a stub session, which is enough because the branch reads only the canonical
project and project-child tables.
"""

from __future__ import annotations

import pytest

from app.auth import Principal
from app.models import CanonicalProject, CanonicalProjectChild, SyncEntity
from app.ownership import AuthorizationRejected, EffectiveScope, authorize_record


def principal(*, capabilities: frozenset[str] | None = None) -> Principal:
    return Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=7,
        capabilities=capabilities if capabilities is not None else frozenset({"delivery.manage"}),
        customer_ids=frozenset({"customer-1"}),
        project_ids=frozenset({"project-1"}),
        all_customers=False,
        all_projects=False,
    )


def quotation_row(*, project_id: str = "project-1") -> CanonicalProjectChild:
    """The canonical child row: Project linkage only, no payload."""
    return CanonicalProjectChild(
        organization_id="org-1",
        entity_type="quotation",
        entity_id="quotation-1",
        project_id=project_id,
    )


def approved_quotation_payload() -> dict:
    """The commercial payload, which lives on the generic entity row."""
    return {
        "id": "quotation-1",
        "projectID": "project-1",
        "customerID": "customer-1",
        "status": "approved",
        "events": [{"id": "qe-1", "kind": "approved"}],
    }


def quotation_entity(payload: dict) -> SyncEntity:
    return SyncEntity(
        organization_id="org-1",
        entity_type="quotation",
        entity_id="quotation-1",
        payload_json=payload,
    )


def project() -> CanonicalProject:
    return CanonicalProject(
        organization_id="org-1",
        project_id="project-1",
        customer_id="customer-1",
    )


class StubSession:
    """Serves only the tables the delivery branch reads."""

    def __init__(self, **rows: object) -> None:
        self.rows = rows

    async def get(self, model, key):
        table = {
            CanonicalProject: self.rows.get("projects"),
            CanonicalProjectChild: self.rows.get("children"),
            SyncEntity: self.rows.get("entities"),
        }.get(model)
        if table is None:
            return None
        return table.get(key)


def child(entity_type: str, entity_id: str, *, project_id: str = "project-1") -> CanonicalProjectChild:
    return CanonicalProjectChild(
        organization_id="org-1",
        entity_type=entity_type,
        entity_id=entity_id,
        project_id=project_id,
    )


def delivery_payload(**overrides) -> dict:
    payload = {
        "id": "execution-1",
        "quotationID": "quotation-1",
        "customerID": "customer-1",
        "status": "engineeringReview",
    }
    payload.update(overrides)
    return payload


async def authorize(
    session: StubSession,
    actor: Principal,
    payload: dict,
    *,
    deleted_at=None,
    generic_entity_exists: bool = False,
):
    return await authorize_record(
        session,
        actor,
        EffectiveScope.from_principal(actor),
        entity_type="delivery_execution",
        entity_id="execution-1",
        payload=payload,
        deleted_at=deleted_at,
        generic_entity_exists=generic_entity_exists,
    )


def session(
    *,
    quotation: CanonicalProjectChild | None = ...,
    quotation_payload: dict | None = ...,
    stored_payload: dict | None = None,
    existing_child: bool = False,
) -> StubSession:
    children = {}
    entities = {}
    if quotation is ...:
        children[("org-1", "quotation", "quotation-1")] = quotation_row()
        entities[("org-1", "quotation", "quotation-1")] = quotation_entity(
            approved_quotation_payload() if quotation_payload is ... else quotation_payload
        )
    elif quotation is not None:
        children[("org-1", "quotation", "quotation-1")] = quotation
        if quotation_payload is not ... and quotation_payload is not None:
            entities[("org-1", "quotation", "quotation-1")] = quotation_entity(quotation_payload)
    if existing_child:
        children[("org-1", "delivery_execution", "execution-1")] = child(
            "delivery_execution", "execution-1"
        )
    if stored_payload is not None:
        entities[("org-1", "delivery_execution", "execution-1")] = SyncEntity(
            organization_id="org-1",
            entity_type="delivery_execution",
            entity_id="execution-1",
            payload_json=stored_payload,
        )
    return StubSession(
        projects={("org-1", "project-1"): project()},
        children=children,
        entities=entities,
    )


# --- the happy path -------------------------------------------------------


@pytest.mark.asyncio
async def test_opening_an_execution_against_an_approved_quotation_is_authorized():
    actor = principal()
    plan = await authorize(session(), actor, delivery_payload())
    assert plan.entity_type == "delivery_execution"
    assert plan.entity_id == "execution-1"
    assert plan.is_create is True
    assert plan.project_id == "project-1"
    assert plan.customer_id == "customer-1"


# --- the approval precondition -------------------------------------------


@pytest.mark.asyncio
async def test_delivery_requires_the_delivery_manage_capability():
    # Having only the quotation capabilities is not enough: delivery is its own
    # authority, and the client's policy model treats them as distinct.
    actor = principal(capabilities=frozenset({"quotation.create", "quotation.send"}))
    with pytest.raises(AuthorizationRejected, match="delivery.manage capability required"):
        await authorize(session(), actor, delivery_payload())


@pytest.mark.asyncio
async def test_delivery_requires_an_approved_quotation():
    draft = {**approved_quotation_payload(), "status": "draft"}
    with pytest.raises(AuthorizationRejected, match="Quotation must be approved"):
        await authorize(session(quotation_payload=draft), principal(), delivery_payload())


@pytest.mark.asyncio
async def test_an_approved_status_without_the_approval_event_is_refused():
    # Status alone is client-asserted. The immutable event is what the server can
    # actually verify, so a quotation claiming "approved" with no approval event
    # in its history cannot open delivery work.
    forged = {**approved_quotation_payload(), "events": [{"id": "qe-1", "kind": "sent"}]}
    with pytest.raises(AuthorizationRejected, match="without its approval event"):
        await authorize(session(quotation_payload=forged), principal(), delivery_payload())


@pytest.mark.asyncio
async def test_delivery_requires_a_canonical_active_quotation():
    with pytest.raises(AuthorizationRejected, match="not canonical and active"):
        await authorize(session(quotation=None), principal(), delivery_payload())

    from datetime import datetime, timezone

    tombstoned = quotation_row()
    tombstoned.deleted_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with pytest.raises(AuthorizationRejected, match="not canonical and active"):
        await authorize(
            session(quotation=tombstoned, quotation_payload=approved_quotation_payload()),
            principal(),
            delivery_payload(),
        )


@pytest.mark.asyncio
async def test_delivery_customer_must_match_the_canonical_project():
    with pytest.raises(AuthorizationRejected, match="Customer does not match canonical Project"):
        await authorize(
            session(), principal(), delivery_payload(customerID="customer-other")
        )


# --- linkage immutability -------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_execution_cannot_be_repointed_at_another_quotation():
    # The stored payload is the authority, so restating quotationID in the
    # incoming record cannot move an execution onto different commercial terms.
    stored = delivery_payload()
    with pytest.raises(AuthorizationRejected, match="Quotation ownership is immutable"):
        await authorize(
            session(stored_payload=stored, existing_child=True),
            principal(),
            delivery_payload(quotationID="quotation-2"),
        )


@pytest.mark.asyncio
async def test_updating_a_stored_execution_keeps_its_quotation():
    actor = principal()
    stored = delivery_payload(status="engineeringReview")
    plan = await authorize(
        session(stored_payload=stored, existing_child=True),
        actor,
        delivery_payload(status="materialsRequired"),
    )
    assert plan.is_create is False
    assert plan.project_id == "project-1"


@pytest.mark.asyncio
async def test_a_generic_record_without_canonical_ownership_is_refused():
    # An execution that exists in the generic table but has no canonical project
    # child row is an orphan from an earlier bug or a forged write; it must not be
    # silently adopted as a fresh create.
    with pytest.raises(AuthorizationRejected, match="lacks canonical ownership"):
        await authorize(
            session(stored_payload=delivery_payload()),
            principal(),
            delivery_payload(),
            generic_entity_exists=True,
        )


# --- identity and tombstones ---------------------------------------------


@pytest.mark.asyncio
async def test_payload_identity_must_match_the_entity_id():
    with pytest.raises(AuthorizationRejected, match="payload identity does not match"):
        await authorize(session(), principal(), delivery_payload(id="execution-other"))


@pytest.mark.asyncio
async def test_an_unknown_execution_cannot_be_tombstoned():
    from datetime import datetime, timezone

    with pytest.raises(AuthorizationRejected, match="cannot tombstone an unknown"):
        await authorize(
            session(),
            principal(),
            {},
            deleted_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
