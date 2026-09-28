from __future__ import annotations

from app.auth import Principal


class DeliveryInstallationRejected(Exception):
    """An installation schedule mutation violates delivery execution policy."""


INSTALLATION_EDITABLE_STATUSES = frozenset({"readyForInstallation", "scheduled"})
INSTALLATION_FROZEN_STATUSES = frozenset({"installed", "complete"})


def _normalized(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _require_provenance(
    schedule: dict,
    principal: Principal,
    *,
    current_actor: bool = False,
) -> None:
    actor_id = _normalized(schedule.get("actorID"))
    organization_id = _normalized(schedule.get("organizationID"))
    membership_id = _normalized(schedule.get("membershipID"))
    session_id = _normalized(schedule.get("sessionID"))
    revision = schedule.get("authorizationRevision")
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
            "delivery installation schedule provenance is incomplete"
        )
    if organization_id != principal.organization_id:
        raise DeliveryInstallationRejected(
            "delivery installation schedule organization is invalid"
        )
    if current_actor and (
        actor_id != principal.user_id
        or membership_id != principal.membership_id
        or session_id != principal.session_id
        or revision != principal.authorization_revision
    ):
        raise DeliveryInstallationRejected(
            "delivery installation schedule does not match authenticated provenance"
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
    note = schedule.get("note")
    if note is not None and not isinstance(note, str):
        raise DeliveryInstallationRejected(
            "delivery installation schedule note is invalid"
        )

    _require_provenance(schedule, principal)
    return schedule


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
    if current_payload is None:
        return

    current_status = current_payload.get("status")
    current_production_plan = current_payload.get("productionPlan")
    current_schedule = _validate_schedule(
        current_payload.get("installationSchedule"),
        current_production_plan,
        principal,
    )

    if current_status in INSTALLATION_FROZEN_STATUSES:
        if schedule != current_schedule:
            raise DeliveryInstallationRejected(
                "delivery installation schedule is immutable after installation is recorded"
            )
        return

    if current_status not in INSTALLATION_EDITABLE_STATUSES:
        if schedule is not None:
            raise DeliveryInstallationRejected(
                "enter Ready for Installation before authoring an installation schedule"
            )
        return

    if current_status == "readyForInstallation" and status == "scheduled":
        if current_schedule is None:
            raise DeliveryInstallationRejected(
                "save delivery installation schedule before moving work to Scheduled"
            )
        if schedule != current_schedule:
            raise DeliveryInstallationRejected(
                "delivery installation schedule must be saved before the Scheduled transition"
            )
        return

    if current_status == "scheduled" and status == "installed":
        if current_schedule is None:
            raise DeliveryInstallationRejected(
                "delivery installation schedule is required before recording installation"
            )
        if schedule != current_schedule:
            raise DeliveryInstallationRejected(
                "delivery installation schedule must be saved before recording installation"
            )
        return

    if status != current_status or schedule == current_schedule:
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
    _require_provenance(schedule, principal, current_actor=True)
