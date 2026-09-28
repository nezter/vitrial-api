from __future__ import annotations

import pytest

from app.auth import Principal
from app.delivery_execution import DeliveryExecutionRejected, validate_delivery_execution_payload
from app.delivery_installation import DeliveryInstallationRejected, validate_installation_evidence_authority
from app.models import CanonicalItem, CanonicalItemChild


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


def ready_stage(actor: Principal) -> dict:
    started = "2026-09-17T14:00:00Z"
    materials_at = "2026-09-17T14:05:00Z"
    materials_plan_at = "2026-09-17T14:06:00Z"
    procurement_at = "2026-09-17T14:10:00Z"
    procurement_plan_at = "2026-09-17T14:11:00Z"
    production_at = "2026-09-17T14:12:00Z"
    production_plan_at = "2026-09-17T14:13:00Z"
    ready_at = "2026-09-17T14:14:00Z"
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
        "updatedAt": ready_at,
        "status": "readyForInstallation",
        "events": [
            event(actor, "delivery-event-1", None, "engineeringReview", started),
            event(actor, "delivery-event-2", "engineeringReview", "materialsRequired", materials_at),
            event(actor, "materials-plan-event-1", "materialsRequired", "materialsRequired", materials_plan_at),
            event(actor, "delivery-event-3", "materialsRequired", "procurement", procurement_at),
            event(actor, "procurement-plan-event-1", "procurement", "procurement", procurement_plan_at),
            event(actor, "delivery-event-4", "procurement", "production", production_at),
            event(actor, "production-plan-event-1", "production", "production", production_plan_at),
            event(actor, "delivery-event-5", "production", "readyForInstallation", ready_at),
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
    }


def schedule(
    actor: Principal,
    *,
    revision: int = 1,
    at: str = "2026-09-17T14:15:00Z",
    scheduled_for: str = "2026-09-20T13:00:00Z",
    installer_id: str = "installer-crew-alpha",
    installer_name: str = "Crew Alpha",
    installer_phone: str | None = "+57 300 555 0101",
) -> dict:
    return {
        "revision": revision,
        "productionPlanRevision": 1,
        "scheduledFor": scheduled_for,
        "installerAssignment": {
            "id": installer_id,
            "displayName": installer_name,
            "phone": installer_phone,
        },
        "note": "Customer confirmed",
        "updatedAt": at,
        **provenance(actor),
    }


def apply_schedule(current: dict, actor: Principal, *, revision: int = 1, at: str = "2026-09-17T14:15:00Z", scheduled_for: str = "2026-09-20T13:00:00Z", event_id: str = "installation-schedule-event-1") -> dict:
    proposed = dict(current)
    proposed["installationSchedule"] = schedule(actor, revision=revision, at=at, scheduled_for=scheduled_for)
    proposed["updatedAt"] = at
    proposed["events"] = current["events"] + [event(actor, event_id, current["status"], current["status"], at)]
    return proposed


def scheduled_stage(actor: Principal) -> dict:
    current = ready_stage(actor)
    planned = apply_schedule(current, actor)
    validate_delivery_execution_payload(planned, current, actor, entity_id="delivery-1")
    scheduled_at = "2026-09-17T14:20:00Z"
    scheduled = dict(planned)
    scheduled["status"] = "scheduled"
    scheduled["updatedAt"] = scheduled_at
    scheduled["events"] = planned["events"] + [event(actor, "delivery-event-scheduled", "readyForInstallation", "scheduled", scheduled_at)]
    validate_delivery_execution_payload(scheduled, planned, actor, entity_id="delivery-1")
    return scheduled


def completion(actor: Principal, *, revision: int = 1, schedule_revision: int = 1, at: str = "2026-09-20T16:00:00Z", document_id: str = "evidence-1") -> dict:
    return {
        "revision": revision,
        "installationScheduleRevision": schedule_revision,
        "completedAt": "2026-09-20T15:55:00Z",
        "evidenceReferences": [{"itemID": "item-1", "documentID": document_id}],
        "note": "Installed and photographed",
        "updatedAt": at,
        **provenance(actor),
    }


def apply_completion(current: dict, actor: Principal, *, revision: int = 1, at: str = "2026-09-20T16:00:00Z", event_id: str = "installation-completion-event-1", document_id: str = "evidence-1") -> dict:
    proposed = dict(current)
    proposed["installationCompletion"] = completion(actor, revision=revision, schedule_revision=current["installationSchedule"]["revision"], at=at, document_id=document_id)
    proposed["updatedAt"] = at
    proposed["events"] = current["events"] + [event(actor, event_id, "scheduled", "scheduled", at)]
    return proposed


