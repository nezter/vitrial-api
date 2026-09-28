from __future__ import annotations

from app.auth import Principal


class DeliveryProductionRejected(Exception):
    """A Production plan mutation violates delivery execution policy."""


PRODUCTION_FROZEN_STATUSES = frozenset({
    "readyForInstallation",
    "scheduled",
    "installed",
    "complete",
})
PRODUCTION_STEP_STATUSES = frozenset({"pending", "inProgress", "complete"})


def _normalized(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _require_provenance(
    plan: dict,
    principal: Principal,
    *,
    current_actor: bool = False,
) -> None:
    actor_id = _normalized(plan.get("actorID"))
    organization_id = _normalized(plan.get("organizationID"))
    membership_id = _normalized(plan.get("membershipID"))
    session_id = _normalized(plan.get("sessionID"))
    revision = plan.get("authorizationRevision")
    if (
        actor_id is None
        or organization_id is None
        or membership_id is None
        or session_id is None
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise DeliveryProductionRejected("delivery production plan provenance is incomplete")
    if organization_id != principal.organization_id:
        raise DeliveryProductionRejected("delivery production plan organization is invalid")
    if current_actor and (
        actor_id != principal.user_id
        or membership_id != principal.membership_id
        or session_id != principal.session_id
        or revision != principal.authorization_revision
    ):
        raise DeliveryProductionRejected(
            "delivery production plan does not match authenticated provenance"
        )


def _validate_plan(
    plan: object,
    procurement_plan: object,
    principal: Principal,
) -> dict | None:
    if plan is None:
        return None
    if not isinstance(plan, dict):
        raise DeliveryProductionRejected("delivery production plan is malformed")
    if not isinstance(procurement_plan, dict):
        raise DeliveryProductionRejected(
            "delivery production plan requires a frozen procurement plan"
        )

    revision = plan.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DeliveryProductionRejected("delivery production plan revision is invalid")
    procurement_revision = plan.get("procurementPlanRevision")
    expected_procurement_revision = procurement_plan.get("revision")
    if (
        not isinstance(procurement_revision, int)
        or isinstance(procurement_revision, bool)
        or procurement_revision < 1
        or procurement_revision != expected_procurement_revision
    ):
        raise DeliveryProductionRejected(
            "delivery production plan procurement revision does not match frozen procurement"
        )
    if _normalized(plan.get("updatedAt")) is None:
        raise DeliveryProductionRejected("delivery production plan updatedAt is required")

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise DeliveryProductionRejected(
            "delivery production plan requires at least one production step"
        )

    seen_ids: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise DeliveryProductionRejected("delivery production step is malformed")
        step_id = _normalized(step.get("id"))
        if step_id is None:
            raise DeliveryProductionRejected("delivery production step id is required")
        if step_id in seen_ids:
            raise DeliveryProductionRejected("delivery production step ids must be unique")
        seen_ids.add(step_id)
        if _normalized(step.get("description")) is None:
            raise DeliveryProductionRejected("delivery production step description is required")
        if step.get("status") not in PRODUCTION_STEP_STATUSES:
            raise DeliveryProductionRejected("delivery production step status is invalid")
        note = step.get("note")
        if note is not None and not isinstance(note, str):
            raise DeliveryProductionRejected("delivery production step note is invalid")

    _require_provenance(plan, principal)
    return plan


def _require_installation_ready(plan: dict | None) -> None:
    if plan is None:
        raise DeliveryProductionRejected(
            "save delivery production work before marking Ready for Installation"
        )
    blocked = [
        step.get("id")
        for step in plan.get("steps", [])
        if step.get("status") != "complete"
    ]
    if blocked:
        raise DeliveryProductionRejected(
            "delivery Ready for Installation is blocked until every production step is complete"
        )


def validate_production_handoff(
    payload: dict,
    current_payload: dict | None,
    principal: Principal,
    *,
    status: str,
) -> None:
    procurement_plan = payload.get("procurementPlan")
    plan = _validate_plan(payload.get("productionPlan"), procurement_plan, principal)

    if status in {"engineeringReview", "materialsRequired", "procurement"} and plan is not None:
        raise DeliveryProductionRejected(
            "delivery production plan can only be authored during Production"
        )
    if status in PRODUCTION_FROZEN_STATUSES:
        _require_installation_ready(plan)
    if current_payload is None:
        return

    current_status = current_payload.get("status")
    current_procurement_plan = current_payload.get("procurementPlan")
    current_plan = _validate_plan(
        current_payload.get("productionPlan"),
        current_procurement_plan,
        principal,
    )

    if current_status in PRODUCTION_FROZEN_STATUSES:
        if plan != current_plan:
            raise DeliveryProductionRejected(
                "delivery production plan is immutable after Ready for Installation"
            )
        return

    if current_status in {"engineeringReview", "materialsRequired", "procurement"}:
        if plan is not None:
            raise DeliveryProductionRejected(
                "enter Production before authoring delivery production work"
            )
        return

    if current_status != "production":
        return

    if status == "readyForInstallation":
        if current_plan is None:
            raise DeliveryProductionRejected(
                "save delivery production work before marking Ready for Installation"
            )
        if plan != current_plan:
            raise DeliveryProductionRejected(
                "delivery production plan must be saved before the Ready for Installation transition"
            )
        _require_installation_ready(plan)
        return

    if status != "production" or plan == current_plan:
        return
    if plan is None:
        raise DeliveryProductionRejected("delivery production plan cannot be cleared")

    expected_revision = 1 if current_plan is None else current_plan["revision"] + 1
    if plan["revision"] != expected_revision:
        raise DeliveryProductionRejected(
            "delivery production plan revision must advance by exactly one"
        )
    if plan.get("updatedAt") != payload.get("updatedAt"):
        raise DeliveryProductionRejected(
            "delivery production plan updatedAt must match delivery execution updatedAt"
        )
    _require_provenance(plan, principal, current_actor=True)
