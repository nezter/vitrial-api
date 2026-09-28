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


def event(
    actor: Principal,
    event_id: str,
    *,
    from_status: str | None,
    to_status: str,
    occurred_at: str,
) -> dict:
    return {
        "id": event_id,
        "fromStatus": from_status,
        "toStatus": to_status,
        "occurredAt": occurred_at,
        "note": None,
        **provenance(actor),
    }


def execution(actor: Principal) -> dict:
    started = "2026-09-17T14:00:00Z"
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
        "updatedAt": started,
        "status": "engineeringReview",
        "events": [event(
            actor,
            "delivery-event-1",
            from_status=None,
            to_status="engineeringReview",
            occurred_at=started,
        )],
    }


def materials_stage(actor: Principal) -> dict:
    value = execution(actor)
    occurred_at = "2026-09-17T14:05:00Z"
    value["status"] = "materialsRequired"
    value["updatedAt"] = occurred_at
    value["events"] = value["events"] + [event(
        actor,
        "delivery-event-2",
        from_status="engineeringReview",
        to_status="materialsRequired",
        occurred_at=occurred_at,
    )]
    return value


def materials_plan(actor: Principal, *, revision: int = 1, updated_at: str | None = None) -> dict:
    return {
        "revision": revision,
        "requirements": [
            {
                "id": "material-1",
                "description": "Aluminum profile 2x4",
                "quantity": 12,
                "unit": "length",
                "note": "Cut list pending vendor allocation",
            }
        ],
        "updatedAt": updated_at or f"2026-09-17T14:{5 + revision:02d}:00Z",
        **provenance(actor),
    }


def apply_plan(
    current: dict,
    actor: Principal,
    *,
    revision: int,
    occurred_at: str,
    event_id: str,
    quantity: int = 12,
) -> dict:
    proposed = dict(current)
    plan = materials_plan(actor, revision=revision, updated_at=occurred_at)
    plan["requirements"] = [dict(plan["requirements"][0]) | {"quantity": quantity}]
    proposed["materialsPlan"] = plan
    proposed["updatedAt"] = occurred_at
    proposed["events"] = current["events"] + [event(
        actor,
        event_id,
        from_status="materialsRequired",
        to_status="materialsRequired",
        occurred_at=occurred_at,
    )]
    return proposed


def test_materials_plan_can_only_be_authored_in_materials_required():
    actor = principal()
    current = execution(actor)
    proposed = dict(current)
    proposed["materialsPlan"] = materials_plan(actor)

    with pytest.raises(DeliveryExecutionRejected, match="only be authored during Materials Required"):
        validate_delivery_execution_payload(proposed, current, actor, entity_id="delivery-1")


def test_materials_plan_revision_is_monotonic_and_current_actor_bound():
    actor = principal()
    current = materials_stage(actor)
    proposed = apply_plan(
        current,
        actor,
        revision=1,
        occurred_at="2026-09-17T14:06:00Z",
        event_id="materials-plan-event-1",
    )
    validate_delivery_execution_payload(proposed, current, actor, entity_id="delivery-1")

    revised = apply_plan(
        proposed,
        actor,
        revision=2,
        occurred_at="2026-09-17T14:07:00Z",
        event_id="materials-plan-event-2",
        quantity=14,
    )
    validate_delivery_execution_payload(revised, proposed, actor, entity_id="delivery-1")

    skipped = apply_plan(
        revised,
        actor,
        revision=4,
        occurred_at="2026-09-17T14:08:00Z",
        event_id="materials-plan-event-4",
    )
    with pytest.raises(DeliveryExecutionRejected, match="revision must advance by exactly one"):
        validate_delivery_execution_payload(skipped, revised, actor, entity_id="delivery-1")

    other = principal(user_id="other-user", session_id="other-session")
    forged = apply_plan(
        current,
        other,
        revision=1,
        occurred_at="2026-09-17T14:06:00Z",
        event_id="materials-plan-event-forged",
    )
    with pytest.raises(DeliveryExecutionRejected, match="does not match authenticated provenance"):
        validate_delivery_execution_payload(forged, current, actor, entity_id="delivery-1")


