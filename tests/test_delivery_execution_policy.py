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


def payload(actor: Principal) -> dict:
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


def test_delivery_execution_create_requires_engineering_review_and_current_actor():
    actor = principal()
    value = payload(actor)
    validate_delivery_execution_payload(value, None, actor, entity_id="delivery-1")

    skipped = dict(value)
    skipped["status"] = "production"
    skipped["events"] = [dict(value["events"][0]) | {"toStatus": "production"}]
    with pytest.raises(DeliveryExecutionRejected, match="start with Engineering Review"):
        validate_delivery_execution_payload(skipped, None, actor, entity_id="delivery-1")

    other = principal(user_id="other-user", session_id="other-session")
    forged = payload(other)
    with pytest.raises(DeliveryExecutionRejected, match="authenticated provenance"):
        validate_delivery_execution_payload(forged, None, actor, entity_id="delivery-1")


def test_delivery_execution_appends_exactly_one_forward_transition():
    actor = principal()
    current = payload(actor)
    next_time = "2026-09-17T14:05:00Z"
    proposed = dict(current)
    proposed["status"] = "materialsRequired"
    proposed["updatedAt"] = next_time
    proposed["events"] = current["events"] + [event(
        actor,
        "delivery-event-2",
        from_status="engineeringReview",
        to_status="materialsRequired",
        occurred_at=next_time,
    )]
    validate_delivery_execution_payload(proposed, current, actor, entity_id="delivery-1")

    skipped = dict(current)
    skipped["status"] = "production"
    skipped["updatedAt"] = next_time
    skipped["events"] = current["events"] + [event(
        actor,
        "delivery-event-skip",
        from_status="engineeringReview",
        to_status="production",
        occurred_at=next_time,
    )]
    with pytest.raises(DeliveryExecutionRejected, match="transition is invalid"):
        validate_delivery_execution_payload(skipped, current, actor, entity_id="delivery-1")


def test_delivery_execution_history_and_commercial_handoff_are_immutable():
    actor = principal()
    current = payload(actor)

    rewritten = dict(current)
    rewritten["events"] = [dict(current["events"][0]) | {"note": "rewritten"}]
    with pytest.raises(DeliveryExecutionRejected, match="history is immutable"):
        validate_delivery_execution_payload(rewritten, current, actor, entity_id="delivery-1")

    changed_quote = dict(current)
    changed_quote["quotationRevision"] = 3
    with pytest.raises(DeliveryExecutionRejected, match="immutable handoff field quotationRevision"):
        validate_delivery_execution_payload(changed_quote, current, actor, entity_id="delivery-1")
