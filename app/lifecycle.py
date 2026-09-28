from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.models import SyncEntity


class LifecycleRejected(Exception):
    """A mutation violates an immutable server-side lifecycle invariant."""


_BLOCKER_EVENT_KINDS = {"created", "updated", "resolved", "reopened"}
_QUOTATION_EVENT_KINDS = {"sent", "approved", "rejected", "superseded"}
_QUOTATION_STATUSES = {"draft", "ready", "sent", "approved", "rejected", "superseded"}
_QUOTATION_CONTENT_LOCKED = {"sent", "rejected", "approved", "superseded"}
_QUOTATION_LIFECYCLE_KEYS = {"status", "events", "updatedAt"}

# Delivery execution is a strictly ordered pipeline that hangs off an approved
# quotation. The client (DeliveryExecution.allowedTransitions) is the source of
# truth for this graph; it is reproduced here so the server rejects a batch that
# claims a transition the client itself would refuse to make. Self-transitions
# are present in the client graph and mean "this state may be re-entered with a
# new event" (a note-only update), so they are legal here too.
_DELIVERY_STATUSES = {
    "engineeringReview",
    "materialsRequired",
    "procurement",
    "production",
    "readyForInstallation",
    "scheduled",
    "installed",
    "complete",
}
_DELIVERY_INITIAL_STATUS = "engineeringReview"
_DELIVERY_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "engineeringReview": ("materialsRequired",),
    "materialsRequired": ("materialsRequired", "procurement"),
    "procurement": ("procurement", "production"),
    "production": ("production", "readyForInstallation"),
    "readyForInstallation": ("readyForInstallation", "scheduled"),
    "scheduled": ("scheduled", "installed"),
    "installed": ("installed", "complete"),
    "complete": (),
}
# A delivery execution is bound to the quotation it was raised against for its
# whole life. Re-pointing it at another quotation or another revision of the
# same quotation would silently restate what the customer agreed to buy, so
# these are immutable after creation — matching the client's own
# commercial-handoff tampering guard.
_DELIVERY_IMMUTABLE_KEYS = (
    "quotationID",
    "quotationRevision",
    "quotationNumber",
    "projectID",
    "customerID",
    "currencyCode",
    "quotedTotal",
    "startedAt",
)