def test_schedule_revision_is_current_actor_bound_and_monotonic():
    actor = principal()
    current = ready_stage(actor)
    first = apply_schedule(current, actor)
    validate_delivery_execution_payload(first, current, actor, entity_id="delivery-1")
    second = apply_schedule(first, actor, revision=2, at="2026-09-17T14:16:00Z", scheduled_for="2026-09-21T13:00:00Z", event_id="installation-schedule-event-2")
    validate_delivery_execution_payload(second, first, actor, entity_id="delivery-1")
    skipped = apply_schedule(second, actor, revision=4, at="2026-09-17T14:17:00Z", event_id="installation-schedule-event-4")
    with pytest.raises(DeliveryExecutionRejected, match="revision must advance by exactly one"):
        validate_delivery_execution_payload(skipped, second, actor, entity_id="delivery-1")
    stale = principal(session_id="stale-session")
    forged = apply_schedule(current, stale)
    with pytest.raises(DeliveryExecutionRejected, match="does not match authenticated provenance"):
        validate_delivery_execution_payload(forged, current, actor, entity_id="delivery-1")


def test_scheduled_transition_requires_saved_schedule_and_rescheduling_is_audited():
    actor = principal()
    current = ready_stage(actor)
    scheduled_at = "2026-09-17T14:20:00Z"
    missing = dict(current)
    missing["status"] = "scheduled"
    missing["updatedAt"] = scheduled_at
    missing["events"] = current["events"] + [event(actor, "delivery-event-scheduled-missing", "readyForInstallation", "scheduled", scheduled_at)]
    with pytest.raises(DeliveryExecutionRejected, match="save delivery installation schedule"):
        validate_delivery_execution_payload(missing, current, actor, entity_id="delivery-1")
    scheduled = scheduled_stage(actor)
    rescheduled = apply_schedule(scheduled, actor, revision=2, at="2026-09-17T14:21:00Z", scheduled_for="2026-09-22T15:00:00Z", event_id="installation-schedule-event-rescheduled")
    validate_delivery_execution_payload(rescheduled, scheduled, actor, entity_id="delivery-1")


def test_installed_requires_saved_completion_and_completion_freezes_schedule():
    actor = principal()
    scheduled = scheduled_stage(actor)
    installed = dict(scheduled)
    installed["status"] = "installed"
    installed["updatedAt"] = "2026-09-20T16:10:00Z"
    installed["events"] = scheduled["events"] + [event(actor, "delivery-event-installed-missing", "scheduled", "installed", "2026-09-20T16:10:00Z")]
    with pytest.raises(DeliveryExecutionRejected, match="completion evidence"):
        validate_delivery_execution_payload(installed, scheduled, actor, entity_id="delivery-1")

    completed = apply_completion(scheduled, actor)
    validate_delivery_execution_payload(completed, scheduled, actor, entity_id="delivery-1")

    rescheduled = apply_schedule(completed, actor, revision=2, at="2026-09-20T16:05:00Z", scheduled_for="2026-09-23T13:00:00Z", event_id="reschedule-after-completion")
    rescheduled["installationCompletion"] = completed["installationCompletion"]
    with pytest.raises(DeliveryExecutionRejected, match="schedule is frozen once completion evidence"):
        validate_delivery_execution_payload(rescheduled, completed, actor, entity_id="delivery-1")

    installed = dict(completed)
    installed["status"] = "installed"
    installed["updatedAt"] = "2026-09-20T16:10:00Z"
    installed["events"] = completed["events"] + [event(actor, "delivery-event-installed", "scheduled", "installed", "2026-09-20T16:10:00Z")]
    validate_delivery_execution_payload(installed, completed, actor, entity_id="delivery-1")

    mutated = dict(installed)
    mutated["installationCompletion"] = completion(actor, revision=2, at="2026-09-20T16:11:00Z", document_id="evidence-2")
    with pytest.raises(DeliveryExecutionRejected, match="completion is immutable after installation"):
        validate_delivery_execution_payload(mutated, installed, actor, entity_id="delivery-1")


