from __future__ import annotations

import pytest

from app.auth import Principal
from app.delivery_execution import DeliveryExecutionRejected, validate_delivery_execution_payload


def principal(*, session_id: str = "session-1") -> Principal:
    return Principal(
        user_id="user-1",
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


def event(actor: Principal, event_id: str, from_status: str | None, to_status: str, at: str) -> dict:
    return {
        "id": event_id,
        "fromStatus": from_status,
        "toStatus": to_status,
        "occurredAt": at,
        "note": None,
        **provenance(actor),
    }


def production_stage(actor: Principal) -> dict:
    started = "2026-09-17T14:00:00Z"
    materials_at = "2026-09-17T14:05:00Z"
    materials_plan_at = "2026-09-17T14:06:00Z"
    procurement_at = "2026-09-17T14:10:00Z"
    procurement_plan_at = "2026-09-17T14:11:00Z"
    production_at = "2026-09-17T14:12:00Z"
    return {
        "id": "delivery-1",
        "quotationID": "quotation-1",
        "quotationNumber": "Q-2026-001",
        "quotationRevision": 2,
        "projectID": "project-1",
        "customerID": "customer-1",
        "currencyCode": "COP",
        "quotedTotal": 450,
        "pricingVersion": "price-book-v1",
        "startedAt": started,
        "updatedAt": production_at,
        "status": "production",
        "events": [
            event(actor, "delivery-event-1", None, "engineeringReview", started),
            event(actor, "delivery-event-2", "engineeringReview", "materialsRequired", materials_at),
            event(actor, "materials-plan-event-1", "materialsRequired", "materialsRequired", materials_plan_at),
            event(actor, "delivery-event-3", "materialsRequired", "procurement", procurement_at),
            event(actor, "procurement-plan-event-1", "procurement", "procurement", procurement_plan_at),
            event(actor, "delivery-event-4", "procurement", "production", production_at),
        ],
        "materialsPlan": {
            "revision": 1,
            "requirements": [
                {
                    "id": "material-1",
                    "description": "Aluminum profile",
                    "quantity": 6,
                    "unit": "length",
                    "note": None,
                }
            ],
            "updatedAt": materials_plan_at,
            **provenance(actor),
        },
        "procurementPlan": {
            "revision": 1,
            "materialsPlanRevision": 1,
            "requirements": [
                {
                    "requirementID": "material-1",
                    "status": "available",
                    "shortageQuantity": None,
                    "leadTimeDays": None,
                    "expectedAvailableAt": None,
                    "note": None,
                }
            ],
            "updatedAt": procurement_plan_at,
            **provenance(actor),
        },
    }


def production_plan(
    actor: Principal,
    *,
    revision: int = 1,
    at: str = "2026-09-17T14:13:00Z",
    first_status: str = "pending",
    second_status: str = "pending",
) -> dict:
    return {
        "revision": revision,
        "procurementPlanRevision": 1,
        "steps": [
            {
                "id": "production-step-1",
                "description": "Fabricate frame",
                "status": first_status,
                "note": None,
            },
            {
                "id": "production-step-2",
                "description": "Glaze and inspect assembly",
                "status": second_status,
                "note": None,
            },
        ],
        "updatedAt": at,
        **provenance(actor),
    }


def apply_production_plan(
    current: dict,
    actor: Principal,
    *,
    revision: int = 1,
    at: str = "2026-09-17T14:13:00Z",
    event_id: str = "production-plan-event-1",
    first_status: str = "pending",
    second_status: str = "pending",
) -> dict:
    proposed = dict(current)
    proposed["productionPlan"] = production_plan(
        actor,
        revision=revision,
        at=at,
        first_status=first_status,
        second_status=second_status,
    )
    proposed["updatedAt"] = at
    proposed["events"] = current["events"] + [
        event(actor, event_id, "production", "production", at)
    ]
    return proposed


def test_production_plan_revision_is_current_actor_bound_and_monotonic():
    actor = principal()
    current = production_stage(actor)
    first = apply_production_plan(current, actor)
    validate_delivery_execution_payload(first, current, actor, entity_id="delivery-1")

    second = apply_production_plan(
        first,
        actor,
        revision=2,
        at="2026-09-17T14:14:00Z",
        event_id="production-plan-event-2",
        first_status="inProgress",
        second_status="complete",
    )
    validate_delivery_execution_payload(second, first, actor, entity_id="delivery-1")

    skipped = apply_production_plan(
        second,
        actor,
        revision=4,
        at="2026-09-17T14:15:00Z",
        event_id="production-plan-event-4",
    )
    with pytest.raises(DeliveryExecutionRejected, match="revision must advance by exactly one"):
        validate_delivery_execution_payload(skipped, second, actor, entity_id="delivery-1")

    stale = principal(session_id="stale-session")
    forged = apply_production_plan(current, stale)
    with pytest.raises(DeliveryExecutionRejected, match="does not match authenticated provenance"):
        validate_delivery_execution_payload(forged, current, actor, entity_id="delivery-1")


def test_ready_for_installation_requires_saved_fully_complete_production_plan():
    actor = principal()
    current = production_stage(actor)
    ready_at = "2026-09-17T14:20:00Z"

    missing = dict(current)
    missing["status"] = "readyForInstallation"
    missing["updatedAt"] = ready_at
    missing["events"] = current["events"] + [
        event(actor, "delivery-event-ready-missing", "production", "readyForInstallation", ready_at)
    ]
    with pytest.raises(DeliveryExecutionRejected, match="save delivery production work"):
        validate_delivery_execution_payload(missing, current, actor, entity_id="delivery-1")

    blocked = apply_production_plan(current, actor, first_status="complete", second_status="pending")
    validate_delivery_execution_payload(blocked, current, actor, entity_id="delivery-1")

    blocked_ready = dict(blocked)
    blocked_ready["status"] = "readyForInstallation"
    blocked_ready["updatedAt"] = ready_at
    blocked_ready["events"] = blocked["events"] + [
        event(actor, "delivery-event-ready-blocked", "production", "readyForInstallation", ready_at)
    ]
    with pytest.raises(DeliveryExecutionRejected, match="blocked until every production step is complete"):
        validate_delivery_execution_payload(blocked_ready, blocked, actor, entity_id="delivery-1")

    complete = apply_production_plan(
        blocked,
        actor,
        revision=2,
        at="2026-09-17T14:15:00Z",
        event_id="production-plan-event-2",
        first_status="complete",
        second_status="complete",
    )
    validate_delivery_execution_payload(complete, blocked, actor, entity_id="delivery-1")

    ready = dict(complete)
    ready["status"] = "readyForInstallation"
    ready["updatedAt"] = ready_at
    ready["events"] = complete["events"] + [
        event(actor, "delivery-event-ready", "production", "readyForInstallation", ready_at)
    ]
    validate_delivery_execution_payload(ready, complete, actor, entity_id="delivery-1")

    mutated = dict(ready)
    mutated["productionPlan"] = production_plan(
        actor,
        revision=3,
        at="2026-09-17T14:21:00Z",
        first_status="complete",
        second_status="complete",
    )
    with pytest.raises(DeliveryExecutionRejected, match="immutable after Ready for Installation"):
        validate_delivery_execution_payload(mutated, ready, actor, entity_id="delivery-1")


def test_production_plan_rejects_duplicate_invalid_status_and_procurement_revision_mismatch():
    actor = principal()
    current = production_stage(actor)

    duplicate = apply_production_plan(current, actor)
    duplicate["productionPlan"]["steps"][1]["id"] = "production-step-1"
    with pytest.raises(DeliveryExecutionRejected, match="ids must be unique"):
        validate_delivery_execution_payload(duplicate, current, actor, entity_id="delivery-1")

    invalid_status = apply_production_plan(current, actor)
    invalid_status["productionPlan"]["steps"][0]["status"] = "blocked"
    with pytest.raises(DeliveryExecutionRejected, match="step status is invalid"):
        validate_delivery_execution_payload(invalid_status, current, actor, entity_id="delivery-1")

    empty_description = apply_production_plan(current, actor)
    empty_description["productionPlan"]["steps"][0]["description"] = "  "
    with pytest.raises(DeliveryExecutionRejected, match="step description is required"):
        validate_delivery_execution_payload(empty_description, current, actor, entity_id="delivery-1")

    wrong_revision = apply_production_plan(current, actor)
    wrong_revision["productionPlan"]["procurementPlanRevision"] = 2
    with pytest.raises(DeliveryExecutionRejected, match="procurement revision does not match"):
        validate_delivery_execution_payload(wrong_revision, current, actor, entity_id="delivery-1")


def test_production_self_status_event_requires_real_plan_revision():
    actor = principal()
    current = production_stage(actor)
    planned = apply_production_plan(current, actor)
    validate_delivery_execution_payload(planned, current, actor, entity_id="delivery-1")

    no_op = dict(planned)
    no_op["updatedAt"] = "2026-09-17T14:14:00Z"
    no_op["events"] = planned["events"] + [
        event(actor, "production-plan-no-op", "production", "production", "2026-09-17T14:14:00Z")
    ]
    with pytest.raises(DeliveryExecutionRejected, match="requires a production plan revision"):
        validate_delivery_execution_payload(no_op, planned, actor, entity_id="delivery-1")


def test_production_plan_cannot_change_without_audit_event():
    actor = principal()
    current = production_stage(actor)
    proposed = dict(current)
    proposed["productionPlan"] = production_plan(actor, at=current["updatedAt"])
    with pytest.raises(DeliveryExecutionRejected, match="operational changes must append"):
        validate_delivery_execution_payload(proposed, current, actor, entity_id="delivery-1")
