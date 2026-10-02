from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, JsonValue
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.observability import correlation_ref, log_event
from app.reference_models import ReferencePublication
from app.schemas import StrictModel

PublicationKind = Literal["catalog", "compatibility_rules", "price_book"]
ReferenceEntryKind = Literal[
    "sector",
    "item_type",
    "aluminum_system",
    "aluminum_profile",
    "finish",
    "glass_type",
    "glass_thickness",
    "glass_treatment",
    "hardware",
    "accessory",
    "vendor_material",
]


class ReferenceEntry(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    kind: ReferenceEntryKind
    code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    active: bool = True
    vendorSKU: str | None = Field(default=None, max_length=256)
    unitCostMinor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class CompatibilityRule(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    itemTypeID: str = Field(min_length=1, max_length=256)
    availableComponents: list[str] = Field(default_factory=list)
    requiredComponents: list[str] = Field(default_factory=list)
    allowedSectionTypes: list[str] = Field(default_factory=list)
    compatibleEntryIDs: list[str] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class PriceBookLine(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    itemTypeID: str | None = Field(default=None, max_length=256)
    referenceEntryID: str | None = Field(default=None, max_length=256)
    description: str = Field(min_length=1, max_length=512)
    unitPriceMinor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    active: bool = True
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class ReferencePublicationPayload(StrictModel):
    source: str = Field(min_length=1, max_length=128)
    authoritative: bool = False
    entries: list[ReferenceEntry] = Field(default_factory=list)
    compatibilityRules: list[CompatibilityRule] = Field(default_factory=list)
    priceBookLines: list[PriceBookLine] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ReferencePublicationCreate(StrictModel):
    publicationID: str = Field(min_length=1, max_length=256)
    kind: PublicationKind
    versionID: str = Field(min_length=1, max_length=256)
    effectiveFrom: datetime
    effectiveUntil: datetime | None = None
    supersedesPublicationID: str | None = Field(default=None, max_length=256)
    payload: ReferencePublicationPayload


class ReferencePublicationResponse(StrictModel):
    publicationID: str
    kind: PublicationKind
    versionID: str
    effectiveFrom: datetime
    effectiveUntil: datetime | None = None
    supersedesPublicationID: str | None = None
    contentSHA256: str
    publishedByUserID: str
    publishedAt: datetime
    payload: ReferencePublicationPayload


class ReferencePublicationSummary(StrictModel):
    publicationID: str
    kind: PublicationKind
    versionID: str
    effectiveFrom: datetime
    effectiveUntil: datetime | None = None
    contentSHA256: str
    publishedAt: datetime


class ReferencePublicationListResponse(StrictModel):
    publications: list[ReferencePublicationResponse] = Field(default_factory=list)


class ReferencePublicationManifest(StrictModel):
    current: list[ReferencePublicationSummary] = Field(default_factory=list)
    available: list[ReferencePublicationSummary] = Field(default_factory=list)


class PublicationConflict(Exception):
    pass


class PublicationNotFound(Exception):
    pass


BASELINE_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)
APPLICATION_CATALOG_EFFECTIVE_FROM = datetime(2026, 9, 10, tzinfo=timezone.utc)


def _entry(id: str, kind: ReferenceEntryKind, code: str, name: str) -> dict:
    return {"id": id, "kind": kind, "code": code, "name": name}


BASELINE_CATALOG = ReferencePublicationCreate(
    publicationID="catalog-bundled-v1",
    kind="catalog",
    versionID="configurator-catalog-v1",
    effectiveFrom=BASELINE_EFFECTIVE_FROM,
    payload=ReferencePublicationPayload(
        source="bundled-ios-v1",
        authoritative=False,
        entries=[
            _entry("al-system-sliding", "aluminum_system", "SLIDING", "Sliding Aluminum System"),
            _entry("al-system-projected", "aluminum_system", "PROJECTED", "Projected / Casement System"),
            _entry("al-system-swing-door", "aluminum_system", "SWING_DOOR", "Swing Door System"),
            _entry("al-system-storefront", "aluminum_system", "STOREFRONT", "Storefront / Facade System"),
            _entry("al-system-railing", "aluminum_system", "RAILING", "Railing / Guard System"),
            _entry("al-profile-frame", "aluminum_profile", "FRAME", "Frame Profile"),
            _entry("al-profile-sash", "aluminum_profile", "SASH", "Sash / Vent Profile"),
            _entry("al-profile-mullion", "aluminum_profile", "MULLION", "Mullion Profile"),
            _entry("al-profile-transom", "aluminum_profile", "TRANSOM", "Transom Profile"),
            _entry("al-profile-threshold", "aluminum_profile", "THRESHOLD", "Threshold / Sill Profile"),
            _entry("finish-natural-anodized", "finish", "NATURAL_ANODIZED", "Natural Anodized"),
            _entry("finish-black", "finish", "BLACK", "Black"),
            _entry("finish-white", "finish", "WHITE", "White"),
            _entry("finish-bronze", "finish", "BRONZE", "Bronze"),
            _entry("finish-custom", "finish", "CUSTOM", "Custom / Project Finish"),
            _entry("glass-clear-float", "glass_type", "CLEAR_FLOAT", "Clear Float Glass"),
            _entry("glass-tempered", "glass_type", "TEMPERED", "Tempered Safety Glass"),
            _entry("glass-laminated", "glass_type", "LAMINATED", "Laminated Safety Glass"),
            _entry("glass-insulated", "glass_type", "INSULATED", "Insulated Glass Unit"),
            _entry("glass-patterned", "glass_type", "PATTERNED", "Patterned / Privacy Glass"),
            _entry("glass-4mm", "glass_thickness", "4MM", "4 mm"),
            _entry("glass-5mm", "glass_thickness", "5MM", "5 mm"),
            _entry("glass-6mm", "glass_thickness", "6MM", "6 mm"),
            _entry("glass-8mm", "glass_thickness", "8MM", "8 mm"),
            _entry("glass-10mm", "glass_thickness", "10MM", "10 mm"),
            _entry("glass-12mm", "glass_thickness", "12MM", "12 mm"),
            _entry("glass-treatment-none", "glass_treatment", "NONE", "No Additional Treatment"),
            _entry("glass-treatment-frosted", "glass_treatment", "FROSTED", "Frosted / Etched"),
            _entry("glass-treatment-low-e", "glass_treatment", "LOW_E", "Low-E Coating"),
            _entry("glass-treatment-reflective", "glass_treatment", "REFLECTIVE", "Reflective Coating"),
            _entry("glass-treatment-tinted", "glass_treatment", "TINTED", "Tinted"),
            _entry("hardware-handle", "hardware", "HANDLE", "Handle / Pull"),
            _entry("hardware-lock", "hardware", "LOCK", "Lock"),
            _entry("hardware-multipoint-lock", "hardware", "MULTIPOINT_LOCK", "Multi-point Lock"),
            _entry("hardware-roller", "hardware", "ROLLER", "Roller Assembly"),
            _entry("hardware-hinge", "hardware", "HINGE", "Hinge Set"),
            _entry("hardware-closer", "hardware", "CLOSER", "Door Closer"),
            _entry("accessory-screen", "accessory", "SCREEN", "Insect Screen"),
            _entry("accessory-weather-seal", "accessory", "WEATHER_SEAL", "Weather Seal / Gasket"),
            _entry("accessory-drainage", "accessory", "DRAINAGE", "Drainage / Weep Kit"),
            _entry("accessory-trim", "accessory", "TRIM", "Interior / Exterior Trim"),
            _entry("accessory-anchor-kit", "accessory", "ANCHOR_KIT", "Anchor / Fastener Kit"),
        ],
        metadata={
            "note": "Bundled compatibility baseline; vendor SKUs/costs are intentionally absent until authoritative data is published."
        },
    ),
)


APPLICATION_CATALOG_ENTRIES = [
    ReferenceEntry(
        id="sector-aluminum-glass-steel",
        kind="sector",
        code="ALUMINUM_GLASS_STEEL",
        name="Aluminum, Glass & Steel",
        attributes={
            "description": "Architectural aluminum, glass, steel and metal construction elements."
        },
    ),
    ReferenceEntry(id="item-type-window", kind="item_type", code="WINDOW", name="Window", attributes={"sectorID": "sector-aluminum-glass-steel", "sortOrder": 10}),
    ReferenceEntry(id="item-type-door", kind="item_type", code="DOOR", name="Door", attributes={"sectorID": "sector-aluminum-glass-steel", "sortOrder": 20}),
    ReferenceEntry(id="item-type-bathroom-division", kind="item_type", code="BATHROOM_DIVISION", name="Bathroom Division", attributes={"sectorID": "sector-aluminum-glass-steel", "sortOrder": 30}),
    ReferenceEntry(id="item-type-office-division", kind="item_type", code="OFFICE_DIVISION", name="Office Division", attributes={"sectorID": "sector-aluminum-glass-steel", "sortOrder": 40}),
    ReferenceEntry(id="item-type-facade", kind="item_type", code="FACADE", name="Facade", attributes={"sectorID": "sector-aluminum-glass-steel", "sortOrder": 50}),
    ReferenceEntry(id="item-type-balcony-railing", kind="item_type", code="BALCONY_RAILING", name="Balcony / Railing", attributes={"sectorID": "sector-aluminum-glass-steel", "sortOrder": 60}),
    ReferenceEntry(id="item-type-other", kind="item_type", code="OTHER", name="Other", attributes={"sectorID": "sector-aluminum-glass-steel", "sortOrder": 70}),
]

BASELINE_CATALOG_V2 = ReferencePublicationCreate(
    publicationID="catalog-bundled-v2",
    kind="catalog",
    versionID="configurator-catalog-v2",
    effectiveFrom=APPLICATION_CATALOG_EFFECTIVE_FROM,
    supersedesPublicationID="catalog-bundled-v1",
    payload=ReferencePublicationPayload(
        source="bundled-ios-v1+application-catalog-v2",
        authoritative=False,
        entries=[*BASELINE_CATALOG.payload.entries, *APPLICATION_CATALOG_ENTRIES],
        metadata={
            "note": "Adds typed configurable Sector and Item Type entries required by SCRUM-1/SCRUM-5 while preserving the V1 technical catalog IDs. Vendor SKUs/costs remain intentionally absent until authoritative data is published."
        },
    ),
)


BASELINE_RULES = ReferencePublicationCreate(
    publicationID="compatibility-rules-bundled-v1",
    kind="compatibility_rules",
    versionID="configurator-rules-v1",
    effectiveFrom=BASELINE_EFFECTIVE_FROM,
    payload=ReferencePublicationPayload(
        source="bundled-ios-v1",
        authoritative=False,
        compatibilityRules=[
            CompatibilityRule(
                id="rule-item-type-window-v1",
                itemTypeID="item-type-window",
                availableComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor", "glassType", "glassThickness", "glassColor", "glassTreatment", "hardware", "accessories", "openingDirection"],
                requiredComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor", "glassType", "glassThickness"],
                allowedSectionTypes=["fixed", "sliding", "projected"],
            ),
            CompatibilityRule(
                id="rule-item-type-door-v1",
                itemTypeID="item-type-door",
                availableComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor", "glassType", "glassThickness", "glassColor", "glassTreatment", "hardware", "accessories", "openingDirection"],
                requiredComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor"],
                allowedSectionTypes=["sliding", "swing", "fixed"],
            ),
            CompatibilityRule(
                id="rule-item-type-bathroom-division-v1",
                itemTypeID="item-type-bathroom-division",
                availableComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor", "glassType", "glassThickness", "glassColor", "glassTreatment", "hardware", "accessories", "openingDirection"],
                requiredComponents=["vanoDimensions", "structure", "glassType", "glassThickness"],
                allowedSectionTypes=["fixed", "sliding", "swing"],
            ),
            CompatibilityRule(
                id="rule-item-type-office-division-v1",
                itemTypeID="item-type-office-division",
                availableComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor", "glassType", "glassThickness", "glassColor", "glassTreatment", "hardware", "accessories", "openingDirection"],
                requiredComponents=["vanoDimensions", "structure"],
                allowedSectionTypes=["fixed", "sliding", "swing"],
            ),
            CompatibilityRule(
                id="rule-item-type-facade-v1",
                itemTypeID="item-type-facade",
                availableComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor", "glassType", "glassThickness", "glassColor", "glassTreatment", "hardware", "accessories"],
                requiredComponents=["vanoDimensions", "structure"],
                allowedSectionTypes=["fixed"],
            ),
            CompatibilityRule(
                id="rule-item-type-balcony-railing-v1",
                itemTypeID="item-type-balcony-railing",
                availableComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor", "glassType", "glassThickness", "glassColor", "glassTreatment", "hardware", "accessories"],
                requiredComponents=["vanoDimensions", "structure"],
                allowedSectionTypes=["fixed"],
            ),
            CompatibilityRule(
                id="rule-item-type-other-v1",
                itemTypeID="item-type-other",
                availableComponents=["vanoDimensions", "structure", "aluminumSystem", "aluminumProfile", "aluminumColor", "glassType", "glassThickness", "glassColor", "glassTreatment", "hardware", "accessories", "openingDirection"],
                requiredComponents=["vanoDimensions"],
                allowedSectionTypes=["fixed", "sliding", "projected", "swing", "other"],
            ),
        ],
    ),
)


BASELINE_PRICE_BOOK = ReferencePublicationCreate(
    publicationID="price-book-unconfigured-v1",
    kind="price_book",
    versionID="price-book-unconfigured-v1",
    effectiveFrom=BASELINE_EFFECTIVE_FROM,
    payload=ReferencePublicationPayload(
        source="system-baseline",
        authoritative=False,
        priceBookLines=[],
        metadata={
            "configured": False,
            "note": "No authoritative vendor pricing has been supplied; no costs or prices are fabricated."
        },
    ),
)

BASELINE_PUBLICATIONS = (
    BASELINE_CATALOG,
    BASELINE_CATALOG_V2,
    BASELINE_RULES,
    BASELINE_PRICE_BOOK,
)


def _canonical_material(request: ReferencePublicationCreate) -> bytes:
    material = request.model_dump(mode="json", exclude_none=False)
    return json.dumps(material, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def publication_sha256(request: ReferencePublicationCreate) -> str:
    return hashlib.sha256(_canonical_material(request)).hexdigest()


def _to_response(model: ReferencePublication) -> ReferencePublicationResponse:
    return ReferencePublicationResponse(
        publicationID=model.publication_id,
        kind=model.kind,
        versionID=model.version_id,
        effectiveFrom=model.effective_from,
        effectiveUntil=model.effective_until,
        supersedesPublicationID=model.supersedes_publication_id,
        contentSHA256=model.content_sha256,
        publishedByUserID=model.published_by_user_id,
        publishedAt=model.published_at,
        payload=ReferencePublicationPayload.model_validate(model.payload_json),
    )


def _summary(model: ReferencePublication) -> ReferencePublicationSummary:
    return ReferencePublicationSummary(
        publicationID=model.publication_id,
        kind=model.kind,
        versionID=model.version_id,
        effectiveFrom=model.effective_from,
        effectiveUntil=model.effective_until,
        contentSHA256=model.content_sha256,
        publishedAt=model.published_at,
    )


def _require_manage_capability(principal: Principal, kind: PublicationKind) -> None:
    required = "pricing.manage" if kind == "price_book" else "catalog.manage"
    if required not in principal.capabilities:
        raise PermissionError(f"{required} capability required")


async def _insert_publication(
    db: AsyncSession,
    organization_id: str,
    request: ReferencePublicationCreate,
    *,
    published_by_user_id: str,
) -> ReferencePublication:
    if request.effectiveUntil is not None and request.effectiveUntil <= request.effectiveFrom:
        raise PublicationConflict("effectiveUntil must be later than effectiveFrom")

    digest = publication_sha256(request)
    existing = await db.get(ReferencePublication, (organization_id, request.publicationID))
    if existing is not None:
        if existing.content_sha256 == digest:
            return existing
        raise PublicationConflict("publicationID is immutable and already refers to different content")

    version_owner = await db.scalar(
        select(ReferencePublication).where(
            ReferencePublication.organization_id == organization_id,
            ReferencePublication.kind == request.kind,
            ReferencePublication.version_id == request.versionID,
        )
    )
    if version_owner is not None:
        raise PublicationConflict("versionID is already published for this reference-data kind")

    if request.supersedesPublicationID is not None:
        predecessor = await db.get(
            ReferencePublication,
            (organization_id, request.supersedesPublicationID),
        )
        if predecessor is None:
            raise PublicationConflict("supersedesPublicationID does not exist")
        if predecessor.kind != request.kind:
            raise PublicationConflict("a publication may supersede only the same reference-data kind")

    model = ReferencePublication(
        organization_id=organization_id,
        publication_id=request.publicationID,
        kind=request.kind,
        version_id=request.versionID,
        effective_from=request.effectiveFrom,
        effective_until=request.effectiveUntil,
        supersedes_publication_id=request.supersedesPublicationID,
        payload_json=request.payload.model_dump(mode="json"),
        content_sha256=digest,
        published_by_user_id=published_by_user_id,
    )
    db.add(model)
    await db.flush()
    return model


async def ensure_baseline_publications(db: AsyncSession, principal: Principal) -> None:
    created = False
    for request in BASELINE_PUBLICATIONS:
        existing = await db.get(
            ReferencePublication,
            (principal.organization_id, request.publicationID),
        )
        if existing is None:
            await _insert_publication(
                db,
                principal.organization_id,
                request,
                published_by_user_id="system:baseline",
            )
            created = True
    if created:
        await db.commit()
        log_event(
            "reference.baseline_seeded",
            organizationRef=correlation_ref(principal.organization_id),
            publicationCount=len(BASELINE_PUBLICATIONS),
        )


async def publish_reference(
    db: AsyncSession,
    principal: Principal,
    request: ReferencePublicationCreate,
) -> ReferencePublicationResponse:
    _require_manage_capability(principal, request.kind)
    await ensure_baseline_publications(db, principal)
    model = await _insert_publication(
        db,
        principal.organization_id,
        request,
        published_by_user_id=principal.user_id,
    )
    await db.commit()
    await db.refresh(model)
    log_event(
        "reference.publication_published",
        organizationRef=correlation_ref(principal.organization_id),
        actorRef=correlation_ref(principal.user_id),
        publicationRef=correlation_ref(model.publication_id),
        publicationKind=model.kind,
        versionRef=correlation_ref(model.version_id),
        effectiveFrom=model.effective_from.isoformat(),
    )
    return _to_response(model)


async def get_publication(
    db: AsyncSession,
    principal: Principal,
    publication_id: str,
) -> ReferencePublicationResponse:
    await ensure_baseline_publications(db, principal)
    model = await db.get(ReferencePublication, (principal.organization_id, publication_id))
    if model is None:
        raise PublicationNotFound(publication_id)
    return _to_response(model)


async def _list_publications(db: AsyncSession, principal: Principal, kind: PublicationKind | None) -> ReferencePublicationListResponse:
    query = select(ReferencePublication).where(
        ReferencePublication.organization_id == principal.organization_id
    )
    if kind is not None:
        query = query.where(ReferencePublication.kind == kind)
    rows = (
        await db.scalars(
            query.order_by(
                ReferencePublication.kind.asc(),
                ReferencePublication.effective_from.desc(),
                ReferencePublication.published_at.desc(),
            )
        )
    ).all()
    return ReferencePublicationListResponse(publications=[_to_response(row) for row in rows])


async def list_publications(
    db: AsyncSession,
    principal: Principal,
    kind: PublicationKind | None = None,
) -> ReferencePublicationListResponse:
    await ensure_baseline_publications(db, principal)
    return await _list_publications(db, principal, kind)


async def current_publications(
    db: AsyncSession,
    principal: Principal,
    *,
    at: datetime | None = None,
) -> ReferencePublicationListResponse:
    await ensure_baseline_publications(db, principal)
    when = at or datetime.now(timezone.utc)
    rows = (
        await db.scalars(
            select(ReferencePublication)
            .where(
                ReferencePublication.organization_id == principal.organization_id,
                ReferencePublication.effective_from <= when,
                or_(
                    ReferencePublication.effective_until.is_(None),
                    ReferencePublication.effective_until > when,
                ),
            )
            .order_by(
                ReferencePublication.kind.asc(),
                ReferencePublication.effective_from.desc(),
                ReferencePublication.published_at.desc(),
            )
        )
    ).all()
    selected: dict[str, ReferencePublication] = {}
    for row in rows:
        selected.setdefault(row.kind, row)
    return ReferencePublicationListResponse(
        publications=[_to_response(selected[key]) for key in sorted(selected)]
    )


async def publication_manifest(
    db: AsyncSession,
    principal: Principal,
) -> ReferencePublicationManifest:
    available_response = await list_publications(db, principal)
    current_response = await current_publications(db, principal)
    return ReferencePublicationManifest(
        current=[
            ReferencePublicationSummary(
                publicationID=p.publicationID,
                kind=p.kind,
                versionID=p.versionID,
                effectiveFrom=p.effectiveFrom,
                effectiveUntil=p.effectiveUntil,
                contentSHA256=p.contentSHA256,
                publishedAt=p.publishedAt,
            )
            for p in current_response.publications
        ],
        available=[
            ReferencePublicationSummary(
                publicationID=p.publicationID,
                kind=p.kind,
                versionID=p.versionID,
                effectiveFrom=p.effectiveFrom,
                effectiveUntil=p.effectiveUntil,
                contentSHA256=p.contentSHA256,
                publishedAt=p.publishedAt,
            )
            for p in available_response.publications
        ],
    )