def test_materials_self_status_event_requires_an_actual_plan_revision():
    actor = principal()
    current = materials_stage(actor)
    planned = apply_plan(
        current,
        actor,
        revision=1,
        occurred_at="2026-09-17T14:06:00Z",
        event_id="materials-plan-event-1",
    )
    validate_delivery_execution_payload(planned, current, actor, entity_id="delivery-1")

    no_op = dict(planned)
    no_op["updatedAt"] = "2026-09-17T14:07:00Z"
    no_op["events"] = planned["events"] + [event(
        actor,
        "materials-plan-no-op",
        from_status="materialsRequired",
        to_status="materialsRequired",
        occurred_at="2026-09-17T14:07:00Z",
    )]
    with pytest.raises(DeliveryExecutionRejected, match="requires a materials plan revision"):
        validate_delivery_execution_payload(no_op, planned, actor, entity_id="delivery-1")


def test_procurement_requires_saved_plan_and_freezes_it():
    actor = principal()
    current = materials_stage(actor)
    procurement_time = "2026-09-17T14:10:00Z"

    missing = dict(current)
    missing["status"] = "procurement"
    missing["updatedAt"] = procurement_time
    missing["events"] = current["events"] + [event(
        actor,
        "delivery-event-3",
        from_status="materialsRequired",
        to_status="procurement",
        occurred_at=procurement_time,
    )]
    with pytest.raises(DeliveryExecutionRejected, match="required before Procurement"):
        validate_delivery_execution_payload(missing, current, actor, entity_id="delivery-1")

    planned = apply_plan(
        current,
        actor,
        revision=1,
        occurred_at="2026-09-17T14:06:00Z",
        event_id="materials-plan-event-1",
    )
    validate_delivery_execution_payload(planned, current, actor, entity_id="delivery-1")

    procurement = dict(planned)
    procurement["status"] = "procurement"
    procurement["updatedAt"] = procurement_time
    procurement["events"] = planned["events"] + [event(
        actor,
        "delivery-event-3",
        from_status="materialsRequired",
        to_status="procurement",
        occurred_at=procurement_time,
    )]
    validate_delivery_execution_payload(procurement, planned, actor, entity_id="delivery-1")

    mutated = dict(procurement)
    mutated["materialsPlan"] = materials_plan(
        actor,
        revision=2,
        updated_at="2026-09-17T14:11:00Z",
    )
    with pytest.raises(DeliveryExecutionRejected, match="immutable after Procurement starts"):
        validate_delivery_execution_payload(mutated, procurement, actor, entity_id="delivery-1")


def test_material_requirements_reject_empty_invalid_or_duplicate_lines():
    actor = principal()
    current = materials_stage(actor)

    empty = apply_plan(
        current,
        actor,
        revision=1,
        occurred_at="2026-09-17T14:06:00Z",
        event_id="materials-plan-empty",
    )
    empty["materialsPlan"]["requirements"] = []
    with pytest.raises(DeliveryExecutionRejected, match="at least one material"):
        validate_delivery_execution_payload(empty, current, actor, entity_id="delivery-1")

    invalid_quantity = apply_plan(
        current,
        actor,
        revision=1,
        occurred_at="2026-09-17T14:06:00Z",
        event_id="materials-plan-invalid-quantity",
        quantity=0,
    )
    with pytest.raises(DeliveryExecutionRejected, match="greater than zero"):
        validate_delivery_execution_payload(invalid_quantity, current, actor, entity_id="delivery-1")

    duplicate = apply_plan(
        current,
        actor,
        revision=1,
        occurred_at="2026-09-17T14:06:00Z",
        event_id="materials-plan-duplicate",
    )
    duplicate["materialsPlan"]["requirements"] = [
        duplicate["materialsPlan"]["requirements"][0],
        dict(duplicate["materialsPlan"]["requirements"][0]),
    ]
    with pytest.raises(DeliveryExecutionRejected, match="ids must be unique"):
        validate_delivery_execution_payload(duplicate, current, actor, entity_id="delivery-1")
