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


def installed_stage(actor: Principal) -> dict:
    started = "2026-09-17T14:00:00Z"
    materials_at = "2026-09-17T14:05:00Z"
    materials_plan_at = "2026-09-17T14:06:00Z"
    procurement_at = "2026-09-17T14:10:00Z"
    procurement_plan_at = "2026-09-17T14:11:00Z"
    production_at = "2026-09-17T14:12:00Z"
    production_plan_at = "2026-09-17T14:13:00Z"
    ready_at = "2026-09-17T14:14:00Z"
    schedule_at = "2026-09-17T14:15:00Z"
    scheduled_at = "2026-09-17T14:20:00Z"
    completion_at = "2026-09-20T16:00:00Z"
    installed_at = "2026-09-20T16:10:00Z"
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
        "updatedAt": installed_at,
        "status": "installed",
        "events": [
            event(actor, "delivery-event-1", None, "engineeringReview", started),
            event(actor, "delivery-event-2", "engineeringReview", "materialsRequired", materials_at),
            event(actor, "materials-plan-event-1", "materialsRequired", "materialsRequired", materials_plan_at),
            event(actor, "delivery-event-3", "materialsRequired", "procurement", procurement_at),
            event(actor, "procurement-plan-event-1", "procurement", "procurement", procurement_plan_at),
            event(actor, "delivery-event-4", "procurement", "production", production_at),
            event(actor, "production-plan-event-1", "production", "production", production_plan_at),
            event(actor, "delivery-event-5", "production", "readyForInstallation", ready_at),
            event(actor, "installation-schedule-event-1", "readyForInstallation", "readyForInstallation", schedule_at),
            event(actor, "delivery-event-scheduled", "readyForInstallation", "scheduled", scheduled_at),
            event(actor, "installation-completion-event-1", "scheduled", "scheduled", completion_at),
            event(actor, "delivery-event-installed", "scheduled", "installed", installed_at),
        ],
        "materialsPlan": {
            "revision": 1,
            "requirements": [{
                "id": "material-1",
                "description": "Aluminum profile",
                "quantity": 6,
                "unit": "length",
                "note": None,
            }],
            "updatedAt": materials_plan_at,
            **provenance(actor),
        },
        "procurementPlan": {
            "revision": 1,
            "materialsPlanRevision": 1,
            "requirements": [{
                "requirementID": "material-1",
                "status": "available",
                "shortageQuantity": None,
                "leadTimeDays": None,
                "expectedAvailableAt": None,
                "note": None,
            }],
            "updatedAt": procurement_plan_at,
            **provenance(actor),
        },
        "productionPlan": {
            "revision": 1,
            "procurementPlanRevision": 1,
            "steps": [{
                "id": "production-step-1",
                "description": "Fabricate and inspect assembly",
                "status": "complete",
                "note": None,
            }],
            "updatedAt": production_plan_at,
            **provenance(actor),
        },
        "installationSchedule": {
            "revision": 1,
            "productionPlanRevision": 1,
            "scheduledFor": "2026-09-20T13:00:00Z",
            "note": "Customer confirmed",
            "updatedAt": schedule_at,
            **provenance(actor),
        },
        "installationCompletion": {
            "revision": 1,
            "installationScheduleRevision": 1,
            "completedAt": "2026-09-20T15:55:00Z",
            "evidenceReferences": [{"itemID": "item-1", "documentID": "evidence-installation"}],
            "note": "Installed and photographed",
            "updatedAt": completion_at,
            **provenance(actor),
        },
    }


def closeout(actor: Principal, *, completion_revision: int = 1, note: str | None = "Final delivery package recorded") -> dict:
    return {
        "revision": 1,
        "installationCompletionRevision": completion_revision,
        "closedAt": "2026-09-20T17:00:00Z",
        "evidenceReferences": [{"itemID": "item-1", "documentID": "evidence-closeout"}],
        "note": note,
        "updatedAt": "2026-09-20T17:05:00Z",
        **provenance(actor),
    }


