from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalItemChild,
    CanonicalProject,
    CanonicalProjectChild,
    CanonicalProjectSector,
    Membership,
    Organization,
    SyncEntity,
)


class AuthorizationRejected(Exception):
    """A record could not be authorized from canonical server state."""


@dataclass
class EffectiveScope:
    all_customers: bool
    all_projects: bool
    customer_ids: set[str] = field(default_factory=set)
    project_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_principal(cls, principal: Principal) -> "EffectiveScope":
        return cls(
            all_customers=principal.all_customers,
            all_projects=principal.all_projects,
            customer_ids=set(principal.customer_ids),
            project_ids=set(principal.project_ids),
        )

    def can_access_customer(self, customer_id: str) -> bool:
        return self.all_customers or customer_id in self.customer_ids

    def can_access_project(self, project_id: str, customer_id: str) -> bool:
        return self.can_access_customer(customer_id) and (
            self.all_projects or project_id in self.project_ids
        )


@dataclass(frozen=True)
class OwnershipPlan:
    entity_type: str
    entity_id: str
    is_create: bool
    deleted_at: datetime | None
    customer_id: str | None = None
    project_id: str | None = None
    project_sector_id: str | None = None
    sector_id: str | None = None
    item_id: str | None = None


ITEM_CHILD_CAPABILITIES = {
    "measurement": "item.measurements.manage",
    "evidence": "item.evidence.manage",
    "customer_requirement": "item.requirements.manage",
    "configuration": "item.configuration.manage",
    "configuration_version": "item.configuration.manage",
    "blocker": "item.blockers.manage",
}
ITEM_CHILD_TYPES = set(ITEM_CHILD_CAPABILITIES) | {"item_audit_event"}
APPEND_ONLY_TYPES = {"item_audit_event", "configuration_version"}
# Records stored in the generic canonical_project_children table.
PROJECT_CHILD_TYPES = {"quotation", "delivery_execution"}
ENTITY_ORDER = {
    "customer": 0,
    "project": 1,
    "project_sector": 2,
    "item": 3,
    "item_audit_event": 4,
    "evidence": 4,
    "measurement": 4,
    "blocker": 4,
    "customer_requirement": 5,
    "configuration": 6,
    "configuration_version": 7,
    "quotation": 8,
    # A delivery execution is raised against a quotation, so it must be
    # authorized after it. Keeping them adjacent is what lets a client send the
    # approved quotation and the delivery execution it opens in one atomic
    # batch, in any client-side order.
    "delivery_execution": 9,
}


def mutation_sort_key(entity_type: str, original_index: int) -> tuple[int, int]:
    # Canonical parents and referenced child records are evaluated first so a single atomic batch
    # can arrive in arbitrary client order without ever trusting a child-provided parent implicitly.
    return ENTITY_ORDER.get(entity_type, 100), original_index


