from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.models import CanonicalItem, CanonicalItemChild


class DeliveryInstallationRejected(Exception):
    """An installation schedule/completion/closeout mutation violates delivery execution policy."""


INSTALLATION_EDITABLE_STATUSES = frozenset({"readyForInstallation", "scheduled"})
INSTALLATION_FROZEN_STATUSES = frozenset({"complete"})


def _normalized(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _require_provenance(
    value: dict,
    principal: Principal,
    *,
    current_actor: bool = False,
    label: str,
) -> None:
    actor_id = _normalized(value.get("actorID"))
    organization_id = _normalized(value.get("organizationID"))
    membership_id = _normalized(value.get("membershipID"))
    session_id = _normalized(value.get("sessionID"))
    revision = value.get("authorizationRevision")
    if (
        actor_id is None
        or organization_id is None
        or membership_id is None
        or session_id is None
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise DeliveryInstallationRejected(
            f"delivery installation {label} provenance is incomplete"
        )
    if organization_id != principal.organization_id:
        raise DeliveryInstallationRejected(
            f"delivery installation {label} organization is invalid"
        )
    if current_actor and (
        actor_id != principal.user_id
        or membership_id != principal.membership_id
        or session_id != principal.session_id
        or revision != principal.authorization_revision
    ):
        raise DeliveryInstallationRejected(
            f"delivery installation {label} does not match authenticated provenance"
        )


def _validate_installer_assignment(value: object) -> dict | None:
    if value is None:
        # 0.2.0 schedules did not carry installer assignment. Preserve read/forward
        # compatibility while 0.2.1 clients require assignment when authoring a revision.
        return None
    if not isinstance(value, dict):
        raise DeliveryInstallationRejected(
            "delivery installation installer assignment is malformed"
        )
    if _normalized(value.get("id")) is None:
        raise DeliveryInstallationRejected(
            "delivery installation installer assignment id is required"
        )
    if _normalized(value.get("displayName")) is None:
        raise DeliveryInstallationRejected(
            "delivery installation installer displayName is required"
        )
    phone = value.get("phone")
    if phone is not None and _normalized(phone) is None:
        raise DeliveryInstallationRejected(
            "delivery installation installer phone is invalid"
        )
    return value


def _require_installer_assignment_for_revision(schedule: dict) -> None:
    if schedule.get("installerAssignment") is None:
        raise DeliveryInstallationRejected(
            "delivery installation installer assignment is required for a new schedule revision"
        )


def _production_ready(production_plan: object) -> bool:
    if not isinstance(production_plan, dict):
        return False
    steps = production_plan.get("steps")
    return (
        isinstance(steps, list)
        and bool(steps)
        and all(
            isinstance(step, dict) and step.get("status") == "complete"
            for step in steps
        )
    )


def _validate_schedule(
    schedule: object,
    production_plan: object,
    principal: Principal,
) -> dict | None:
    if schedule is None:
        return None
    if not isinstance(schedule, dict):
        raise DeliveryInstallationRejected(
            "delivery installation schedule is malformed"
        )
    if not isinstance(production_plan, dict):
        raise DeliveryInstallationRejected(
            "delivery installation schedule requires a frozen production plan"
        )
    if not _production_ready(production_plan):
        raise DeliveryInstallationRejected(
            "delivery installation schedule requires completed Production work"
        )

    revision = schedule.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DeliveryInstallationRejected(
            "delivery installation schedule revision is invalid"
        )
    production_revision = schedule.get("productionPlanRevision")
    expected_production_revision = production_plan.get("revision")
    if (
        not isinstance(production_revision, int)
        or isinstance(production_revision, bool)
        or production_revision < 1
        or production_revision != expected_production_revision
    ):
        raise DeliveryInstallationRejected(
            "delivery installation schedule production revision does not match frozen Production"
        )
    if _normalized(schedule.get("scheduledFor")) is None:
        raise DeliveryInstallationRejected(
            "delivery installation scheduledFor is required"
        )
    if _normalized(schedule.get("updatedAt")) is None:
        raise DeliveryInstallationRejected(
            "delivery installation schedule updatedAt is required"
        )
    _validate_installer_assignment(schedule.get("installerAssignment"))
    note = schedule.get("note")
    if note is not None and not isinstance(note, str):
        raise DeliveryInstallationRejected(
            "delivery installation schedule note is invalid"
        )

    _require_provenance(schedule, principal, label="schedule")
    return schedule


def _validated_references(
    references: object,
    *,
    label: str,
    require_nonempty: bool,
) -> list[dict]:
    if not isinstance(references, list) or (require_nonempty and not references):
        suffix = " requires at least one evidence reference" if require_nonempty else " evidence references are malformed"
        raise DeliveryInstallationRejected(f"delivery installation {label}{suffix}")
    seen: set[tuple[str, str]] = set()
    normalized_references: list[dict] = []
    for reference in references:
        if not isinstance(reference, dict):
            raise DeliveryInstallationRejected(
                f"delivery installation {label} evidence reference is malformed"
            )
        item_id = _normalized(reference.get("itemID"))
        document_id = _normalized(reference.get("documentID"))
        if item_id is None or document_id is None:
            raise DeliveryInstallationRejected(
                f"delivery installation {label} evidence reference requires itemID and documentID"
            )
        key = (item_id, document_id)
        if key in seen:
            raise DeliveryInstallationRejected(
                f"delivery installation {label} evidence references must be unique"
            )
        seen.add(key)
        normalized_references.append(reference)
    return normalized_references


def _validate_completion(
    completion: object,
    schedule: dict | None,
    principal: Principal,
) -> dict | None:
    if completion is None:
        return None
    if not isinstance(completion, dict):
        raise DeliveryInstallationRejected(
            "delivery installation completion is malformed"
        )
    if schedule is None:
        raise DeliveryInstallationRejected(
            "delivery installation completion requires a saved installation schedule"
        )

    revision = completion.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DeliveryInstallationRejected(
            "delivery installation completion revision is invalid"
        )
    schedule_revision = completion.get("installationScheduleRevision")
    if (
        not isinstance(schedule_revision, int)
        or isinstance(schedule_revision, bool)
        or schedule_revision < 1
        or schedule_revision != schedule.get("revision")
    ):
        raise DeliveryInstallationRejected(
            "delivery installation completion schedule revision does not match saved schedule"
        )
    if _normalized(completion.get("completedAt")) is None:
        raise DeliveryInstallationRejected(
            "delivery installation completion completedAt is required"
        )
    if _normalized(completion.get("updatedAt")) is None:
        raise DeliveryInstallationRejected(
            "delivery installation completion updatedAt is required"
        )
    note = completion.get("note")
    if note is not None and not isinstance(note, str):
        raise DeliveryInstallationRejected(
            "delivery installation completion note is invalid"
        )

    _validated_references(
        completion.get("evidenceReferences"),
        label="completion",
        require_nonempty=True,
    )
    _require_provenance(completion, principal, label="completion")
    return completion


def _validate_closeout(
    closeout: object,
    completion: dict | None,
    principal: Principal,
) -> dict | None:
    if closeout is None:
        return None
    if not isinstance(closeout, dict):
        raise DeliveryInstallationRejected("delivery closeout is malformed")
    if completion is None:
        raise DeliveryInstallationRejected(
            "delivery closeout requires recorded installation completion"
        )
    revision = closeout.get("revision")
    if revision != 1 or isinstance(revision, bool):
        raise DeliveryInstallationRejected(
            "delivery closeout revision must be exactly one"
        )
    completion_revision = closeout.get("installationCompletionRevision")
    if (
        not isinstance(completion_revision, int)
        or isinstance(completion_revision, bool)
        or completion_revision != completion.get("revision")
    ):
        raise DeliveryInstallationRejected(
            "delivery closeout completion revision does not match installed completion"
        )
    if _normalized(closeout.get("closedAt")) is None:
        raise DeliveryInstallationRejected("delivery closeout closedAt is required")
    if _normalized(closeout.get("updatedAt")) is None:
        raise DeliveryInstallationRejected("delivery closeout updatedAt is required")
    note = closeout.get("note")
    if note is not None and not isinstance(note, str):
        raise DeliveryInstallationRejected("delivery closeout note is invalid")
    references = _validated_references(
        closeout.get("evidenceReferences"),
        label="closeout",
        require_nonempty=False,
    )
    if not references and _normalized(note) is None:
        raise DeliveryInstallationRejected(
            "delivery closeout requires a final note or at least one evidence reference"
        )
    _require_provenance(closeout, principal, label="closeout")
    return closeout


def validate_installation_handoff(
    payload: dict,
    current_payload: dict | None,
    principal: Principal,
    *,
    status: str,
) -> None:
    production_plan = payload.get("productionPlan")
    schedule = _validate_schedule(
        payload.get("installationSchedule"),
        production_plan,
        principal,
    )

    if current_payload is not None and current_payload.get("status") == "scheduled":
        raw_current_completion = current_payload.get("installationCompletion")
        if raw_current_completion is not None:
            current_schedule_for_freeze = _validate_schedule(
                current_payload.get("installationSchedule"),
                current_payload.get("productionPlan"),
                principal,
            )
            if schedule != current_schedule_for_freeze:
                raise DeliveryInstallationRejected(
                    "delivery installation schedule is frozen once completion evidence is recorded"
                )

    completion = _validate_completion(
        payload.get("installationCompletion"),
        schedule,
        principal,
    )
    closeout = _validate_closeout(
        payload.get("deliveryCloseout"),
        completion,
        principal,
    )

    if status in {
        "engineeringReview",
        "materialsRequired",
        "procurement",
        "production",
    } and schedule is not None:
        raise DeliveryInstallationRejected(
            "delivery installation schedule can only be authored after Production is complete"
        )
    if status in {"scheduled", "installed", "complete"} and schedule is None:
        raise DeliveryInstallationRejected(
            "save delivery installation schedule before moving work to Scheduled"
        )
    if status in {
        "engineeringReview",
        "materialsRequired",
        "procurement",
        "production",
        "readyForInstallation",
    } and completion is not None:
        raise DeliveryInstallationRejected(
            "delivery installation completion can only be authored while Scheduled"
        )
    if status in {"installed", "complete"} and completion is None:
        raise DeliveryInstallationRejected(
            "save installation completion evidence before marking delivery Installed"
        )
    if status != "complete" and closeout is not None:
        raise DeliveryInstallationRejected(
            "delivery closeout can only be recorded with the Complete transition"
        )
    if status == "complete" and closeout is None:
        raise DeliveryInstallationRejected(
            "record delivery closeout before marking delivery Complete"
        )
    if current_payload is None:
        return

    current_status = current_payload.get("status")
    current_production_plan = current_payload.get("productionPlan")
    current_schedule = _validate_schedule(
        current_payload.get("installationSchedule"),
        current_production_plan,
        principal,
    )
    current_completion = _validate_completion(
        current_payload.get("installationCompletion"),
        current_schedule,
        principal,
    )
    current_closeout = _validate_closeout(
        current_payload.get("deliveryCloseout"),
        current_completion,
        principal,
    )

    if current_status in INSTALLATION_FROZEN_STATUSES:
        if schedule != current_schedule:
            raise DeliveryInstallationRejected(
                "delivery installation schedule is immutable after installation is recorded"
            )
        if completion != current_completion:
            raise DeliveryInstallationRejected(
                "delivery installation completion is immutable after installation is recorded"
            )
        if closeout != current_closeout:
            raise DeliveryInstallationRejected(
                "delivery closeout is immutable after delivery is Complete"
            )
        return

    if current_status == "installed":
        if schedule != current_schedule:
            raise DeliveryInstallationRejected(
                "delivery installation schedule is immutable after installation is recorded"
            )
        if completion != current_completion:
            raise DeliveryInstallationRejected(
                "delivery installation completion is immutable after installation is recorded"
            )
        if status == "complete":
            if current_closeout is not None:
                raise DeliveryInstallationRejected(
                    "delivery closeout is already recorded"
                )
            if closeout is None:
                raise DeliveryInstallationRejected(
                    "record delivery closeout before marking delivery Complete"
                )
            if closeout.get("updatedAt") != payload.get("updatedAt"):
                raise DeliveryInstallationRejected(
                    "delivery closeout updatedAt must match delivery execution updatedAt"
                )
            _require_provenance(closeout, principal, current_actor=True, label="closeout")
            return
        if closeout != current_closeout:
            raise DeliveryInstallationRejected(
                "delivery closeout can only be recorded with the Complete transition"
            )
        return

    if current_status not in INSTALLATION_EDITABLE_STATUSES:
        if schedule is not None:
            raise DeliveryInstallationRejected(
                "enter Ready for Installation before authoring an installation schedule"
            )
        if completion is not None:
            raise DeliveryInstallationRejected(
                "enter Scheduled before authoring installation completion"
            )
        return

    if current_status == "readyForInstallation":
        if completion is not None:
            raise DeliveryInstallationRejected(
                "enter Scheduled before authoring installation completion"
            )
        if status == "scheduled":
            if current_schedule is None:
                raise DeliveryInstallationRejected(
                    "save delivery installation schedule before moving work to Scheduled"
                )
            if schedule != current_schedule:
                raise DeliveryInstallationRejected(
                    "delivery installation schedule must be saved before the Scheduled transition"
                )
            return
        if status != "readyForInstallation" or schedule == current_schedule:
            return
        if schedule is None:
            raise DeliveryInstallationRejected(
                "delivery installation schedule cannot be cleared"
            )
        expected_revision = 1 if current_schedule is None else current_schedule["revision"] + 1
        if schedule["revision"] != expected_revision:
            raise DeliveryInstallationRejected(
                "delivery installation schedule revision must advance by exactly one"
            )
        if schedule.get("updatedAt") != payload.get("updatedAt"):
            raise DeliveryInstallationRejected(
                "delivery installation schedule updatedAt must match delivery execution updatedAt"
            )
        _require_installer_assignment_for_revision(schedule)
        _require_provenance(schedule, principal, current_actor=True, label="schedule")
        return

    if status == "installed":
        if current_schedule is None:
            raise DeliveryInstallationRejected(
                "delivery installation schedule is required before recording installation"
            )
        if current_completion is None:
            raise DeliveryInstallationRejected(
                "save installation completion evidence before marking delivery Installed"
            )
        if schedule != current_schedule:
            raise DeliveryInstallationRejected(
                "delivery installation schedule must be saved before recording installation"
            )
        if completion != current_completion:
            raise DeliveryInstallationRejected(
                "delivery installation completion must be saved before recording installation"
            )
        return

    if status != "scheduled":
        return

    schedule_changed = schedule != current_schedule
    completion_changed = completion != current_completion
    if schedule_changed and completion_changed:
        raise DeliveryInstallationRejected(
            "delivery installation may revise either schedule or completion per mutation, not both"
        )

    if schedule_changed:
        if current_completion is not None:
            raise DeliveryInstallationRejected(
                "delivery installation schedule is frozen once completion evidence is recorded"
            )
        if schedule is None:
            raise DeliveryInstallationRejected(
                "delivery installation schedule cannot be cleared"
            )
        expected_revision = 1 if current_schedule is None else current_schedule["revision"] + 1
        if schedule["revision"] != expected_revision:
            raise DeliveryInstallationRejected(
                "delivery installation schedule revision must advance by exactly one"
            )
        if schedule.get("updatedAt") != payload.get("updatedAt"):
            raise DeliveryInstallationRejected(
                "delivery installation schedule updatedAt must match delivery execution updatedAt"
            )
        _require_installer_assignment_for_revision(schedule)
        _require_provenance(schedule, principal, current_actor=True, label="schedule")
        return

    if completion_changed:
        if completion is None:
            raise DeliveryInstallationRejected(
                "delivery installation completion cannot be cleared"
            )
        expected_revision = 1 if current_completion is None else current_completion["revision"] + 1
        if completion["revision"] != expected_revision:
            raise DeliveryInstallationRejected(
                "delivery installation completion revision must advance by exactly one"
            )
        if completion.get("updatedAt") != payload.get("updatedAt"):
            raise DeliveryInstallationRejected(
                "delivery installation completion updatedAt must match delivery execution updatedAt"
            )
        _require_provenance(completion, principal, current_actor=True, label="completion")


async def _validate_evidence_authority(
    db: AsyncSession,
    principal: Principal,
    references: list[dict],
    *,
    project_id: str,
    label: str,
) -> None:
    for reference in references:
        if not isinstance(reference, dict):
            raise DeliveryInstallationRejected(
                f"delivery installation {label} evidence reference is malformed"
            )
        item_id = _normalized(reference.get("itemID"))
        document_id = _normalized(reference.get("documentID"))
        if item_id is None or document_id is None:
            raise DeliveryInstallationRejected(
                f"delivery installation {label} evidence reference requires itemID and documentID"
            )

        item = await db.get(CanonicalItem, (principal.organization_id, item_id))
        if item is None or item.deleted_at is not None or item.project_id != project_id:
            raise DeliveryInstallationRejected(
                f"delivery installation {label} evidence Item is not active in the delivery Project"
            )
        evidence = await db.get(
            CanonicalItemChild,
            (principal.organization_id, "evidence", document_id),
        )
        if (
            evidence is None
            or evidence.deleted_at is not None
            or evidence.item_id != item_id
        ):
            raise DeliveryInstallationRejected(
                f"delivery installation {label} evidence is not canonical for the referenced Item"
            )


async def validate_installation_evidence_authority(
    db: AsyncSession,
    principal: Principal,
    payload: dict,
    current_payload: dict | None,
    *,
    project_id: str,
) -> None:
    completion = payload.get("installationCompletion")
    current_completion = (
        current_payload.get("installationCompletion")
        if isinstance(current_payload, dict)
        else None
    )
    if completion is not None and completion != current_completion:
        if not isinstance(completion, dict):
            raise DeliveryInstallationRejected(
                "delivery installation completion is malformed"
            )
        await _validate_evidence_authority(
            db,
            principal,
            completion.get("evidenceReferences", []),
            project_id=project_id,
            label="completion",
        )

    closeout = payload.get("deliveryCloseout")
    current_closeout = (
        current_payload.get("deliveryCloseout")
        if isinstance(current_payload, dict)
        else None
    )
    if closeout is not None and closeout != current_closeout:
        if not isinstance(closeout, dict):
            raise DeliveryInstallationRejected("delivery closeout is malformed")
        await _validate_evidence_authority(
            db,
            principal,
            closeout.get("evidenceReferences", []),
            project_id=project_id,
            label="closeout",
        )