def _normalized(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _events(payload: dict, *, name: str, required: bool = False) -> list[dict]:
    raw = payload.get("events")
    if raw is None and not required:
        return []
    if not isinstance(raw, list) or (required and not raw):
        raise LifecycleRejected(f"{name} history is required")
    if not all(isinstance(event, dict) for event in raw):
        raise LifecycleRejected(f"{name} history is malformed")
    return raw


def _require_unique_event_ids(events: Iterable[dict], *, name: str) -> None:
    seen: set[str] = set()
    for event in events:
        event_id = _normalized(event.get("id"))
        if event_id is None:
            raise LifecycleRejected(f"{name} event id is required")
        if event_id in seen:
            raise LifecycleRejected(f"{name} event ids must be unique")
        seen.add(event_id)


def _require_connected_provenance(
    event: dict,
    principal: Principal,
    *,
    name: str,
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
        raise LifecycleRejected(f"{name} event provenance is incomplete")
    if organization_id != principal.organization_id:
        raise LifecycleRejected(f"{name} event organization is invalid")
    if current_actor and (
        actor_id != principal.user_id
        or membership_id != principal.membership_id
        or session_id != principal.session_id
        or revision != principal.authorization_revision
    ):
        raise LifecycleRejected(f"{name} latest event does not match authenticated provenance")


def _require_immutable_prefix(
    current_payload: dict | None,
    proposed_events: list[dict],
    *,
    name: str,
) -> list[dict]:
    if current_payload is None:
        return proposed_events
    current_events = current_payload.get("events") or []
    if not isinstance(current_events, list) or not all(isinstance(event, dict) for event in current_events):
        raise LifecycleRejected(f"canonical {name} history is malformed")
    if len(proposed_events) < len(current_events):
        raise LifecycleRejected(f"{name} history cannot be truncated")
    if proposed_events[: len(current_events)] != current_events:
        raise LifecycleRejected(f"{name} history is immutable")
    return proposed_events[len(current_events) :]


def _blocker_snapshot(event: dict) -> tuple[object, ...]:
    return (
        event.get("blockerCategory"),
        _normalized(event.get("blockerKind")) or "",
        _normalized(event.get("description")) or "",
        event.get("critical"),
        _normalized(event.get("responsiblePerson")) or "",
        _normalized(event.get("vendorSupplier")) or "",
    )


def _validate_optional_blocker_snapshot(payload: dict, event: dict) -> None:
    checks = (
        ("blockerCategory", payload.get("category")),
        ("blockerKind", _normalized(payload.get("kind")) or ""),
        ("description", _normalized(payload.get("description")) or ""),
        ("critical", payload.get("critical")),
        ("responsiblePerson", _normalized(payload.get("responsiblePerson")) or ""),
        ("vendorSupplier", _normalized(payload.get("vendorSupplier")) or ""),
    )
    for event_key, expected in checks:
        if event_key not in event:
            continue
        actual = event.get(event_key)
        if event_key in {"blockerKind", "description", "responsiblePerson", "vendorSupplier"}:
            actual = _normalized(actual) or ""
        if actual != expected:
            raise LifecycleRejected(f"blocker latest event {event_key} does not match snapshot")


def validate_blocker_lifecycle(
    payload: dict,
    current_payload: dict | None,
    principal: Principal,
    *,
    entity_id: str,
) -> None:
    description = _normalized(payload.get("description"))
    if description is None:
        raise LifecycleRejected("blocker description is required")
    status = payload.get("status")
    if status not in {"open", "resolved"}:
        raise LifecycleRejected("blocker status is invalid")
    if status == "open" and payload.get("resolvedAt") is not None:
        raise LifecycleRejected("open blocker cannot have resolvedAt")
    if status == "resolved" and payload.get("resolvedAt") is None:
        raise LifecycleRejected("resolved blocker requires resolvedAt")

    item_id = _normalized(payload.get("itemID"))
    if item_id is None:
        raise LifecycleRejected("blocker itemID is required")
    events = _events(payload, name="blocker", required=True)
    _require_unique_event_ids(events, name="blocker")
    appended = _require_immutable_prefix(current_payload, events, name="blocker")

    state: str | None = None
    previous_snapshot: tuple[object, ...] | None = None
    for index, event in enumerate(events):
        kind = event.get("kind")
        if kind not in _BLOCKER_EVENT_KINDS:
            raise LifecycleRejected("blocker event kind is invalid")
        if event.get("blockerID") != entity_id or event.get("itemID") != item_id:
            raise LifecycleRejected("blocker event identity is invalid")
        _require_connected_provenance(event, principal, name="blocker")

        snapshot = _blocker_snapshot(event)
        if kind == "created":
            if index != 0 or state is not None:
                raise LifecycleRejected("blocker created event must be first")
            state = "open"
        elif kind == "updated":
            if state is None:
                raise LifecycleRejected("blocker update cannot precede creation")
            if previous_snapshot is not None and snapshot == previous_snapshot:
                raise LifecycleRejected("blocker updated event must record a material field change")
        elif kind == "resolved":
            if state != "open":
                raise LifecycleRejected("blocker can only resolve from open")
            state = "resolved"
        elif kind == "reopened":
            if state != "resolved":
                raise LifecycleRejected("blocker can only reopen from resolved")
            state = "open"
        previous_snapshot = snapshot

    if state != status:
        raise LifecycleRejected("blocker status does not match lifecycle history")
    _validate_optional_blocker_snapshot(payload, events[-1])

    if current_payload is not None and not appended:
        protected = (
            "category", "kind", "description", "status", "critical", "resolvedAt",
            "responsiblePerson", "vendorSupplier",
        )
        if any(current_payload.get(key) != payload.get(key) for key in protected):
            raise LifecycleRejected("blocker changes must append a lifecycle event")
    if appended:
        _require_connected_provenance(
            appended[-1], principal, name="blocker", current_actor=True
        )


def _quotation_no_event_closure(states: set[str]) -> set[str]:
    result = set(states)
    changed = True
    while changed:
        changed = False
        for state in tuple(result):
            destinations: tuple[str, ...]
            if state == "draft":
                destinations = ("ready",)
            elif state == "ready":
                destinations = ("draft",)
            elif state == "rejected":
                destinations = ("draft",)
            else:
                destinations = ()
            for destination in destinations:
                if destination not in result:
                    result.add(destination)
                    changed = True
    return result


def _apply_quotation_event(states: set[str], kind: str) -> set[str]:
    reachable = _quotation_no_event_closure(states)
    if kind == "sent":
        return {"sent"} if "ready" in reachable else set()
    if kind == "approved":
        return {"approved"} if "sent" in reachable else set()
    if kind == "rejected":
        return {"rejected"} if "sent" in reachable else set()
    if kind == "superseded":
        return {"superseded"} if reachable & {"draft", "ready", "sent", "rejected"} else set()
    return set()


def _quotation_revision(payload: dict) -> int:
    revision = payload.get("revision")
    if revision is None:
        return 1
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise LifecycleRejected("quotation revision must be at least 1")
    return revision


def _commercial_content(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key not in _QUOTATION_LIFECYCLE_KEYS}


async def validate_quotation_lifecycle(
    db: AsyncSession,
    payload: dict,
    current_payload: dict | None,
    principal: Principal,
    *,
    entity_id: str,
) -> None:
    status = payload.get("status")
    if status not in _QUOTATION_STATUSES:
        raise LifecycleRejected("quotation status is invalid")
    events = _events(payload, name="quotation")
    _require_unique_event_ids(events, name="quotation")
    for event in events:
        if event.get("kind") not in _QUOTATION_EVENT_KINDS:
            raise LifecycleRejected("quotation event kind is invalid")
        _require_connected_provenance(event, principal, name="quotation")

    appended = _require_immutable_prefix(current_payload, events, name="quotation")
    states = {"draft"}
    for event in events:
        states = _apply_quotation_event(states, event["kind"])
        if not states:
            raise LifecycleRejected("quotation lifecycle event sequence is invalid")
    if status not in _quotation_no_event_closure(states):
        raise LifecycleRejected("quotation status is not reachable from lifecycle history")

    if appended:
        _require_connected_provenance(
            appended[-1], principal, name="quotation", current_actor=True
        )

    revision = _quotation_revision(payload)
    supersedes = _normalized(payload.get("supersedesQuotationID"))
    if current_payload is not None:
        if _quotation_revision(current_payload) != revision:
            raise LifecycleRejected("quotation revision is immutable")
        if _normalized(current_payload.get("supersedesQuotationID")) != supersedes:
            raise LifecycleRejected("quotation supersession identity is immutable")
        current_status = current_payload.get("status")
        if current_status not in _QUOTATION_STATUSES:
            raise LifecycleRejected("canonical quotation status is invalid")
        if current_status in _QUOTATION_CONTENT_LOCKED:
            if _commercial_content(current_payload) != _commercial_content(payload):
                raise LifecycleRejected(
                    "customer-facing quotation content is locked; create a replacement revision"
                )
    else:
        if revision == 1:
            if supersedes is not None:
                raise LifecycleRejected("quotation revision 1 cannot supersede another quotation")
        else:
            if supersedes is None or supersedes == entity_id:
                raise LifecycleRejected("replacement quotation requires a predecessor")
            predecessor = await db.get(
                SyncEntity,
                (principal.organization_id, "quotation", supersedes),
            )
            if (
                predecessor is None
                or predecessor.deleted_at is not None
                or not isinstance(predecessor.payload_json, dict)
            ):
                raise LifecycleRejected("quotation predecessor is not canonical and active")
            predecessor_payload = predecessor.payload_json
            if predecessor_payload.get("projectID") != payload.get("projectID"):
                raise LifecycleRejected("quotation predecessor belongs to another Project")
            if predecessor_payload.get("customerID") != payload.get("customerID"):
                raise LifecycleRejected("quotation predecessor belongs to another Customer")
            if _quotation_revision(predecessor_payload) != revision - 1:
                raise LifecycleRejected("quotation predecessor revision is not contiguous")


def _delivery_no_event_closure(states: set[str]) -> set[str]:
    # A status may be re-entered without changing the state graph only via a
    # self-transition, so the closure of a set is itself. Kept as a function so
    # the reachability check reads the same way as the quotation validator's.
    return set(states)


def _delivery_status_for_event(
    event: dict, *, reachable: set[str]
) -> str:
    from_status = _normalized(event.get("fromStatus"))
    to_status = _normalized(event.get("toStatus"))
    if to_status is None or to_status not in _DELIVERY_STATUSES:
        raise LifecycleRejected("delivery execution event toStatus is invalid")
    if from_status is None:
        # The opening event declares no predecessor. The client builds it with
        # fromStatus nil and toStatus set to the initial status, so it opens the
        # pipeline rather than traversing an edge — and allowedTransitions has no
        # self-edge into engineeringReview to check it against. It is therefore
        # legal only as the very first event.
        if reachable != {_DELIVERY_INITIAL_STATUS}:
            raise LifecycleRejected("delivery execution creation event is not at the start")
        if to_status != _DELIVERY_INITIAL_STATUS:
            raise LifecycleRejected(
                "delivery execution must open at the initial status"
            )
        return to_status
    if from_status not in _DELIVERY_STATUSES:
        raise LifecycleRejected("delivery execution event fromStatus is invalid")
    if from_status not in reachable:
        raise LifecycleRejected("delivery execution event fromStatus is not reachable")
    if to_status not in _DELIVERY_TRANSITIONS[from_status]:
        raise LifecycleRejected("delivery execution transition is not permitted")
    return to_status


async def validate_delivery_execution_lifecycle(
    db: AsyncSession,
    payload: dict,
    current_payload: dict | None,
    principal: Principal,
    *,
    entity_id: str,
) -> None:
    status = payload.get("status")
    if status not in _DELIVERY_STATUSES:
        raise LifecycleRejected("delivery execution status is invalid")

    events = _events(payload, name="delivery execution")
    _require_unique_event_ids(events, name="delivery execution")
    for event in events:
        _require_connected_provenance(event, principal, name="delivery execution")

    appended = _require_immutable_prefix(current_payload, events, name="delivery execution")
    reachable = {_DELIVERY_INITIAL_STATUS}
    for event in events:
        reachable = {_delivery_status_for_event(event, reachable=reachable)}

    if status not in _delivery_no_event_closure(reachable):
        raise LifecycleRejected(
            "delivery execution status is not reachable from lifecycle history"
        )

    if appended:
        _require_connected_provenance(
            appended[-1], principal, name="delivery execution", current_actor=True
        )

    if current_payload is not None:
        for key in _DELIVERY_IMMUTABLE_KEYS:
            if current_payload.get(key) != payload.get(key):
                raise LifecycleRejected(
                    f"delivery execution {key} is immutable"
                )


async def validate_lifecycle_mutation(
    db: AsyncSession,
    principal: Principal,
    *,
    entity_type: str,
    entity_id: str,
    payload: dict,
    deleted_at: object | None,
    current: SyncEntity | None,
) -> None:
    # Tombstone authorization/referential integrity belongs to canonical ownership policy. A V1
    # tombstone may intentionally carry an empty payload, so lifecycle validation is for upserts.
    if deleted_at is not None:
        return
    current_payload = (
        current.payload_json
        if current is not None and isinstance(current.payload_json, dict)
        else None
    )
    if entity_type == "blocker":
        validate_blocker_lifecycle(
            payload, current_payload, principal, entity_id=entity_id
        )
    elif entity_type == "quotation":
        await validate_quotation_lifecycle(
            db, payload, current_payload, principal, entity_id=entity_id
        )
    elif entity_type == "delivery_execution":
        await validate_delivery_execution_lifecycle(
            db, payload, current_payload, principal, entity_id=entity_id
        )
