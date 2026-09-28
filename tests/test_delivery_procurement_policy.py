from __future__ import annotations

import pytest

from app.auth import Principal
from app.delivery_execution import DeliveryExecutionRejected, validate_delivery_execution_payload


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


def event(actor: Principal, event_id: str, from_status: str | None, to_status: str, at: str) -> dict:
    return {
        "id": event_id,
        "fromStatus": from_status,
        "toStatus": to_status,
        "occurredAt": at,
        "note": None,
        **provenance(actor),
    }


def procurement_stage(actor: Principal) -> dict:
    started = "2026-09-17T14:00:00Z"
    materials_at = "2026-09-17T14:05:00Z"
    plan_at = "2026-09-17T14:06:00Z"
    procurement_at = "2026-09-17T14:10:00Z"
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
        "updatedAt": procurement_at,
        "status": "procurement",
        "events": [
            event(actor, "delivery-event-1", None, "engineeringReview", started),
            event(actor, "delivery-event-2", "engineeringReview", "materialsRequired", materials_at),
            event(actor, "materials-plan-event-1", "materialsRequired", "materialsRequired", plan_at),
            event(actor, "delivery-event-3", "materialsRequired", "procurement", procurement_at),
        ],
        "materialsPlan": {
            "revision": 1,
            "requirements": [
                {
                    "id": "material-1",
                    "description": "Aluminum profile 2x4",
                    "quantity": 12,
                    "unit": "length",
                    "note": None,
                },
                {
                    "id": "material-2",
                    "description": "Clear laminated glass",
                    "quantity": 2,
                    "unit": "sheet",
                    "note": None,
                },
            ],
            "updatedAt": plan_at,
            **provenance(actor),
        },
    }


def procurement_plan(
    actor: Principal,
    *,
    revision: int = 1,
    at: str = "2026-09-17T14:11:00Z",
    first_status: str = "ordered",
    second_status: str = "unconfirmed",
) -> dict:
    first = {
        "requirementID": "material-1",
        "status": first_status,
        "shortageQuantity": None,
        "leadTimeDays": 4 if first_status == "ordered" else None,
        "expectedAvailableAt": None,
        "note": None,
    }
    second = {
        "requirementID": "material-2",
        "status": second_status,
        "shortageQuantity": None,
        "leadTimeDays": None,
        "expectedAvailableAt": None,
        "note": None,
    }
    return {
        "revision": revision,
        "materialsPlanRevision": 1,
        "requirements": [first, second],
        "updatedAt": at,
        **provenance(actor),
    }


def apply_procurement_plan(
    current: dict,
    actor: Principal,
    *,
    revision: int = 1,
    at: str = "2026-09-17T14:11:00Z",
    event_id: str = "procurement-plan-event-1",
    first_status: str = "ordered",
    second_status: str = "unconfirmed",
) -> dict:
    proposed = dict(current)
    proposed["procurementPlan"] = procurement_plan(
        actor,
        revision=revision,
        at=at,
        first_status=first_status,
        second_status=second_status,
    )
    proposed["updatedAt"] = at
    proposed["events"] = current["events"] + [
        event(actor, event_id, "procurement", "procurement", at)
    ]
    return proposed


def test_procurement_plan_revision_is_current_actor_bound_and_monotonic():
    actor = principal()
    current = procurement_stage(actor)
    first = apply_procurement_plan(current, actor)
    validate_delivery_execution_payload(first, current, actor, entity_id="delivery-1")

    second = apply_procurement_plan(
        first,
        actor,
        revision=2,
        at="2026-09-17T14:12:00Z",
        event_id="procurement-plan-event-2",
        first_status="available",
        second_status="ordered",
    )
    validate_delivery_execution_payload(second, first, actor, entity_id="delivery-1")

    skipped = apply_procurement_plan(
        second,
        actor,
        revision=4,
        at="2026-09-17T14:13:00Z",
        event_id="procurement-plan-event-4",
    )
    with pytest.raises(DeliveryExecutionRejected, match="revision must advance by exactly one"):
        validate_delivery_execution_payload(skipped, second, actor, entity_id="delivery-1")

    stale = principal(session_id="stale-session")
    forged = apply_procurement_plan(current, stale)
    with pytest.raises(DeliveryExecutionRejected, match="does not match authenticated provenance"):
        validate_delivery_execution_payload(forged, current, actor, entity_id="delivery-1")


