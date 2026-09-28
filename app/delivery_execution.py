from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.models import CanonicalProject, CanonicalProjectChild, SyncEntity
from app.ownership import EffectiveScope


class DeliveryExecutionRejected(Exception):
    """A delivery execution mutation violates canonical authority or lifecycle rules."""


DELIVERY_ENTITY_TYPE = "delivery_execution"
DELIVERY_CAPABILITY = "delivery.manage"
DELIVERY_STATUSES = (
    "engineeringReview",
    "materialsRequired",
    "procurement",
    "production",
    "readyForInstallation",
    "scheduled",
    "installed",
    "complete",
)
DELIVERY_TRANSITIONS = {
    "engineeringReview": "materialsRequired",
    "materialsRequired": "procurement",
    "procurement": "production",
    "production": "readyForInstallation",
    "readyForInstallation": "scheduled",
    "scheduled": "installed",
    "installed": "complete",
}
MATERIALS_FROZEN_STATUSES = frozenset({
    "procurement",
    "production",
    "readyForInstallation",
    "scheduled",
    "installed",
    "complete",
})
PROCUREMENT_FROZEN_STATUSES = frozenset({
    "production",
    "readyForInstallation",
    "scheduled",
    "installed",
    "complete",
})
PROCUREMENT_REQUIREMENT_STATUSES = frozenset({
    "unconfirmed",
    "ordered",
    "shortage",
    "available",
})
IMMUTABLE_HANDOFF_KEYS = (
    "id",
    "quotationID",
    "quotationNumber",
    "quotationRevision",
    "projectID",
    "customerID",
    "currencyCode",
    "quotedTotal",
    "pricingVersion",
    "startedAt",
)


