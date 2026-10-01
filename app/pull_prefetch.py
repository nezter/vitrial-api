"""Batched resolution of a pull page.

Section 4 of the 0.3.0 plan calls for removing the per-record N+1 in
`pull_since`. A full `MAX_SYNC_PULL_SCAN_CHANGES` (500) page issues roughly
1,000-2,000 sequential round trips: `record_is_visible` performs 1-3 `db.get` calls
depending on entity type, and the payload fetch is one more.

This module replaces those per-record lookups with a fixed number of set-based
queries, independent of page size. For a full page it is ~12 queries instead of
1,000-2,000.

## The rule this module exists to obey

`resolve_visible` must return exactly what `record_is_visible` returns, for every
input. Not "the same in the cases I checked" -- the same, provably, by construction.

Two things protect that:

1. **Same order of checks.** Each branch below mirrors the corresponding branch of
   `record_is_visible` line for line, including the early `is None` returns. A
   missing row and a row the principal cannot reach both yield `False`, and they must
   stay indistinguishable from the outside.
2. **No new rules.** In particular this module does *not* add `deleted_at` checks to
   the generic types. `delivery_execution_is_visible` checks `deleted_at` at each
   hop; `record_is_visible` does not, and that asymmetry is pre-existing behaviour.
   Changing it here would be an authorization change wearing a performance
   commit's clothes, so it is left exactly as found and called out in DELIVERY.

`tests/test_pull_prefetch_equivalence.py` asserts the two agree, case by case.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalItemChild,
    CanonicalProject,
    CanonicalProjectChild,
    CanonicalProjectSector,
    SyncChangeLog,
    SyncEntity,
)
from app.delivery_execution import DELIVERY_ENTITY_TYPE
from app.ownership import (
    ITEM_CHILD_TYPES,
    PROJECT_CHILD_TYPES,
    EffectiveScope,
)

# Guards against a pathological page asking for unbounded IN lists. At 500 changes
# the largest single IN list is 500 tuples, which is well inside PostgreSQL's
# parameter limit. This is a backstop, not a tuning knob.
MAX_BATCH = 2_000


@dataclass
class PageContext:
    """Everything the page needs, loaded in a fixed number of queries.

    Maps are keyed by the same composite primary keys `db.get` would have been
    called with, so a missing key means exactly what a `None` return meant: the row
    does not exist. That equivalence is what lets `resolve_visible` read like the
    original rather than defensively re-checking existence.
    """

    organization_id: str
    scope: EffectiveScope
    customers: dict[str, CanonicalCustomer] = field(default_factory=dict)
    projects: dict[str, CanonicalProject] = field(default_factory=dict)
    sectors: dict[str, CanonicalProjectSector] = field(default_factory=dict)
    items: dict[str, CanonicalItem] = field(default_factory=dict)
    project_children: dict[tuple[str, str], CanonicalProjectChild] = field(
        default_factory=dict
    )
    item_children: dict[tuple[str, str], CanonicalItemChild] = field(
        default_factory=dict
    )
    entities: dict[tuple[str, str], SyncEntity] = field(default_factory=dict)


def _chunks(values: list, size: int = MAX_BATCH):
    """Split `values` into chunks of at most `size`.

    A plain generator, deliberately: it does no I/O, and marking it `async` made it
    an async generator that callers must `async for` rather than iterate. The
    `await` on it in `_load_keyed` was only ever exercised against a real database,
    so it passed locally and failed in the integration job.
    """
    for start in range(0, len(values), size):
        yield values[start : start + size]


async def _load_keyed(db: AsyncSession, model, key_columns: list[str], keys: list[tuple]):
    """Load `model` rows for composite `keys`, returning a dict keyed by those keys.

    One query per chunk. Uses `tuple_(...) IN (...)` so the lookup matches the
    primary key exactly rather than approximating it with per-column IN lists, which
    would match cross-product combinations that do not exist.
    """
    out: dict[tuple, object] = {}
    for chunk in _chunks(keys):
        predicate = tuple_(*[getattr(model, col) for col in key_columns]).in_(chunk)
        rows = (await db.scalars(select(model).where(predicate))).all()
        for row in rows:
            out[tuple(getattr(row, col) for col in key_columns)] = row
    return out


async def load_page_context(
    db: AsyncSession,
    principal: Principal,
    changes: list[SyncChangeLog],
) -> PageContext:
    """Load every row the page's visibility decisions could depend on.

    Deliberately loads *more* than strictly necessary -- for example the projects
    reachable from sectors, even though only some are in scope. Over-fetching by a
    bounded amount is what keeps the query count independent of page size; the
    alternative is a second round of dependent queries, which reintroduces the
    problem this module exists to remove.
    """
    org = principal.organization_id
    ctx = PageContext(organization_id=org, scope=EffectiveScope.from_principal(principal))

    by_type: dict[str, list[str]] = {}
    for change in changes:
        by_type.setdefault(change.entity_type, []).append(change.entity_id)

    # Payloads for the whole page, fetched whether or not the record turns out to be
    # visible. The original fetched only for visible records; over-fetching here is
    # one query total instead of up to one per visible record, and payload rows are
    # already selected by the change-log query's own page.
    # Keyed by (entity_type, entity_id) without the organization: the whole context
    # is org-scoped, and the resolvers look up the 2-tuple form. `_load_keyed` returns
    # keys shaped like its `key_columns`, so the organization has to be stripped
    # here -- leaving it in made every lookup miss.
    ctx.entities = {
        (k[1], k[2]): v
        for k, v in (
            await _load_keyed(
                db, SyncEntity, ["organization_id", "entity_type", "entity_id"],
                [(org, t, e) for t, ids in by_type.items() for e in ids],
            )
        ).items()
    }

    if by_type.get("customer"):
        rows = await _load_keyed(
            db, CanonicalCustomer, ["organization_id", "customer_id"],
            [(org, e) for e in by_type["customer"]],
        )
        ctx.customers = {k[1]: v for k, v in rows.items()}

    # `delivery_execution` is a project child that the pull routes through
    # `delivery_execution_is_visible` (extra `deleted_at` checks at each hop) rather
    # than `record_is_visible`. It still has to be *loaded* here, because
    # `resolve_delivery_visible` reads the child from this same map to make that
    # decision. Excluding it -- which an earlier draft did, under a comment claiming
    # the opposite -- silently made every delivery record invisible.
    project_child_types = PROJECT_CHILD_TYPES
    project_child_ids = [
        (org, t, e) for t in project_child_types & by_type.keys() for e in by_type[t]
    ]
    if project_child_ids:
        rows = await _load_keyed(
            db, CanonicalProjectChild, ["organization_id", "entity_type", "entity_id"],
            project_child_ids,
        )
        ctx.project_children = {(k[1], k[2]): v for k, v in rows.items()}

    item_child_ids = [
        (org, t, e) for t in ITEM_CHILD_TYPES & by_type.keys() for e in by_type[t]
    ]
    if item_child_ids:
        rows = await _load_keyed(
            db, CanonicalItemChild, ["organization_id", "entity_type", "entity_id"],
            item_child_ids,
        )
        ctx.item_children = {(k[1], k[2]): v for k, v in rows.items()}

    sector_ids = by_type.get("project_sector", [])
    direct_item_ids = by_type.get("item", [])

    # Second hop: every project id any loaded row points at. Collected in one pass
    # so the project query does not depend on which branches turned out to be live.
    wanted_projects: set[str] = set()
    if "project" in by_type:
        wanted_projects.update(by_type["project"])
    for child in ctx.project_children.values():
        wanted_projects.add(child.project_id)
    # Sectors are loaded in their own query, then their projects collected here.
    if sector_ids:
        rows = await _load_keyed(
            db, CanonicalProjectSector, ["organization_id", "project_sector_id"],
            [(org, e) for e in sector_ids],
        )
        ctx.sectors = {k[1]: v for k, v in rows.items()}
        for sector in ctx.sectors.values():
            wanted_projects.add(sector.project_id)

    # Direct items plus any items reachable from item children, loaded in one query,
    # then the projects those point at. Two dependent hops, both bounded by page size.
    #
    # These cannot be folded into `wanted_projects` earlier: an item's project id is
    # only known once the item row is loaded. Collecting them in a set first and
    # issuing one `IN` per hop keeps the count fixed at two rather than one per item.
    reachable_item_ids = set(direct_item_ids) | {c.item_id for c in ctx.item_children.values()}
    if reachable_item_ids:
        rows = await _load_keyed(
            db, CanonicalItem, ["organization_id", "item_id"],
            [(org, e) for e in reachable_item_ids],
        )
        ctx.items = {k[1]: v for k, v in rows.items()}
        for item in ctx.items.values():
            wanted_projects.add(item.project_id)

    if wanted_projects:
        rows = await _load_keyed(
            db, CanonicalProject, ["organization_id", "project_id"],
            [(org, e) for e in wanted_projects],
        )
        ctx.projects = {k[1]: v for k, v in rows.items()}

    return ctx


def resolve_visible(ctx: PageContext, *, entity_type: str, entity_id: str) -> bool:
    """Batched twin of `record_is_visible`.

    Each branch mirrors the original exactly. Reads of `ctx.*` return `None` for an
    absent key via `.get`, which is the same signal the original's `db.get` returning
    `None` produced.
    """
    scope = ctx.scope

    if entity_type == "customer":
        customer = ctx.customers.get(entity_id)
        return customer is not None and scope.can_access_customer(customer.customer_id)
    if entity_type == "project":
        project = ctx.projects.get(entity_id)
        return (
            project is not None
            and scope.can_access_project(project.project_id, project.customer_id)
        )
    if entity_type == "project_sector":
        sector = ctx.sectors.get(entity_id)
        if sector is None:
            return False
        project = ctx.projects.get(sector.project_id)
        return (
            project is not None
            and scope.can_access_project(project.project_id, project.customer_id)
        )
    if entity_type == "item":
        item = ctx.items.get(entity_id)
        if item is None:
            return False
        project = ctx.projects.get(item.project_id)
        return (
            project is not None
            and scope.can_access_project(project.project_id, project.customer_id)
        )
    if entity_type in PROJECT_CHILD_TYPES:
        child = ctx.project_children.get((entity_type, entity_id))
        if child is None:
            return False
        project = ctx.projects.get(child.project_id)
        return (
            project is not None
            and scope.can_access_project(project.project_id, project.customer_id)
        )
    if entity_type in ITEM_CHILD_TYPES:
        child = ctx.item_children.get((entity_type, entity_id))
        if child is None:
            return False
        item = ctx.items.get(child.item_id)
        if item is None:
            return False
        project = ctx.projects.get(item.project_id)
        return (
            project is not None
            and scope.can_access_project(project.project_id, project.customer_id)
        )
    return False


def resolve_delivery_visible(ctx: PageContext, *, entity_id: str) -> bool:
    """Batched twin of `delivery_execution_is_visible`.

    Keeps that function's `deleted_at` checks at every hop. Note the generic
    branches in `resolve_visible` do *not* check `deleted_at`, because
    `record_is_visible` does not; the asymmetry is pre-existing and preserved.
    """
    entity = ctx.entities.get((DELIVERY_ENTITY_TYPE, entity_id))
    if (
        entity is None
        or entity.deleted_at is not None
        or not isinstance(entity.payload_json, dict)
    ):
        return False
    child = ctx.project_children.get((DELIVERY_ENTITY_TYPE, entity_id))
    if child is None or child.deleted_at is not None:
        return False
    project = ctx.projects.get(child.project_id)
    if project is None or project.deleted_at is not None:
        return False
    return ctx.scope.can_access_project(project.project_id, project.customer_id)