def test_production_requires_saved_fully_available_procurement_plan():
    actor = principal()
    current = procurement_stage(actor)
    production_at = "2026-09-17T14:20:00Z"

    missing = dict(current)
    missing["status"] = "production"
    missing["updatedAt"] = production_at
    missing["events"] = current["events"] + [
        event(actor, "delivery-event-production", "procurement", "production", production_at)
    ]
    with pytest.raises(DeliveryExecutionRejected, match="save delivery procurement readiness"):
        validate_delivery_execution_payload(missing, current, actor, entity_id="delivery-1")

    blocked = apply_procurement_plan(
        current,
        actor,
        first_status="shortage",
        second_status="available",
    )
    blocked["procurementPlan"]["requirements"][0]["shortageQuantity"] = 3
    blocked["procurementPlan"]["requirements"][0]["leadTimeDays"] = 5
    validate_delivery_execution_payload(blocked, current, actor, entity_id="delivery-1")

    blocked_production = dict(blocked)
    blocked_production["status"] = "production"
    blocked_production["updatedAt"] = production_at
    blocked_production["events"] = blocked["events"] + [
        event(actor, "delivery-event-production-blocked", "procurement", "production", production_at)
    ]
    with pytest.raises(DeliveryExecutionRejected, match="blocked until every frozen material is available"):
        validate_delivery_execution_payload(blocked_production, blocked, actor, entity_id="delivery-1")

    ready = apply_procurement_plan(
        blocked,
        actor,
        revision=2,
        at="2026-09-17T14:15:00Z",
        event_id="procurement-plan-event-2",
        first_status="available",
        second_status="available",
    )
    validate_delivery_execution_payload(ready, blocked, actor, entity_id="delivery-1")

    production = dict(ready)
    production["status"] = "production"
    production["updatedAt"] = production_at
    production["events"] = ready["events"] + [
        event(actor, "delivery-event-production-ready", "procurement", "production", production_at)
    ]
    validate_delivery_execution_payload(production, ready, actor, entity_id="delivery-1")

    mutated = dict(production)
    mutated["procurementPlan"] = procurement_plan(
        actor,
        revision=3,
        at="2026-09-17T14:21:00Z",
        first_status="available",
        second_status="available",
    )
    with pytest.raises(DeliveryExecutionRejected, match="immutable after Production starts"):
        validate_delivery_execution_payload(mutated, production, actor, entity_id="delivery-1")


def test_procurement_plan_rejects_unknown_missing_duplicate_and_invalid_shortage_data():
    actor = principal()
    current = procurement_stage(actor)

    unknown = apply_procurement_plan(current, actor)
    unknown["procurementPlan"]["requirements"][1]["requirementID"] = "material-unknown"
    with pytest.raises(DeliveryExecutionRejected, match="unknown frozen material"):
        validate_delivery_execution_payload(unknown, current, actor, entity_id="delivery-1")

    missing = apply_procurement_plan(current, actor)
    missing["procurementPlan"]["requirements"] = missing["procurementPlan"]["requirements"][:1]
    with pytest.raises(DeliveryExecutionRejected, match="missing frozen material"):
        validate_delivery_execution_payload(missing, current, actor, entity_id="delivery-1")

    duplicate = apply_procurement_plan(current, actor)
    duplicate["procurementPlan"]["requirements"][1]["requirementID"] = "material-1"
    with pytest.raises(DeliveryExecutionRejected, match="ids must be unique"):
        validate_delivery_execution_payload(duplicate, current, actor, entity_id="delivery-1")

    excessive_shortage = apply_procurement_plan(
        current,
        actor,
        first_status="shortage",
        second_status="available",
    )
    excessive_shortage["procurementPlan"]["requirements"][0]["shortageQuantity"] = 13
    with pytest.raises(DeliveryExecutionRejected, match="shortage quantity is invalid"):
        validate_delivery_execution_payload(excessive_shortage, current, actor, entity_id="delivery-1")

    invalid_lead = apply_procurement_plan(current, actor)
    invalid_lead["procurementPlan"]["requirements"][0]["leadTimeDays"] = -1
    with pytest.raises(DeliveryExecutionRejected, match="lead time is invalid"):
        validate_delivery_execution_payload(invalid_lead, current, actor, entity_id="delivery-1")

    wrong_material_revision = apply_procurement_plan(current, actor)
    wrong_material_revision["procurementPlan"]["materialsPlanRevision"] = 2
    with pytest.raises(DeliveryExecutionRejected, match="materials revision does not match"):
        validate_delivery_execution_payload(wrong_material_revision, current, actor, entity_id="delivery-1")


def test_procurement_self_status_event_requires_real_plan_revision():
    actor = principal()
    current = procurement_stage(actor)
    planned = apply_procurement_plan(current, actor)
    validate_delivery_execution_payload(planned, current, actor, entity_id="delivery-1")

    no_op = dict(planned)
    no_op["updatedAt"] = "2026-09-17T14:12:00Z"
    no_op["events"] = planned["events"] + [
        event(
            actor,
            "procurement-plan-no-op",
            "procurement",
            "procurement",
            "2026-09-17T14:12:00Z",
        )
    ]
    with pytest.raises(DeliveryExecutionRejected, match="requires a procurement plan revision"):
        validate_delivery_execution_payload(no_op, planned, actor, entity_id="delivery-1")


def test_procurement_plan_cannot_change_without_audit_event():
    actor = principal()
    current = procurement_stage(actor)
    proposed = dict(current)
    proposed["procurementPlan"] = procurement_plan(actor, at=current["updatedAt"])
    with pytest.raises(DeliveryExecutionRejected, match="operational changes must append"):
        validate_delivery_execution_payload(proposed, current, actor, entity_id="delivery-1")