def test_completion_revision_is_current_actor_bound_monotonic_and_evidence_validated():
    actor = principal()
    scheduled = scheduled_stage(actor)
    first = apply_completion(scheduled, actor)
    validate_delivery_execution_payload(first, scheduled, actor, entity_id="delivery-1")
    second = apply_completion(first, actor, revision=2, at="2026-09-20T16:05:00Z", event_id="installation-completion-event-2", document_id="evidence-2")
    validate_delivery_execution_payload(second, first, actor, entity_id="delivery-1")

    skipped = apply_completion(second, actor, revision=4, at="2026-09-20T16:06:00Z", event_id="installation-completion-event-4")
    with pytest.raises(DeliveryExecutionRejected, match="completion revision must advance by exactly one"):
        validate_delivery_execution_payload(skipped, second, actor, entity_id="delivery-1")

    stale = principal(session_id="stale-session")
    forged = apply_completion(scheduled, stale)
    with pytest.raises(DeliveryExecutionRejected, match="completion does not match authenticated provenance"):
        validate_delivery_execution_payload(forged, scheduled, actor, entity_id="delivery-1")

    empty = apply_completion(scheduled, actor)
    empty["installationCompletion"]["evidenceReferences"] = []
    with pytest.raises(DeliveryExecutionRejected, match="at least one evidence reference"):
        validate_delivery_execution_payload(empty, scheduled, actor, entity_id="delivery-1")

    duplicate = apply_completion(scheduled, actor)
    duplicate["installationCompletion"]["evidenceReferences"].append({"itemID": "item-1", "documentID": "evidence-1"})
    with pytest.raises(DeliveryExecutionRejected, match="references must be unique"):
        validate_delivery_execution_payload(duplicate, scheduled, actor, entity_id="delivery-1")


def test_scheduled_self_status_event_requires_schedule_or_completion_revision():
    actor = principal()
    scheduled = scheduled_stage(actor)
    no_op = dict(scheduled)
    no_op["updatedAt"] = "2026-09-17T14:21:00Z"
    no_op["events"] = scheduled["events"] + [event(actor, "scheduled-no-op", "scheduled", "scheduled", "2026-09-17T14:21:00Z")]
    with pytest.raises(DeliveryExecutionRejected, match="schedule or completion revision"):
        validate_delivery_execution_payload(no_op, scheduled, actor, entity_id="delivery-1")


def test_installer_assignment_is_validated_and_legacy_schedule_remains_compatible():
    actor = principal()
    current = ready_stage(actor)

    missing = apply_schedule(current, actor)
    missing["installationSchedule"].pop("installerAssignment")
    with pytest.raises(
        DeliveryExecutionRejected,
        match="installer assignment is required for a new schedule revision",
    ):
        validate_delivery_execution_payload(missing, current, actor, entity_id="delivery-1")

    # Simulate an RC3 schedule that was persisted before 0.2.1 existed. The
    # backend must not force a destructive migration just to advance or complete it.
    legacy_ready = apply_schedule(current, actor)
    legacy_ready["installationSchedule"].pop("installerAssignment")
    scheduled_at = "2026-09-17T14:20:00Z"
    legacy_scheduled = dict(legacy_ready)
    legacy_scheduled["status"] = "scheduled"
    legacy_scheduled["updatedAt"] = scheduled_at
    legacy_scheduled["events"] = legacy_ready["events"] + [
        event(
            actor,
            "legacy-delivery-event-scheduled",
            "readyForInstallation",
            "scheduled",
            scheduled_at,
        )
    ]
    validate_delivery_execution_payload(
        legacy_scheduled,
        legacy_ready,
        actor,
        entity_id="delivery-1",
    )
    legacy_completed = apply_completion(
        legacy_scheduled,
        actor,
        event_id="legacy-installation-completion",
    )
    validate_delivery_execution_payload(
        legacy_completed,
        legacy_scheduled,
        actor,
        entity_id="delivery-1",
    )

    malformed = apply_schedule(current, actor)
    malformed["installationSchedule"]["installerAssignment"] = {"id": " ", "displayName": "Crew Alpha"}
    with pytest.raises(DeliveryExecutionRejected, match="installer assignment id is required"):
        validate_delivery_execution_payload(malformed, current, actor, entity_id="delivery-1")

    missing_name = apply_schedule(current, actor)
    missing_name["installationSchedule"]["installerAssignment"] = {"id": "crew-1", "displayName": " "}
    with pytest.raises(DeliveryExecutionRejected, match="installer displayName is required"):
        validate_delivery_execution_payload(missing_name, current, actor, entity_id="delivery-1")

    bad_phone = apply_schedule(current, actor)
    bad_phone["installationSchedule"]["installerAssignment"] = {
        "id": "crew-1",
        "displayName": "Crew Alpha",
        "phone": " ",
    }
    with pytest.raises(DeliveryExecutionRejected, match="installer phone is invalid"):
        validate_delivery_execution_payload(bad_phone, current, actor, entity_id="delivery-1")