def _value(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuthorizationRejected(f"{key} is required for canonical ownership")
    return value.strip()


def _require_identity(payload: dict, entity_id: str, *, deleting: bool) -> None:
    value = payload.get("id")
    if deleting and value is None:
        return
    if not isinstance(value, str) or value.strip() != entity_id:
        raise AuthorizationRejected("payload identity does not match canonical entityID")


def _require_capability(principal: Principal, capability: str) -> None:
    if capability not in principal.capabilities:
        raise AuthorizationRejected(f"{capability} capability required")


def _normalized(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


async def _current_quotation_id(
    db: AsyncSession,
    principal: Principal,
    entity_id: str,
    payload: dict,
    *,
    deleting: bool,
) -> str:
    """Resolve — and pin — the quotation a stored delivery execution belongs to.

    The canonical child row records the owning Project but not the Quotation, so
    the linkage is read back from the committed payload. Reading it from storage
    rather than trusting the incoming payload is what makes the immutability
    check meaningful: a client cannot re-point an execution at another quotation
    by simply restating quotationID.
    """
    current = await db.get(
        SyncEntity,
        (principal.organization_id, "delivery_execution", entity_id),
    )
    if current is None or not isinstance(current.payload_json, dict):
        raise AuthorizationRejected("existing delivery execution has no canonical payload")
    stored = _normalized(current.payload_json.get("quotationID"))
    if stored is None:
        raise AuthorizationRejected("existing delivery execution lacks a canonical Quotation")
    supplied = _normalized(payload.get("quotationID"))
    if not deleting and supplied is not None and supplied != stored:
        raise AuthorizationRejected("Delivery execution Quotation ownership is immutable")
    return stored


def _require_current_provenance(principal: Principal, payload: dict) -> None:
    expected = {
        "actorID": principal.user_id,
        "organizationID": principal.organization_id,
        "membershipID": principal.membership_id,
        "sessionID": principal.session_id,
        "authorizationRevision": principal.authorization_revision,
    }
    for key, value in expected.items():
        supplied = payload.get(key)
        if supplied != value:
            raise AuthorizationRejected(f"{key} does not match authenticated provenance")


async def _project_for_scope(
    db: AsyncSession,
    principal: Principal,
    scope: EffectiveScope,
    project_id: str,
) -> CanonicalProject:
    project = await db.get(CanonicalProject, (principal.organization_id, project_id))
    if project is None or project.deleted_at is not None:
        raise AuthorizationRejected("Project is not canonical and active")
    if not scope.can_access_project(project.project_id, project.customer_id):
        raise AuthorizationRejected("Project is outside authorized scope")
    return project


async def _item_for_scope(
    db: AsyncSession,
    principal: Principal,
    scope: EffectiveScope,
    item_id: str,
) -> tuple[CanonicalItem, CanonicalProject]:
    item = await db.get(CanonicalItem, (principal.organization_id, item_id))
    if item is None or item.deleted_at is not None:
        raise AuthorizationRejected("Item is not canonical and active")
    if not item.project_sector_id:
        raise AuthorizationRejected("Item lacks canonical ProjectSector ownership")
    project = await _project_for_scope(db, principal, scope, item.project_id)
    project_sector = await db.get(
        CanonicalProjectSector,
        (principal.organization_id, item.project_sector_id),
    )
    if (
        project_sector is None
        or project_sector.deleted_at is not None
        or project_sector.project_id != item.project_id
    ):
        raise AuthorizationRejected("Item ProjectSector is not canonical and active")
    return item, project


async def _active_item_child(
    db: AsyncSession,
    principal: Principal,
    entity_type: str,
    entity_id: str,
) -> CanonicalItemChild | None:
    value = await db.get(
        CanonicalItemChild,
        (principal.organization_id, entity_type, entity_id),
    )
    return value if value is not None and value.deleted_at is None else None


async def _require_item_references(
    db: AsyncSession,
    principal: Principal,
    item_id: str,
    entity_type: str,
    ids: list | set | tuple,
) -> None:
    for entity_id in ids:
        if not isinstance(entity_id, str) or not entity_id:
            raise AuthorizationRejected(f"invalid {entity_type} reference")
        child = await _active_item_child(db, principal, entity_type, entity_id)
        if child is None or child.item_id != item_id:
            raise AuthorizationRejected(
                f"{entity_type} reference is not canonical for the same Item"
            )


async def _current_payload(
    db: AsyncSession,
    principal: Principal,
    entity_type: str,
    entity_id: str,
) -> dict | None:
    entity = await db.get(
        SyncEntity,
        (principal.organization_id, entity_type, entity_id),
    )
    return entity.payload_json if entity is not None and isinstance(entity.payload_json, dict) else None


async def _ensure_customer_delete_safe(
    db: AsyncSession,
    principal: Principal,
    customer_id: str,
) -> None:
    active = await db.scalar(
        select(CanonicalProject.project_id).where(
            CanonicalProject.organization_id == principal.organization_id,
            CanonicalProject.customer_id == customer_id,
            CanonicalProject.deleted_at.is_(None),
        ).limit(1)
    )
    if active is not None:
        raise AuthorizationRejected("Customer cannot be deleted while Projects remain active")


async def _ensure_project_delete_safe(
    db: AsyncSession,
    principal: Principal,
    project_id: str,
) -> None:
    active_sector = await db.scalar(
        select(CanonicalProjectSector.project_sector_id).where(
            CanonicalProjectSector.organization_id == principal.organization_id,
            CanonicalProjectSector.project_id == project_id,
            CanonicalProjectSector.deleted_at.is_(None),
        ).limit(1)
    )
    active_item = await db.scalar(
        select(CanonicalItem.item_id).where(
            CanonicalItem.organization_id == principal.organization_id,
            CanonicalItem.project_id == project_id,
            CanonicalItem.deleted_at.is_(None),
        ).limit(1)
    )
    active_child = await db.scalar(
        select(CanonicalProjectChild.entity_id).where(
            CanonicalProjectChild.organization_id == principal.organization_id,
            CanonicalProjectChild.project_id == project_id,
            CanonicalProjectChild.deleted_at.is_(None),
        ).limit(1)
    )
    if active_sector is not None or active_item is not None or active_child is not None:
        raise AuthorizationRejected("Project cannot be deleted while canonical children remain active")


async def _ensure_project_sector_delete_safe(
    db: AsyncSession,
    principal: Principal,
    project_sector_id: str,
) -> None:
    active = await db.scalar(
        select(CanonicalItem.item_id).where(
            CanonicalItem.organization_id == principal.organization_id,
            CanonicalItem.project_sector_id == project_sector_id,
            CanonicalItem.deleted_at.is_(None),
        ).limit(1)
    )
    if active is not None:
        raise AuthorizationRejected("ProjectSector cannot be deleted while Items remain active")


async def _ensure_item_delete_safe(
    db: AsyncSession,
    principal: Principal,
    item_id: str,
) -> None:
    active = await db.scalar(
        select(CanonicalItemChild.entity_id).where(
            CanonicalItemChild.organization_id == principal.organization_id,
            CanonicalItemChild.item_id == item_id,
            CanonicalItemChild.entity_type != "item_audit_event",
            CanonicalItemChild.deleted_at.is_(None),
        ).limit(1)
    )
    if active is not None:
        raise AuthorizationRejected("Item cannot be deleted while sidecar/history records remain active")


async def _ensure_evidence_delete_safe(
    db: AsyncSession,
    principal: Principal,
    item_id: str,
    evidence_id: str,
) -> None:
    entities = (
        await db.scalars(
            select(SyncEntity).where(
                SyncEntity.organization_id == principal.organization_id,
                SyncEntity.entity_type.in_(["measurement", "customer_requirement"]),
                SyncEntity.deleted_at.is_(None),
            )
        )
    ).all()
    for entity in entities:
        payload = entity.payload_json or {}
        if payload.get("itemID") != item_id:
            continue
        refs = payload.get("evidenceReferences") if entity.entity_type == "measurement" else payload.get("evidenceReferenceIDs")
        if isinstance(refs, list) and evidence_id in refs:
            raise AuthorizationRejected("Evidence cannot be deleted while referenced")


async def authorize_record(
    db: AsyncSession,
    principal: Principal,
    scope: EffectiveScope,
    *,
    entity_type: str,
    entity_id: str,
    payload: dict,
    deleted_at: datetime | None,
    generic_entity_exists: bool,
) -> OwnershipPlan:
    deleting = deleted_at is not None

    if entity_type == "customer":
        canonical = await db.get(CanonicalCustomer, (principal.organization_id, entity_id))
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing Customer lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown Customer")
            _require_capability(principal, "customer.create")
            _require_identity(payload, entity_id, deleting=False)
            return OwnershipPlan("customer", entity_id, True, deleted_at, customer_id=entity_id)

        _require_capability(principal, "customer.edit")
        if not scope.can_access_customer(canonical.customer_id):
            raise AuthorizationRejected("Customer is outside authorized scope")
        _require_identity(payload, entity_id, deleting=deleting)
        if deleting:
            await _ensure_customer_delete_safe(db, principal, canonical.customer_id)
        return OwnershipPlan("customer", entity_id, False, deleted_at, customer_id=canonical.customer_id)

    if entity_type == "project":
        canonical = await db.get(CanonicalProject, (principal.organization_id, entity_id))
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing Project lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown Project")
            _require_capability(principal, "project.create")
            _require_identity(payload, entity_id, deleting=False)
            customer_id = _value(payload, "customerID")
            customer = await db.get(CanonicalCustomer, (principal.organization_id, customer_id))
            if customer is None or customer.deleted_at is not None:
                raise AuthorizationRejected("Project parent Customer is not canonical and active")
            if not scope.can_access_customer(customer_id):
                raise AuthorizationRejected("Project parent Customer is outside authorized scope")
            return OwnershipPlan(
                "project", entity_id, True, deleted_at,
                customer_id=customer_id, project_id=entity_id,
            )

        _require_capability(principal, "project.edit")
        if not scope.can_access_project(canonical.project_id, canonical.customer_id):
            raise AuthorizationRejected("Project is outside authorized scope")
        _require_identity(payload, entity_id, deleting=deleting)
        supplied_customer = payload.get("customerID")
        if supplied_customer is not None and supplied_customer != canonical.customer_id:
            raise AuthorizationRejected("Project Customer ownership is immutable")
        if deleting:
            await _ensure_project_delete_safe(db, principal, canonical.project_id)
        return OwnershipPlan(
            "project", entity_id, False, deleted_at,
            customer_id=canonical.customer_id, project_id=canonical.project_id,
        )

    if entity_type == "project_sector":
        canonical = await db.get(CanonicalProjectSector, (principal.organization_id, entity_id))
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing ProjectSector lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown ProjectSector")
            _require_capability(principal, "project.edit")
            _require_identity(payload, entity_id, deleting=False)
            project_id = _value(payload, "projectID")
            sector_id = _value(payload, "sectorID")
            project = await _project_for_scope(db, principal, scope, project_id)
            return OwnershipPlan(
                "project_sector", entity_id, True, deleted_at,
                customer_id=project.customer_id,
                project_id=project.project_id,
                project_sector_id=entity_id,
                sector_id=sector_id,
            )

        _require_capability(principal, "project.edit")
        project = await _project_for_scope(db, principal, scope, canonical.project_id)
        _require_identity(payload, entity_id, deleting=deleting)
        supplied_project = payload.get("projectID")
        supplied_sector = payload.get("sectorID")
        if supplied_project is not None and supplied_project != canonical.project_id:
            raise AuthorizationRejected("ProjectSector Project ownership is immutable")
        if supplied_sector is not None and supplied_sector != canonical.sector_id:
            raise AuthorizationRejected("ProjectSector Sector identity is immutable")
        if deleting:
            await _ensure_project_sector_delete_safe(db, principal, canonical.project_sector_id)
        return OwnershipPlan(
            "project_sector", entity_id, False, deleted_at,
            customer_id=project.customer_id,
            project_id=canonical.project_id,
            project_sector_id=canonical.project_sector_id,
            sector_id=canonical.sector_id,
        )

    if entity_type == "item":
        canonical = await db.get(CanonicalItem, (principal.organization_id, entity_id))
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing Item lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown Item")
            _require_capability(principal, "item.create")
            _require_identity(payload, entity_id, deleting=False)
            project_id = _value(payload, "projectID")
            project_sector_id = _value(payload, "projectSectorID")
            project = await _project_for_scope(db, principal, scope, project_id)
            project_sector = await db.get(
                CanonicalProjectSector,
                (principal.organization_id, project_sector_id),
            )
            if (
                project_sector is None
                or project_sector.deleted_at is not None
                or project_sector.project_id != project_id
            ):
                raise AuthorizationRejected("Item ProjectSector is not canonical for its Project")
            return OwnershipPlan(
                "item", entity_id, True, deleted_at,
                customer_id=project.customer_id,
                project_id=project.project_id,
                project_sector_id=project_sector.project_sector_id,
            )

        _require_capability(principal, "item.edit")
        if not canonical.project_sector_id:
            raise AuthorizationRejected("existing Item lacks canonical ProjectSector ownership")
        project = await _project_for_scope(db, principal, scope, canonical.project_id)
        project_sector = await db.get(
            CanonicalProjectSector,
            (principal.organization_id, canonical.project_sector_id),
        )
        if (
            project_sector is None
            or project_sector.deleted_at is not None
            or project_sector.project_id != canonical.project_id
        ):
            raise AuthorizationRejected("Item canonical ProjectSector is missing or inactive")
        _require_identity(payload, entity_id, deleting=deleting)
        supplied_project = payload.get("projectID")
        supplied_sector = payload.get("projectSectorID")
        if supplied_project is not None and supplied_project != canonical.project_id:
            raise AuthorizationRejected("Item Project ownership is immutable")
        if supplied_sector is not None and supplied_sector != canonical.project_sector_id:
            raise AuthorizationRejected("Item ProjectSector ownership is immutable")
        if deleting:
            await _ensure_item_delete_safe(db, principal, canonical.item_id)
        return OwnershipPlan(
            "item", entity_id, False, deleted_at,
            customer_id=project.customer_id,
            project_id=canonical.project_id,
            project_sector_id=canonical.project_sector_id,
        )

    if entity_type in ITEM_CHILD_TYPES:
        canonical = await db.get(
            CanonicalItemChild,
            (principal.organization_id, entity_type, entity_id),
        )
        if entity_type in APPEND_ONLY_TYPES and (canonical is not None or generic_entity_exists):
            raise AuthorizationRejected(f"{entity_type} history is append-only")
        if deleting and entity_type in APPEND_ONLY_TYPES:
            raise AuthorizationRejected(f"{entity_type} history cannot be tombstoned")
        if canonical is None and generic_entity_exists:
            raise AuthorizationRejected(f"existing {entity_type} lacks canonical ownership")
        if canonical is None and deleting:
            raise AuthorizationRejected(f"cannot tombstone an unknown {entity_type}")

        _require_identity(payload, entity_id, deleting=deleting)
        if canonical is None:
            if entity_type == "configuration_version":
                configuration = payload.get("configuration")
                if not isinstance(configuration, dict):
                    raise AuthorizationRejected("configuration_version payload is malformed")
                item_id = _value(configuration, "itemID")
                configuration_id = _value(configuration, "id")
                version = configuration.get("version")
                if not isinstance(version, int) or version < 1:
                    raise AuthorizationRejected("configuration_version version is invalid")
                if entity_id != f"{configuration_id}#v{version}":
                    raise AuthorizationRejected("configuration_version identity is invalid")
                base = await _active_item_child(db, principal, "configuration", configuration_id)
                if base is None or base.item_id != item_id:
                    raise AuthorizationRejected("configuration_version base configuration is not canonical")
            else:
                item_id = _value(payload, "itemID")
        else:
            item_id = canonical.item_id
            supplied_item = None
            if entity_type == "configuration_version":
                configuration = payload.get("configuration")
                if isinstance(configuration, dict):
                    supplied_item = configuration.get("itemID")
            else:
                supplied_item = payload.get("itemID")
            if supplied_item is not None and supplied_item != item_id:
                raise AuthorizationRejected(f"{entity_type} Item ownership is immutable")

        item, project = await _item_for_scope(db, principal, scope, item_id)

        if entity_type == "item_audit_event":
            kind = payload.get("kind")
            if kind == "created":
                _require_capability(principal, "item.create")
            elif kind == "updated":
                _require_capability(principal, "item.edit")
            elif kind == "commercial_readiness_changed":
                _require_capability(principal, "item.readiness.change")
                if payload.get("readinessOverrideUsed") is True:
                    _require_capability(principal, "item.readiness.override")
            else:
                raise AuthorizationRejected("item audit kind is invalid")
            after = payload.get("after")
            before = payload.get("before")
            if not isinstance(after, dict) or after.get("id") != item_id:
                raise AuthorizationRejected("item audit resulting Item identity is invalid")
            if after.get("projectID") != item.project_id or after.get("projectSectorID") != item.project_sector_id:
                raise AuthorizationRejected("item audit parent relationship is not canonical")
            if before is not None:
                if not isinstance(before, dict) or before.get("id") != item_id:
                    raise AuthorizationRejected("item audit prior Item identity is invalid")
                if before.get("projectID") != item.project_id or before.get("projectSectorID") != item.project_sector_id:
                    raise AuthorizationRejected("item audit prior parent relationship is not canonical")
            _require_current_provenance(principal, payload)

        else:
            _require_capability(principal, ITEM_CHILD_CAPABILITIES[entity_type])

        if entity_type == "measurement" and not deleting:
            refs = payload.get("evidenceReferences", [])
            if not isinstance(refs, list):
                raise AuthorizationRejected("measurement evidenceReferences is invalid")
            await _require_item_references(db, principal, item_id, "evidence", refs)

        if entity_type == "customer_requirement" and not deleting:
            refs = payload.get("evidenceReferenceIDs", [])
            if not isinstance(refs, list):
                raise AuthorizationRejected("requirement evidenceReferenceIDs is invalid")
            await _require_item_references(db, principal, item_id, "evidence", refs)

        if entity_type in {"configuration", "configuration_version"} and not deleting:
            configuration = payload if entity_type == "configuration" else payload.get("configuration")
            assert isinstance(configuration, dict)
            status = configuration.get("status")
            current = await _current_payload(db, principal, "configuration", configuration.get("id", entity_id))
            current_status = current.get("status") if isinstance(current, dict) else None
            if status == "complete" and current_status != "complete":
                _require_capability(principal, "item.configuration.finalize")
            elif current_status == "complete" and status != "complete":
                _require_capability(principal, "item.configuration.reopen")
            requirements = configuration.get("customerRequirements", [])
            blockers = configuration.get("blockerIDs", [])
            if not isinstance(requirements, list) or not isinstance(blockers, list):
                raise AuthorizationRejected("configuration references are invalid")
            await _require_item_references(
                db, principal, item_id, "customer_requirement", requirements
            )
            await _require_item_references(db, principal, item_id, "blocker", blockers)

        if entity_type == "blocker" and not deleting:
            events = payload.get("events")
            if not isinstance(events, list) or not events:
                raise AuthorizationRejected("connected blocker history requires events")
            for event in events:
                if not isinstance(event, dict):
                    raise AuthorizationRejected("blocker event is malformed")
                if event.get("blockerID") != entity_id or event.get("itemID") != item_id:
                    raise AuthorizationRejected("blocker event identity is invalid")
                if _normalized(event.get("organizationID")) != principal.organization_id:
                    raise AuthorizationRejected("blocker event organization is invalid")
                if any(event.get(key) is None for key in (
                    "actorID", "membershipID", "sessionID", "authorizationRevision"
                )):
                    raise AuthorizationRejected("blocker event provenance is incomplete")
            _require_current_provenance(principal, events[-1])

        if entity_type == "evidence" and deleting:
            await _ensure_evidence_delete_safe(db, principal, item_id, entity_id)

        return OwnershipPlan(
            entity_type, entity_id, canonical is None, deleted_at,
            customer_id=project.customer_id,
            project_id=project.project_id,
            project_sector_id=item.project_sector_id,
            item_id=item_id,
        )

    if entity_type == "quotation":
        canonical = await db.get(
            CanonicalProjectChild,
            (principal.organization_id, entity_type, entity_id),
        )
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing quotation lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown quotation")
            _require_identity(payload, entity_id, deleting=False)
            project_id = _value(payload, "projectID")
        else:
            project_id = canonical.project_id
            _require_identity(payload, entity_id, deleting=deleting)
            supplied_project = payload.get("projectID")
            if supplied_project is not None and supplied_project != project_id:
                raise AuthorizationRejected("Quotation Project ownership is immutable")

        project = await _project_for_scope(db, principal, scope, project_id)
        if not deleting:
            if payload.get("customerID") != project.customer_id:
                raise AuthorizationRejected("Quotation Customer does not match canonical Project")
            status = payload.get("status")
            capability = {
                "sent": "quotation.send",
                "approved": "quotation.approve",
                "draft": "quotation.create",
                "ready": "quotation.create",
                "rejected": "quotation.create",
                "superseded": "quotation.create",
            }.get(status)
            if capability is None:
                raise AuthorizationRejected("Quotation status is invalid")
            _require_capability(principal, capability)
            lines = payload.get("lines", [])
            if not isinstance(lines, list) or not lines:
                raise AuthorizationRejected("Quotation must contain canonical Item lines")
            for line in lines:
                if not isinstance(line, dict):
                    raise AuthorizationRejected("Quotation line is malformed")
                line_item_id = _value(line, "itemID")
                item, _ = await _item_for_scope(db, principal, scope, line_item_id)
                if item.project_id != project_id:
                    raise AuthorizationRejected("Quotation line Item belongs to another Project")
            events = payload.get("events") or []
            if not isinstance(events, list):
                raise AuthorizationRejected("Quotation event history is invalid")
            for event in events:
                if not isinstance(event, dict):
                    raise AuthorizationRejected("Quotation event is malformed")
                org = event.get("organizationID")
                if org is not None and org != principal.organization_id:
                    raise AuthorizationRejected("Quotation event organization is invalid")
            if status in {"sent", "approved", "rejected", "superseded"}:
                if not events:
                    raise AuthorizationRejected("Quotation lifecycle transition requires provenance")
                _require_current_provenance(principal, events[-1])
        else:
            _require_capability(principal, "quotation.create")

        return OwnershipPlan(
            "quotation", entity_id, canonical is None, deleted_at,
            customer_id=project.customer_id,
            project_id=project.project_id,
        )

    if entity_type == "delivery_execution":
        canonical = await db.get(
            CanonicalProjectChild,
            (principal.organization_id, entity_type, entity_id),
        )
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing delivery execution lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown delivery execution")
            _require_identity(payload, entity_id, deleting=False)
            quotation_id = _value(payload, "quotationID")
        else:
            quotation_id = await _current_quotation_id(
                db, principal, entity_id, payload, deleting=deleting
            )
            _require_identity(payload, entity_id, deleting=deleting)

        # The parent quotation must itself be canonical, active and in scope. A
        # delivery execution is meaningless without the commercial document it
        # executes, so this is checked on create and on every later update.
        quotation = await db.get(
            CanonicalProjectChild,
            (principal.organization_id, "quotation", quotation_id),
        )
        if quotation is None or quotation.deleted_at is not None:
            raise AuthorizationRejected("Delivery execution Quotation is not canonical and active")
        # The canonical child row carries the Project linkage only; the
        # commercial payload lives on the generic entity row, which is what the
        # lifecycle validator reads too.
        quotation_entity = await db.get(
            SyncEntity,
            (principal.organization_id, "quotation", quotation_id),
        )
        quotation_payload = (
            quotation_entity.payload_json if quotation_entity is not None else None
        )
        if not isinstance(quotation_payload, dict):
            raise AuthorizationRejected("Delivery execution Quotation payload is malformed")

        project = await _project_for_scope(db, principal, scope, quotation.project_id)
        if not deleting:
            if payload.get("customerID") != project.customer_id:
                raise AuthorizationRejected(
                    "Delivery execution Customer does not match canonical Project"
                )
            # Mirrors the client's own DeliveryExecutionLifecycle.start: work
            # cannot begin against a quotation the customer has not approved,
            # and an approved status alone is not enough — the approval must be
            # backed by its immutable event.
            if quotation_payload.get("status") != "approved":
                raise AuthorizationRejected(
                    "Delivery execution Quotation must be approved"
                )
            quotation_events = quotation_payload.get("events")
            if not isinstance(quotation_events, list) or not any(
                isinstance(event, dict) and event.get("kind") == "approved"
                for event in quotation_events
            ):
                raise AuthorizationRejected(
                    "Delivery execution Quotation is approved without its approval event"
                )
            _require_capability(principal, "delivery.manage")

        return OwnershipPlan(
            "delivery_execution", entity_id, canonical is None, deleted_at,
            customer_id=project.customer_id,
            project_id=project.project_id,
        )

    raise AuthorizationRejected(f"canonical authorization is not implemented for {entity_type}")


async def apply_ownership_plan(
    db: AsyncSession,
    principal: Principal,
    scope: EffectiveScope,
    plan: OwnershipPlan,
) -> None:
    if plan.entity_type == "customer":
        model = await db.get(CanonicalCustomer, (principal.organization_id, plan.entity_id))
        if model is None:
            model = CanonicalCustomer(
                organization_id=principal.organization_id,
                customer_id=plan.entity_id,
                deleted_at=plan.deleted_at,
            )
            db.add(model)
            scope.customer_ids.add(plan.entity_id)
            await _grant_created_scope(db, principal, customer_id=plan.entity_id)
        else:
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    if plan.entity_type == "project":
        assert plan.customer_id is not None
        model = await db.get(CanonicalProject, (principal.organization_id, plan.entity_id))
        if model is None:
            model = CanonicalProject(
                organization_id=principal.organization_id,
                project_id=plan.entity_id,
                customer_id=plan.customer_id,
                deleted_at=plan.deleted_at,
            )
            db.add(model)
            scope.project_ids.add(plan.entity_id)
            await _grant_created_scope(db, principal, project_id=plan.entity_id)
        else:
            if model.customer_id != plan.customer_id:
                raise AuthorizationRejected("Project Customer ownership is immutable")
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    if plan.entity_type == "project_sector":
        assert plan.project_id is not None and plan.sector_id is not None
        model = await db.get(CanonicalProjectSector, (principal.organization_id, plan.entity_id))
        if model is None:
            db.add(CanonicalProjectSector(
                organization_id=principal.organization_id,
                project_sector_id=plan.entity_id,
                project_id=plan.project_id,
                sector_id=plan.sector_id,
                deleted_at=plan.deleted_at,
            ))
        else:
            if model.project_id != plan.project_id or model.sector_id != plan.sector_id:
                raise AuthorizationRejected("ProjectSector ownership is immutable")
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    if plan.entity_type == "item":
        assert plan.project_id is not None and plan.project_sector_id is not None
        model = await db.get(CanonicalItem, (principal.organization_id, plan.entity_id))
        if model is None:
            db.add(CanonicalItem(
                organization_id=principal.organization_id,
                item_id=plan.entity_id,
                project_id=plan.project_id,
                project_sector_id=plan.project_sector_id,
                deleted_at=plan.deleted_at,
            ))
        else:
            if (
                model.project_id != plan.project_id
                or model.project_sector_id != plan.project_sector_id
            ):
                raise AuthorizationRejected("Item canonical ownership is immutable")
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    if plan.entity_type in PROJECT_CHILD_TYPES:
        assert plan.project_id is not None
        model = await db.get(
            CanonicalProjectChild,
            (principal.organization_id, plan.entity_type, plan.entity_id),
        )
        if model is None:
            db.add(CanonicalProjectChild(
                organization_id=principal.organization_id,
                entity_type=plan.entity_type,
                entity_id=plan.entity_id,
                project_id=plan.project_id,
                deleted_at=plan.deleted_at,
            ))
        else:
            if model.project_id != plan.project_id:
                label = plan.entity_type.replace("_", " ").capitalize()
                raise AuthorizationRejected(
                    f"{label} Project ownership is immutable"
                )
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    if plan.entity_type in ITEM_CHILD_TYPES:
        assert plan.item_id is not None
        model = await db.get(
            CanonicalItemChild,
            (principal.organization_id, plan.entity_type, plan.entity_id),
        )
        if model is None:
            db.add(CanonicalItemChild(
                organization_id=principal.organization_id,
                entity_type=plan.entity_type,
                entity_id=plan.entity_id,
                item_id=plan.item_id,
                deleted_at=plan.deleted_at,
            ))
        else:
            if model.item_id != plan.item_id:
                raise AuthorizationRejected(f"{plan.entity_type} Item ownership is immutable")
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    raise AuthorizationRejected(f"unsupported ownership plan {plan.entity_type}")


async def _grant_created_scope(
    db: AsyncSession,
    principal: Principal,
    *,
    customer_id: str | None = None,
    project_id: str | None = None,
) -> None:
    membership = await db.get(Membership, principal.membership_id)
    organization = await db.get(Organization, principal.organization_id)
    if (
        membership is None
        or organization is None
        or not membership.active
        or membership.organization_id != principal.organization_id
        or membership.user_id != principal.user_id
    ):
        raise AuthorizationRejected("creator membership is not canonical and active")

    changed = False
    if customer_id is not None and not membership.all_customers and customer_id not in membership.customer_ids:
        membership.customer_ids = [*membership.customer_ids, customer_id]
        changed = True
    if project_id is not None and not membership.all_projects and project_id not in membership.project_ids:
        membership.project_ids = [*membership.project_ids, project_id]
        changed = True
    if changed:
        organization.authorization_revision += 1
        await db.flush()


async def require_item_access(
    db: AsyncSession,
    principal: Principal,
    item_id: str,
    *,
    capability: str | None = None,
) -> tuple[CanonicalItem, CanonicalProject]:
    if capability is not None:
        _require_capability(principal, capability)
    scope = EffectiveScope.from_principal(principal)
    return await _item_for_scope(db, principal, scope, item_id)


async def record_is_visible(
    db: AsyncSession,
    principal: Principal,
    *,
    entity_type: str,
    entity_id: str,
) -> bool:
    scope = EffectiveScope.from_principal(principal)
    if entity_type == "customer":
        customer = await db.get(CanonicalCustomer, (principal.organization_id, entity_id))
        return customer is not None and scope.can_access_customer(customer.customer_id)
    if entity_type == "project":
        project = await db.get(CanonicalProject, (principal.organization_id, entity_id))
        return project is not None and scope.can_access_project(project.project_id, project.customer_id)
    if entity_type == "project_sector":
        sector = await db.get(CanonicalProjectSector, (principal.organization_id, entity_id))
        if sector is None:
            return False
        project = await db.get(CanonicalProject, (principal.organization_id, sector.project_id))
        return project is not None and scope.can_access_project(project.project_id, project.customer_id)
    if entity_type == "item":
        item = await db.get(CanonicalItem, (principal.organization_id, entity_id))
        if item is None:
            return False
        project = await db.get(CanonicalProject, (principal.organization_id, item.project_id))
        return project is not None and scope.can_access_project(project.project_id, project.customer_id)
    if entity_type in PROJECT_CHILD_TYPES:
        child = await db.get(
            CanonicalProjectChild,
            (principal.organization_id, entity_type, entity_id),
        )
        if child is None:
            return False
        project = await db.get(CanonicalProject, (principal.organization_id, child.project_id))
        return project is not None and scope.can_access_project(project.project_id, project.customer_id)
    if entity_type in ITEM_CHILD_TYPES:
        child = await db.get(
            CanonicalItemChild,
            (principal.organization_id, entity_type, entity_id),
        )
        if child is None:
            return False
        item = await db.get(CanonicalItem, (principal.organization_id, child.item_id))
        if item is None:
            return False
        project = await db.get(CanonicalProject, (principal.organization_id, item.project_id))
        return project is not None and scope.can_access_project(project.project_id, project.customer_id)
    return False