def complete_transition(current: dict, actor: Principal, *, closeout_actor: Principal | None = None) -> dict:
    proposed = dict(current)
    proposed["status"] = "complete"
    proposed["updatedAt"] = "2026-09-20T17:05:00Z"
    proposed["deliveryCloseout"] = closeout(closeout_actor or actor)
    proposed["events"] = current["events"] + [
        event(actor, "delivery-event-complete", "installed", "complete", "2026-09-20T17:05:00Z")
    ]
    return proposed


def test_complete_requires_atomic_closeout_snapshot():
    actor = principal()
    current = installed_stage(actor)
    missing = dict(current)
    missing["status"] = "complete"
    missing["updatedAt"] = "2026-09-20T17:05:00Z"
    missing["events"] = current["events"] + [
        event(actor, "delivery-event-complete-missing", "installed", "complete", "2026-09-20T17:05:00Z")
    ]
    with pytest.raises(DeliveryExecutionRejected, match="record delivery closeout"):
        validate_delivery_execution_payload(missing, current, actor, entity_id="delivery-1")

    proposed = complete_transition(current, actor)
    validate_delivery_execution_payload(proposed, current, actor, entity_id="delivery-1")


def test_closeout_must_bind_exact_installation_completion_and_current_session():
    actor = principal()
    current = installed_stage(actor)

    wrong_revision = complete_transition(current, actor)
    wrong_revision["deliveryCloseout"]["installationCompletionRevision"] = 2
    with pytest.raises(DeliveryExecutionRejected, match="completion revision does not match"):
        validate_delivery_execution_payload(wrong_revision, current, actor, entity_id="delivery-1")

    stale = principal(session_id="stale-session")
    forged = complete_transition(current, actor, closeout_actor=stale)
    with pytest.raises(DeliveryExecutionRejected, match="closeout does not match authenticated provenance"):
        validate_delivery_execution_payload(forged, current, actor, entity_id="delivery-1")


def test_closeout_requires_final_record_and_unique_valid_evidence():
    actor = principal()
    current = installed_stage(actor)

    empty = complete_transition(current, actor)
    empty["deliveryCloseout"]["evidenceReferences"] = []
    empty["deliveryCloseout"]["note"] = "   "
    with pytest.raises(DeliveryExecutionRejected, match="requires a final note or at least one evidence reference"):
        validate_delivery_execution_payload(empty, current, actor, entity_id="delivery-1")

    duplicate = complete_transition(current, actor)
    duplicate["deliveryCloseout"]["evidenceReferences"].append(
        {"itemID": "item-1", "documentID": "evidence-closeout"}
    )
    with pytest.raises(DeliveryExecutionRejected, match="closeout evidence references must be unique"):
        validate_delivery_execution_payload(duplicate, current, actor, entity_id="delivery-1")

    note_only = complete_transition(current, actor)
    note_only["deliveryCloseout"]["evidenceReferences"] = []
    note_only["deliveryCloseout"]["note"] = "Final punch list cleared"
    validate_delivery_execution_payload(note_only, current, actor, entity_id="delivery-1")


def test_closeout_cannot_be_presaved_or_changed_after_complete():
    actor = principal()
    current = installed_stage(actor)

    presaved = dict(current)
    presaved["deliveryCloseout"] = closeout(actor)
    with pytest.raises(DeliveryExecutionRejected, match="only be recorded with the Complete transition"):
        validate_delivery_execution_payload(presaved, current, actor, entity_id="delivery-1")

    complete = complete_transition(current, actor)
    validate_delivery_execution_payload(complete, current, actor, entity_id="delivery-1")

    mutated = dict(complete)
    mutated["deliveryCloseout"] = dict(complete["deliveryCloseout"])
    mutated["deliveryCloseout"]["note"] = "rewrite"
    with pytest.raises(DeliveryExecutionRejected, match="closeout is immutable"):
        validate_delivery_execution_payload(mutated, complete, actor, entity_id="delivery-1")