def test_installer_assignment_change_requires_audited_schedule_revision():
    actor = principal()
    current = ready_stage(actor)
    first = apply_schedule(current, actor)
    validate_delivery_execution_payload(first, current, actor, entity_id="delivery-1")

    reassigned = apply_schedule(
        first,
        actor,
        revision=2,
        at="2026-09-17T14:16:00Z",
        scheduled_for="2026-09-20T13:00:00Z",
        event_id="installation-schedule-reassigned",
    )
    reassigned["installationSchedule"]["installerAssignment"] = {
        "id": "installer-crew-beta",
        "displayName": "Crew Beta",
        "phone": "+57 300 555 0202",
    }
    validate_delivery_execution_payload(reassigned, first, actor, entity_id="delivery-1")

    removed = apply_schedule(
        first,
        actor,
        revision=2,
        at="2026-09-17T14:16:30Z",
        scheduled_for="2026-09-20T13:00:00Z",
        event_id="installation-schedule-assignment-removed",
    )
    removed["installationSchedule"].pop("installerAssignment")
    with pytest.raises(
        DeliveryExecutionRejected,
        match="installer assignment is required for a new schedule revision",
    ):
        validate_delivery_execution_payload(removed, first, actor, entity_id="delivery-1")


def test_schedule_rejects_invalid_shape_and_production_revision_mismatch():
    actor = principal()
    current = ready_stage(actor)
    missing_date = apply_schedule(current, actor)
    missing_date["installationSchedule"]["scheduledFor"] = "  "
    with pytest.raises(DeliveryExecutionRejected, match="scheduledFor is required"):
        validate_delivery_execution_payload(missing_date, current, actor, entity_id="delivery-1")
    wrong_revision = apply_schedule(current, actor)
    wrong_revision["installationSchedule"]["productionPlanRevision"] = 2
    with pytest.raises(DeliveryExecutionRejected, match="production revision does not match"):
        validate_delivery_execution_payload(wrong_revision, current, actor, entity_id="delivery-1")


def test_installation_self_status_event_requires_real_schedule_revision():
    actor = principal()
    current = ready_stage(actor)
    planned = apply_schedule(current, actor)
    validate_delivery_execution_payload(planned, current, actor, entity_id="delivery-1")
    no_op = dict(planned)
    no_op["updatedAt"] = "2026-09-17T14:16:00Z"
    no_op["events"] = planned["events"] + [event(actor, "installation-schedule-no-op", "readyForInstallation", "readyForInstallation", "2026-09-17T14:16:00Z")]
    with pytest.raises(DeliveryExecutionRejected, match="requires an installation schedule revision"):
        validate_delivery_execution_payload(no_op, planned, actor, entity_id="delivery-1")


def test_schedule_cannot_change_without_audit_event():
    actor = principal()
    current = ready_stage(actor)
    proposed = dict(current)
    proposed["installationSchedule"] = schedule(actor, at=current["updatedAt"])
    with pytest.raises(DeliveryExecutionRejected, match="operational changes must append"):
        validate_delivery_execution_payload(proposed, current, actor, entity_id="delivery-1")


class FakeDB:
    def __init__(self, values: dict[tuple[object, tuple], object]):
        self.values = values

    async def get(self, model, key):
        return self.values.get((model, key))


@pytest.mark.asyncio
async def test_completion_evidence_authority_requires_same_project_canonical_evidence():
    actor = principal()
    scheduled = scheduled_stage(actor)
    completed = apply_completion(scheduled, actor)
    item = CanonicalItem(
        organization_id="org-1",
        item_id="item-1",
        project_id="project-1",
        project_sector_id=None,
        deleted_at=None,
    )
    evidence = CanonicalItemChild(
        organization_id="org-1",
        entity_type="evidence",
        entity_id="evidence-1",
        item_id="item-1",
        deleted_at=None,
    )
    db = FakeDB({
        (CanonicalItem, ("org-1", "item-1")): item,
        (CanonicalItemChild, ("org-1", "evidence", "evidence-1")): evidence,
    })
    await validate_installation_evidence_authority(
        db,
        actor,
        completed,
        scheduled,
        project_id="project-1",
    )

    wrong_item = CanonicalItem(
        organization_id="org-1",
        item_id="item-1",
        project_id="project-other",
        project_sector_id=None,
        deleted_at=None,
    )
    bad_db = FakeDB({
        (CanonicalItem, ("org-1", "item-1")): wrong_item,
        (CanonicalItemChild, ("org-1", "evidence", "evidence-1")): evidence,
    })
    with pytest.raises(DeliveryInstallationRejected, match="not active in the delivery Project"):
        await validate_installation_evidence_authority(
            bad_db,
            actor,
            completed,
            scheduled,
            project_id="project-1",
        )