def _normalized(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise DeliveryExecutionRejected(f"{name} is invalid")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DeliveryExecutionRejected(f"{name} is invalid") from exc


def _quotation_revision(payload: dict) -> int:
    revision = payload.get("revision")
    if revision is None:
        return 1
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DeliveryExecutionRejected("canonical quotation revision is invalid")
    return revision


def _quotation_total(payload: dict) -> Decimal:
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        raise DeliveryExecutionRejected("canonical quotation lines are unavailable")
    total = Decimal(0)
    for line in lines:
        if not isinstance(line, dict):
            raise DeliveryExecutionRejected("canonical quotation line is malformed")
        total += _decimal(line.get("totalPrice"), name="canonical quotation totalPrice")
    return total


def _events(payload: dict) -> list[dict]:
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise DeliveryExecutionRejected("delivery execution history is required")
    if not all(isinstance(event, dict) for event in events):
        raise DeliveryExecutionRejected("delivery execution history is malformed")
    return events


def _require_unique_event_ids(events: list[dict]) -> None:
    seen: set[str] = set()
    for event in events:
        event_id = _normalized(event.get("id"))
        if event_id is None:
            raise DeliveryExecutionRejected("delivery execution event id is required")
        if event_id in seen:
            raise DeliveryExecutionRejected("delivery execution event ids must be unique")
        seen.add(event_id)


def _require_event_provenance(
    event: dict,
    principal: Principal,
    *,
    current_actor: bool = False,
) -> None:
    actor_id = _normalized(event.get("actorID"))
    organization_id = _normalized(event.get("organizationID"))
    membership_id = _normalized(event.get("membershipID"))
    session_id = _normalized(event.get("sessionID"))
    revision = event.get("authorizationRevision")
    if (
        actor_id is None
        or organization_id is None
        or membership_id is None
        or session_id is None
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise DeliveryExecutionRejected("delivery execution event provenance is incomplete")
    if organization_id != principal.organization_id:
        raise DeliveryExecutionRejected("delivery execution event organization is invalid")
    if current_actor and (
        actor_id != principal.user_id
        or membership_id != principal.membership_id
        or session_id != principal.session_id
        or revision != principal.authorization_revision
    ):
        raise DeliveryExecutionRejected(
            "delivery execution latest event does not match authenticated provenance"
        )


def _require_materials_plan_provenance(
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
        raise DeliveryExecutionRejected("delivery materials plan provenance is incomplete")
    if organization_id != principal.organization_id:
        raise DeliveryExecutionRejected("delivery materials plan organization is invalid")
    if current_actor and (
        actor_id != principal.user_id
        or membership_id != principal.membership_id
        or session_id != principal.session_id
        or revision != principal.authorization_revision
    ):
        raise DeliveryExecutionRejected(
            "delivery materials plan does not match authenticated provenance"
        )


def _require_procurement_plan_provenance(
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
        raise DeliveryExecutionRejected("delivery procurement plan provenance is incomplete")
    if organization_id != principal.organization_id:
        raise DeliveryExecutionRejected("delivery procurement plan organization is invalid")
    if current_actor and (
        actor_id != principal.user_id
        or membership_id != principal.membership_id
        or session_id != principal.session_id
        or revision != principal.authorization_revision
    ):
        raise DeliveryExecutionRejected(
            "delivery procurement plan does not match authenticated provenance"
        )


def _validate_materials_plan(plan: object, principal: Principal) -> dict | None:
    if plan is None:
        return None
    if not isinstance(plan, dict):
        raise DeliveryExecutionRejected("delivery materials plan is malformed")

    revision = plan.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DeliveryExecutionRejected("delivery materials plan revision is invalid")
    if _normalized(plan.get("updatedAt")) is None:
        raise DeliveryExecutionRejected("delivery materials plan updatedAt is required")

    requirements = plan.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise DeliveryExecutionRejected("delivery materials plan requires at least one material")

    seen_ids: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise DeliveryExecutionRejected("delivery material requirement is malformed")
        requirement_id = _normalized(requirement.get("id"))
        if requirement_id is None:
            raise DeliveryExecutionRejected("delivery material requirement id is required")
        if requirement_id in seen_ids:
            raise DeliveryExecutionRejected("delivery material requirement ids must be unique")
        seen_ids.add(requirement_id)

        if _normalized(requirement.get("description")) is None:
            raise DeliveryExecutionRejected("delivery material description is required")
        if _normalized(requirement.get("unit")) is None:
            raise DeliveryExecutionRejected("delivery material unit is required")
        if _decimal(requirement.get("quantity"), name="delivery material quantity") <= 0:
            raise DeliveryExecutionRejected("delivery material quantity must be greater than zero")
        note = requirement.get("note")
        if note is not None and not isinstance(note, str):
            raise DeliveryExecutionRejected("delivery material note is invalid")

    _require_materials_plan_provenance(plan, principal)
    return plan


def _validate_procurement_plan(
    plan: object,
    materials_plan: dict | None,
    principal: Principal,
) -> dict | None:
    if plan is None:
        return None
    if not isinstance(plan, dict):
        raise DeliveryExecutionRejected("delivery procurement plan is malformed")
    if materials_plan is None:
        raise DeliveryExecutionRejected(
            "delivery procurement plan requires a frozen materials plan"
        )

    revision = plan.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DeliveryExecutionRejected("delivery procurement plan revision is invalid")
    materials_revision = plan.get("materialsPlanRevision")
    if (
        not isinstance(materials_revision, int)
        or isinstance(materials_revision, bool)
        or materials_revision < 1
        or materials_revision != materials_plan.get("revision")
    ):
        raise DeliveryExecutionRejected(
            "delivery procurement plan materials revision does not match frozen materials"
        )
    if _normalized(plan.get("updatedAt")) is None:
        raise DeliveryExecutionRejected("delivery procurement plan updatedAt is required")

    material_requirements = materials_plan.get("requirements") or []
    material_by_id = {
        _normalized(requirement.get("id")): requirement
        for requirement in material_requirements
        if isinstance(requirement, dict) and _normalized(requirement.get("id")) is not None
    }
    requirements = plan.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise DeliveryExecutionRejected(
            "delivery procurement plan requires every frozen material"
        )

    seen_ids: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise DeliveryExecutionRejected("delivery procurement requirement is malformed")
        requirement_id = _normalized(requirement.get("requirementID"))
        if requirement_id is None:
            raise DeliveryExecutionRejected("delivery procurement requirement id is required")
        if requirement_id in seen_ids:
            raise DeliveryExecutionRejected(
                "delivery procurement requirement ids must be unique"
            )
        seen_ids.add(requirement_id)
        material = material_by_id.get(requirement_id)
        if material is None:
            raise DeliveryExecutionRejected(
                "delivery procurement requirement references unknown frozen material"
            )

        status = requirement.get("status")
        if status not in PROCUREMENT_REQUIREMENT_STATUSES:
            raise DeliveryExecutionRejected("delivery procurement requirement status is invalid")

        lead_time = requirement.get("leadTimeDays")
        if lead_time is not None and (
            not isinstance(lead_time, int)
            or isinstance(lead_time, bool)
            or lead_time < 0
        ):
            raise DeliveryExecutionRejected(
                "delivery procurement requirement lead time is invalid"
            )
        expected_available_at = requirement.get("expectedAvailableAt")
        if expected_available_at is not None and _normalized(expected_available_at) is None:
            raise DeliveryExecutionRejected(
                "delivery procurement expected availability is invalid"
            )
        note = requirement.get("note")
        if note is not None and not isinstance(note, str):
            raise DeliveryExecutionRejected("delivery procurement requirement note is invalid")

        shortage_quantity = requirement.get("shortageQuantity")
        if status == "shortage":
            shortage = _decimal(
                shortage_quantity,
                name="delivery procurement shortage quantity",
            )
            required = _decimal(
                material.get("quantity"),
                name="delivery material quantity",
            )
            if shortage <= 0 or shortage > required:
                raise DeliveryExecutionRejected(
                    "delivery procurement shortage quantity is invalid"
                )
        elif shortage_quantity is not None:
            raise DeliveryExecutionRejected(
                "delivery procurement shortage quantity is only valid for shortages"
            )

    expected_ids = set(material_by_id)
    if seen_ids != expected_ids:
        missing = sorted(expected_ids - seen_ids)
        if missing:
            raise DeliveryExecutionRejected(
                "delivery procurement plan is missing frozen material requirements"
            )
        raise DeliveryExecutionRejected(
            "delivery procurement plan requirements do not match frozen materials"
        )

    _require_procurement_plan_provenance(plan, principal)
    return plan


def _validate_materials_handoff(
    payload: dict,
    current_payload: dict | None,
    principal: Principal,
    *,
    status: str,
) -> None:
    plan = _validate_materials_plan(payload.get("materialsPlan"), principal)

    if status == "engineeringReview" and plan is not None:
        raise DeliveryExecutionRejected(
            "delivery materials plan can only be authored during Materials Required"
        )
    if status in MATERIALS_FROZEN_STATUSES and plan is None:
        raise DeliveryExecutionRejected(
            "delivery materials plan is required before Procurement"
        )
    if current_payload is None:
        return

    current_status = current_payload.get("status")
    current_plan = _validate_materials_plan(current_payload.get("materialsPlan"), principal)

    if current_status in MATERIALS_FROZEN_STATUSES:
        if plan != current_plan:
            raise DeliveryExecutionRejected(
                "delivery materials plan is immutable after Procurement starts"
            )
        return

    if current_status == "engineeringReview":
        if plan is not None:
            raise DeliveryExecutionRejected(
                "enter Materials Required before authoring the delivery materials plan"
            )
        return

    if current_status != "materialsRequired":
        return

    if status == "procurement":
        if current_plan is None:
            raise DeliveryExecutionRejected(
                "save a delivery materials plan before starting Procurement"
            )
        if plan != current_plan:
            raise DeliveryExecutionRejected(
                "delivery materials plan must be saved before the Procurement transition"
            )
        return

    if status != "materialsRequired" or plan == current_plan:
        return
    if plan is None:
        raise DeliveryExecutionRejected("delivery materials plan cannot be cleared")

    expected_revision = 1 if current_plan is None else current_plan["revision"] + 1
    if plan["revision"] != expected_revision:
        raise DeliveryExecutionRejected(
            "delivery materials plan revision must advance by exactly one"
        )
    if plan.get("updatedAt") != payload.get("updatedAt"):
        raise DeliveryExecutionRejected(
            "delivery materials plan updatedAt must match delivery execution updatedAt"
        )
    _require_materials_plan_provenance(plan, principal, current_actor=True)


def _require_production_ready(plan: dict | None) -> None:
    if plan is None:
        raise DeliveryExecutionRejected(
            "save delivery procurement readiness before starting Production"
        )
    blocked = [
        requirement.get("requirementID")
        for requirement in plan.get("requirements", [])
        if requirement.get("status") != "available"
    ]
    if blocked:
        raise DeliveryExecutionRejected(
            "delivery Production is blocked until every frozen material is available"
        )


def _validate_procurement_handoff(
    payload: dict,
    current_payload: dict | None,
    principal: Principal,
    *,
    status: str,
) -> None:
    materials_plan = _validate_materials_plan(payload.get("materialsPlan"), principal)
    plan = _validate_procurement_plan(
        payload.get("procurementPlan"),
        materials_plan,
        principal,
    )

    if status in {"engineeringReview", "materialsRequired"} and plan is not None:
        raise DeliveryExecutionRejected(
            "delivery procurement plan can only be authored during Procurement"
        )
    if status in PROCUREMENT_FROZEN_STATUSES:
        _require_production_ready(plan)
    if current_payload is None:
        return

    current_status = current_payload.get("status")
    current_materials = _validate_materials_plan(
        current_payload.get("materialsPlan"),
        principal,
    )
    current_plan = _validate_procurement_plan(
        current_payload.get("procurementPlan"),
        current_materials,
        principal,
    )

    if current_status in PROCUREMENT_FROZEN_STATUSES:
        if plan != current_plan:
            raise DeliveryExecutionRejected(
                "delivery procurement plan is immutable after Production starts"
            )
        return

    if current_status in {"engineeringReview", "materialsRequired"}:
        if plan is not None:
            raise DeliveryExecutionRejected(
                "enter Procurement before authoring delivery procurement readiness"
            )
        return

    if current_status != "procurement":
        return

    if status == "production":
        if current_plan is None:
            raise DeliveryExecutionRejected(
                "save delivery procurement readiness before starting Production"
            )
        if plan != current_plan:
            raise DeliveryExecutionRejected(
                "delivery procurement plan must be saved before the Production transition"
            )
        _require_production_ready(plan)
        return

    if status != "procurement" or plan == current_plan:
        return
    if plan is None:
        raise DeliveryExecutionRejected("delivery procurement plan cannot be cleared")

    expected_revision = 1 if current_plan is None else current_plan["revision"] + 1
    if plan["revision"] != expected_revision:
        raise DeliveryExecutionRejected(
            "delivery procurement plan revision must advance by exactly one"
        )
    if plan.get("updatedAt") != payload.get("updatedAt"):
        raise DeliveryExecutionRejected(
            "delivery procurement plan updatedAt must match delivery execution updatedAt"
        )
    _require_procurement_plan_provenance(plan, principal, current_actor=True)


def validate_delivery_execution_payload(
    payload: dict,
    current_payload: dict | None,
    principal: Principal,
    *,
    entity_id: str,
) -> None:
    if payload.get("id") != entity_id:
        raise DeliveryExecutionRejected("delivery execution payload identity is invalid")

    status = payload.get("status")
    if status not in DELIVERY_STATUSES:
        raise DeliveryExecutionRejected("delivery execution status is invalid")

    events = _events(payload)
    _require_unique_event_ids(events)
    for event in events:
        _require_event_provenance(event, principal)

    first = events[0]
    if first.get("fromStatus") is not None or first.get("toStatus") != "engineeringReview":
        raise DeliveryExecutionRejected(
            "delivery execution must start with Engineering Review"
        )

    state = "engineeringReview"
    for index, event in enumerate(events):
        to_status = event.get("toStatus")
        if to_status not in DELIVERY_STATUSES:
            raise DeliveryExecutionRejected("delivery execution event status is invalid")
        if index == 0:
            continue
        if event.get("fromStatus") != state:
            raise DeliveryExecutionRejected("delivery execution event chain is discontinuous")
        expected = DELIVERY_TRANSITIONS.get(state)
        is_materials_revision = state == "materialsRequired" and to_status == "materialsRequired"
        is_procurement_revision = state == "procurement" and to_status == "procurement"
        if to_status != expected and not is_materials_revision and not is_procurement_revision:
            raise DeliveryExecutionRejected("delivery execution transition is invalid")
        state = to_status

    if state != status:
        raise DeliveryExecutionRejected(
            "delivery execution status does not match lifecycle history"
        )

    if payload.get("updatedAt") != events[-1].get("occurredAt"):
        raise DeliveryExecutionRejected(
            "delivery execution updatedAt must match its latest immutable event"
        )
    if payload.get("startedAt") != events[0].get("occurredAt"):
        raise DeliveryExecutionRejected(
            "delivery execution startedAt must match its initial event"
        )

    _validate_materials_handoff(payload, current_payload, principal, status=status)
    _validate_procurement_handoff(payload, current_payload, principal, status=status)

    if current_payload is None:
        _require_event_provenance(events[-1], principal, current_actor=True)
        return

    for key in IMMUTABLE_HANDOFF_KEYS:
        if current_payload.get(key) != payload.get(key):
            raise DeliveryExecutionRejected(
                f"delivery execution immutable handoff field {key} changed"
            )

    current_events = current_payload.get("events")
    if not isinstance(current_events, list) or not all(
        isinstance(event, dict) for event in current_events
    ):
        raise DeliveryExecutionRejected("canonical delivery execution history is malformed")
    if len(events) < len(current_events):
        raise DeliveryExecutionRejected("delivery execution history cannot be truncated")
    if events[: len(current_events)] != current_events:
        raise DeliveryExecutionRejected("delivery execution history is immutable")

    appended = events[len(current_events) :]
    if not appended:
        protected = {"status", "updatedAt", "materialsPlan", "procurementPlan"}
        if any(current_payload.get(key) != payload.get(key) for key in protected):
            raise DeliveryExecutionRejected(
                "delivery execution operational changes must append a lifecycle event"
            )
        return
    if len(appended) != 1:
        raise DeliveryExecutionRejected(
            "delivery execution may append only one transition per mutation"
        )
    newest = appended[-1]
    if (
        newest.get("fromStatus") == "materialsRequired"
        and newest.get("toStatus") == "materialsRequired"
        and current_payload.get("materialsPlan") == payload.get("materialsPlan")
    ):
        raise DeliveryExecutionRejected(
            "Materials Required self-transition requires a materials plan revision"
        )
    if (
        newest.get("fromStatus") == "procurement"
        and newest.get("toStatus") == "procurement"
        and current_payload.get("procurementPlan") == payload.get("procurementPlan")
    ):
        raise DeliveryExecutionRejected(
            "Procurement self-transition requires a procurement plan revision"
        )
    _require_event_provenance(newest, principal, current_actor=True)


async def authorize_delivery_execution(
    db: AsyncSession,
    principal: Principal,
    scope: EffectiveScope,
    *,
    entity_id: str,
    payload: dict,
    deleted_at: object | None,
    current: SyncEntity | None,
) -> str:
    if DELIVERY_CAPABILITY not in principal.capabilities:
        raise DeliveryExecutionRejected(f"{DELIVERY_CAPABILITY} capability required")
    if deleted_at is not None:
        raise DeliveryExecutionRejected("delivery execution history cannot be tombstoned")

    current_payload = (
        current.payload_json
        if current is not None and isinstance(current.payload_json, dict)
        else None
    )
    validate_delivery_execution_payload(
        payload,
        current_payload,
        principal,
        entity_id=entity_id,
    )

    project_id = _normalized(payload.get("projectID"))
    customer_id = _normalized(payload.get("customerID"))
    quotation_id = _normalized(payload.get("quotationID"))
    if project_id is None or customer_id is None or quotation_id is None:
        raise DeliveryExecutionRejected(
            "delivery execution project/customer/quotation identity is required"
        )

    project = await db.get(CanonicalProject, (principal.organization_id, project_id))
    if project is None or project.deleted_at is not None:
        raise DeliveryExecutionRejected("delivery execution Project is not canonical and active")
    if project.customer_id != customer_id:
        raise DeliveryExecutionRejected(
            "delivery execution Customer does not match canonical Project"
        )
    if not scope.can_access_project(project.project_id, project.customer_id):
        raise DeliveryExecutionRejected("delivery execution Project is outside authorized scope")

    quotation = await db.get(
        SyncEntity,
        (principal.organization_id, "quotation", quotation_id),
    )
    if (
        quotation is None
        or quotation.deleted_at is not None
        or not isinstance(quotation.payload_json, dict)
    ):
        raise DeliveryExecutionRejected(
            "delivery execution requires a canonical approved quotation"
        )
    quote = quotation.payload_json
    if quote.get("status") != "approved":
        raise DeliveryExecutionRejected(
            "delivery execution requires an approved quotation"
        )
    if quote.get("projectID") != project_id or quote.get("customerID") != customer_id:
        raise DeliveryExecutionRejected(
            "delivery execution quotation ownership does not match Project"
        )
    quote_events = quote.get("events") or []
    if not isinstance(quote_events, list) or not any(
        isinstance(event, dict) and event.get("kind") == "approved"
        for event in quote_events
    ):
        raise DeliveryExecutionRejected(
            "delivery execution quotation is missing immutable approval history"
        )

    expected = {
        "quotationNumber": quote.get("number"),
        "quotationRevision": _quotation_revision(quote),
        "currencyCode": quote.get("currencyCode"),
        "pricingVersion": quote.get("pricingVersion"),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise DeliveryExecutionRejected(
                f"delivery execution {key} does not match canonical quotation"
            )
    if _decimal(payload.get("quotedTotal"), name="quotedTotal") != _quotation_total(quote):
        raise DeliveryExecutionRejected(
            "delivery execution quotedTotal does not match canonical quotation"
        )

    child = await db.get(
        CanonicalProjectChild,
        (principal.organization_id, DELIVERY_ENTITY_TYPE, entity_id),
    )
    if child is not None and child.project_id != project_id:
        raise DeliveryExecutionRejected(
            "delivery execution Project ownership is immutable"
        )
    return project_id


async def apply_delivery_execution_ownership(
    db: AsyncSession,
    principal: Principal,
    *,
    entity_id: str,
    project_id: str,
) -> None:
    child = await db.get(
        CanonicalProjectChild,
        (principal.organization_id, DELIVERY_ENTITY_TYPE, entity_id),
    )
    if child is None:
        db.add(CanonicalProjectChild(
            organization_id=principal.organization_id,
            entity_type=DELIVERY_ENTITY_TYPE,
            entity_id=entity_id,
            project_id=project_id,
            deleted_at=None,
        ))
    else:
        if child.project_id != project_id:
            raise DeliveryExecutionRejected(
                "delivery execution Project ownership is immutable"
            )
        child.deleted_at = None
    await db.flush()


async def delivery_execution_is_visible(
    db: AsyncSession,
    principal: Principal,
    *,
    entity_id: str,
) -> bool:
    child = await db.get(
        CanonicalProjectChild,
        (principal.organization_id, DELIVERY_ENTITY_TYPE, entity_id),
    )
    if child is None or child.deleted_at is not None:
        return False
    project = await db.get(
        CanonicalProject,
        (principal.organization_id, child.project_id),
    )
    if project is None or project.deleted_at is not None:
        return False
    return EffectiveScope.from_principal(principal).can_access_project(
        project.project_id,
        project.customer_id,
    )
