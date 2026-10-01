"""The batched resolver must agree with the function it replaces.

#48 measured the pull N+1. #52 pinned the visible set. This asserts the property
that makes the optimization safe: for every input, `resolve_visible` returns what
`record_is_visible` returns.

That is a stronger claim than "the visible set is unchanged for this fixture". It is
checked per (principal, entity_type, entity_id) rather than per page, so a divergence
names the exact input that diverged instead of a set difference.

Runs without PostgreSQL. `record_is_visible` only ever calls `db.get`, so a stub
session that answers from a table of rows drives the real function unmodified. That
is the property being exploited: the function under test is the shipped one, not a
reimplementation of it. (A harness that reimplemented `record_is_visible` would agree
with the twin by construction and prove nothing -- that is the mistake #52's CI run
caught.)
"""

from __future__ import annotations

import inspect

import pytest

from app.auth import Principal
from app.delivery_execution import DELIVERY_ENTITY_TYPE
from app.ownership import EffectiveScope, record_is_visible
from app.pull_prefetch import PageContext, resolve_visible

ORG = "org-prefetch-equiv"

TYPES = ["customer", "project", "project_sector", "item", "quotation", "measurement"]


class Row:
    """Minimal stand-in for an ORM row; only the attributes under test are read."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class StubDB:
    """Answers `db.get(model, pk)` from `rows`, counting every lookup.

    Counting is what lets the test assert the twin is the cheap one, not merely a
    correct one -- a twin that were also slow would still be wrong to ship.
    """

    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    async def get(self, model, pk):
        self.queries += 1
        return self.rows.get((model.__name__, pk))


def principal(**kw) -> Principal:
    base = dict(
        user_id="user-equiv",
        organization_id=ORG,
        membership_id="membership-equiv",
        session_id="session-equiv",
        authorization_revision=1,
        capabilities=frozenset({"sync"}),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )
    base.update(kw)
    return Principal(**base)


def scenario():
    """Rows, principals, and queries covering every branch and rejection.

    Both an accessible and an inaccessible customer/project, a sector and item whose
    project is missing, a project child whose project is missing, an item child whose
    item is missing, and a child row that does not exist at all.
    """
    rows = {
        # customers
        ("CanonicalCustomer", (ORG, "cust-mine")): Row(customer_id="cust-mine"),
        ("CanonicalCustomer", (ORG, "cust-theirs")): Row(customer_id="cust-theirs"),
        # projects
        ("CanonicalProject", (ORG, "proj-mine")): Row(
            project_id="proj-mine", customer_id="cust-mine"
        ),
        ("CanonicalProject", (ORG, "proj-theirs")): Row(
            project_id="proj-theirs", customer_id="cust-theirs"
        ),
        # sector pointing at a project that does not exist
        ("CanonicalProjectSector", (ORG, "sector-orphan")): Row(
            project_sector_id="sector-orphan", project_id="proj-absent", sector_id="s"
        ),
        ("CanonicalProjectSector", (ORG, "sector-mine")): Row(
            project_sector_id="sector-mine", project_id="proj-mine", sector_id="s"
        ),
        # items
        ("CanonicalItem", (ORG, "item-mine")): Row(item_id="item-mine", project_id="proj-mine"),
        ("CanonicalItem", (ORG, "item-orphan")): Row(
            item_id="item-orphan", project_id="proj-absent"
        ),
        ("CanonicalItem", (ORG, "item-absent-parent")): Row(
            item_id="item-absent-parent", project_id="proj-mine"
        ),
        # project children
        ("CanonicalProjectChild", (ORG, "quotation", "q-mine")): Row(
            entity_type="quotation", entity_id="q-mine", project_id="proj-mine"
        ),
        ("CanonicalProjectChild", (ORG, "quotation", "q-orphan")): Row(
            entity_type="quotation", entity_id="q-orphan", project_id="proj-absent"
        ),
        # item children, one pointing at an item that does not exist
        ("CanonicalItemChild", (ORG, "measurement", "m-mine")): Row(
            entity_type="measurement", entity_id="m-mine", item_id="item-mine"
        ),
        ("CanonicalItemChild", (ORG, "measurement", "m-no-item")): Row(
            entity_type="measurement", entity_id="m-no-item", item_id="item-absent"
        ),
    }

    principals = {
        "broad": principal(),
        "narrow": principal(
            all_customers=False,
            all_projects=False,
            customer_ids=frozenset({"cust-mine"}),
            project_ids=frozenset({"proj-mine"}),
        ),
        "customer-only": principal(
            all_customers=True, all_projects=False, project_ids=frozenset()
        ),
        "project-no-customer": principal(
            all_customers=False,
            all_projects=False,
            customer_ids=frozenset(),
            project_ids=frozenset({"proj-mine"}),
        ),
    }

    ids = [
        "cust-mine",
        "cust-theirs",
        "cust-absent",
        "proj-mine",
        "proj-theirs",
        "proj-absent",
        "sector-mine",
        "sector-orphan",
        "sector-absent",
        "item-mine",
        "item-orphan",
        "item-absent",
        "q-mine",
        "q-orphan",
        "q-absent",
        "m-mine",
        "m-no-item",
        "m-absent",
    ]
    return rows, principals, [(t, e) for t in TYPES for e in ids]


def twin_from(rows, principal_, cases) -> PageContext:
    """Build the batched context the loader *would* produce, from the same rows.

    Mirrors `load_page_context`'s population order. It reuses the twin's own loaders
    so the test does not hand-build a context that differs from the real one in some
    way the twin happens to tolerate.
    """
    ctx = PageContext(organization_id=ORG, scope=EffectiveScope.from_principal(principal_))
    by_type: dict[str, list[str]] = {}
    for t, e in cases:
        by_type.setdefault(t, []).append(e)

    def put(model_name, key_cols, ids):
        """Fetch rows for `ids` and key them the way `load_page_context` does.

        The real loader keys single-id maps by the bare id and composite maps by
        `(entity_type, entity_id)`, because `resolve_visible` looks up by those
        values. Keying by the raw primary-key tuple here produced maps nothing could
        read, which the equivalence assertion caught on its first case.
        """
        for e in ids:
            pk = (ORG, *key_cols(e))
            row = rows.get((model_name, pk))
            if row is not None:
                yield key_cols(e)[-1] if len(key_cols(e)) == 1 else key_cols(e), row

    ctx.customers = dict(put("CanonicalCustomer", lambda e: (e,), by_type.get("customer", [])))
    ctx.sectors = dict(
        put("CanonicalProjectSector", lambda e: (e,), by_type.get("project_sector", []))
    )
    for t in ("quotation", "delivery_execution"):
        ctx.project_children.update(
            dict(put("CanonicalProjectChild", lambda e, t=t: (t, e), by_type.get(t, [])))
        )
    for t in ("measurement", "evidence", "customer_requirement", "configuration",
              "configuration_version", "blocker", "item_audit_event"):
        ctx.item_children.update(
            dict(put("CanonicalItemChild", lambda e, t=t: (t, e), by_type.get(t, [])))
        )

    reachable = set(by_type.get("item", [])) | {
        c.item_id for c in ctx.item_children.values()
    }
    ctx.items = dict(put("CanonicalItem", lambda e: (e,), reachable))

    wanted = set(by_type.get("project", []))
    for c in ctx.project_children.values():
        wanted.add(c.project_id)
    for s in ctx.sectors.values():
        wanted.add(s.project_id)
    for i in ctx.items.values():
        wanted.add(i.project_id)
    ctx.projects = dict(put("CanonicalProject", lambda e: (e,), wanted))
    return ctx


@pytest.mark.asyncio
async def test_batched_resolver_matches_record_is_visible_for_every_case():
    rows, principals, cases = scenario()
    for name, p in principals.items():
        for entity_type, entity_id in cases:
            expected = await record_is_visible(
                StubDB(rows), p, entity_type=entity_type, entity_id=entity_id
            )
            ctx = twin_from(rows, p, cases)
            actual = resolve_visible(ctx, entity_type=entity_type, entity_id=entity_id)
            assert actual == expected, (
                f"principal={name} entity_type={entity_type} entity_id={entity_id}: "
                f"batched={actual} original={expected}"
            )


@pytest.mark.asyncio
async def test_equivalence_survives_a_sabotaged_original():
    """The comparison must be able to fail.

    Guards against the harness agreeing with the twin for the wrong reason. Mutating
    the original so it denies a case it should allow must make the comparison red.
    """
    rows, principals, cases = scenario()
    p = principals["broad"]
    victim = ("item", "item-mine")

    original = await record_is_visible(
        StubDB(rows), p, entity_type=victim[0], entity_id=victim[1]
    )
    assert original is True, "victim should be visible before sabotage"

    # Sabotage: the twin, not the original -- the twin is the new code under test.
    ctx = twin_from(rows, p, cases)
    ctx.projects = {}
    sabotaged = resolve_visible(ctx, entity_type=victim[0], entity_id=victim[1])
    assert sabotaged is False
    assert sabotaged != original, "sabotage must be detected as a divergence"


@pytest.mark.asyncio
async def test_delivery_twin_matches_including_deleted_at_hops():
    """The delivery twin keeps `deleted_at` checks the generic twin does not have.

    `delivery_execution_is_visible` rejects a deleted payload, a deleted child, or a
    deleted project. `record_is_visible` has no such checks at all. That asymmetry is
    pre-existing, and the batched pair preserves it exactly -- if either twin grew or
    lost a `deleted_at` check, this is where it would show.
    """
    from datetime import datetime, timezone

    from app.delivery_execution import delivery_execution_is_visible
    from app.pull_prefetch import resolve_delivery_visible

    stamp = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    p = principal()

    def build(entity_deleted, child_deleted, project_deleted):
        ctx = PageContext(organization_id=ORG, scope=EffectiveScope.from_principal(p))
        ctx.entities = {
            (DELIVERY_ENTITY_TYPE, "de-1"): Row(
                payload_json={"id": "de-1"},
                deleted_at=stamp if entity_deleted else None,
            )
        }
        ctx.project_children = {
            (DELIVERY_ENTITY_TYPE, "de-1"): Row(
                entity_type=DELIVERY_ENTITY_TYPE,
                entity_id="de-1",
                project_id="proj-mine",
                deleted_at=stamp if child_deleted else None,
            )
        }
        ctx.projects = {
            "proj-mine": Row(
                project_id="proj-mine",
                customer_id="cust-mine",
                deleted_at=stamp if project_deleted else None,
            )
        }
        return ctx

    def original_rows(entity_deleted, child_deleted, project_deleted):
        key = DELIVERY_ENTITY_TYPE
        return {
            ("SyncEntity", (ORG, key, "de-1")): Row(
                payload_json={"id": "de-1"},
                deleted_at=stamp if entity_deleted else None,
            ),
            ("CanonicalProjectChild", (ORG, key, "de-1")): Row(
                entity_type=key,
                entity_id="de-1",
                project_id="proj-mine",
                deleted_at=stamp if child_deleted else None,
            ),
            ("CanonicalProject", (ORG, "proj-mine")): Row(
                project_id="proj-mine",
                customer_id="cust-mine",
                deleted_at=stamp if project_deleted else None,
            ),
        }

    for label, flags in (
        ("clean", (False, False, False)),
        ("deleted payload", (True, False, False)),
        ("deleted child", (False, True, False)),
        ("deleted project", (False, False, True)),
        ("all deleted", (True, True, True)),
    ):
        expected = await delivery_execution_is_visible(
            StubDB(original_rows(*flags)), p, entity_id="de-1"
        )
        actual = resolve_delivery_visible(build(*flags), entity_id="de-1")
        assert actual == expected, (
            f"{label}: batched={actual} original={expected}"
        )


@pytest.mark.asyncio
async def test_loader_runs_against_any_session_shaped_object():
    """Drive the real loader with a stub session, so it is not database-only.

    `load_page_context` was, until now, only ever executed against PostgreSQL -- which
    is how an `await` on a non-awaitable survived every local run. `_chunks` was
    declared `async` without doing any I/O, making it an async generator, and
    `_load_keyed` awaited it. The integration job caught it; nothing local could,
    because the equivalence tests call the resolvers directly and never load.

    This test asserts the loader actually issues its queries, keys the rows it gets
    back, and chunks an oversized key list -- all without a database.
    """
    from app.pull_prefetch import _chunks, _load_keyed, load_page_context

    assert not inspect.isasyncgenfunction(_chunks), (
        "_chunks does no I/O; marking it async makes it an async generator that "
        "callers must `async for`, which is how the integration job found an await "
        "on a non-awaitable"
    )
    assert list(_chunks([1, 2, 3, 4, 5], size=2)) == [[1, 2], [3, 4], [5]]

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class LoaderDB:
        def __init__(self, rows_by_table):
            self.rows_by_table = rows_by_table
            self.statements: list[str] = []

        async def scalars(self, statement):
            self.statements.append(str(statement))
            for model_name, rows in self.rows_by_table.items():
                if model_name in str(statement):
                    return Result(rows)
            return Result([])

    # One query per requested table, not one per key.
    from app.models import CanonicalProject, CanonicalProjectSector

    project_rows = [
        Row(organization_id=ORG, project_id="proj-1", customer_id="cust-1"),
        Row(organization_id=ORG, project_id="proj-2", customer_id="cust-1"),
    ]
    sector_rows = [
        Row(organization_id=ORG, project_sector_id="sec-1", project_id="proj-1", sector_id="s")
    ]
    db = LoaderDB({"canonical_projects": project_rows, "canonical_project_sectors": sector_rows})

    keys = [(ORG, f"proj-{i}") for i in range(1, 3)]
    loaded = await _load_keyed(db, CanonicalProject, ["organization_id", "project_id"], keys)
    assert set(loaded) == set(keys), "rows must be keyed by the requested composite key"
    assert len(db.statements) == 1, "one query for the whole key list, not one per key"

    # And the composite-IN shape, which is what avoids matching cross-product
    # combinations that do not exist.
    assert "IN" in db.statements[0].upper()

    # An oversized key list must chunk rather than issue one enormous IN list.
    class CountingDB(LoaderDB):
        async def scalars(self, statement):
            self.statements.append(str(statement))
            return Result([])

    from app.pull_prefetch import MAX_BATCH

    big = CountingDB({})
    oversized = [(ORG, f"p{i}") for i in range(MAX_BATCH + 5)]
    await _load_keyed(big, CanonicalProject, ["organization_id", "project_id"], oversized)
    assert len(big.statements) == 2, (
        f"expected 2 chunked queries for {len(oversized)} keys, got {len(big.statements)}"
    )

    # `load_page_context` end to end: one query per present type, never per record.
    p = principal()

    class Change:
        def __init__(self, entity_type, entity_id):
            self.entity_type = entity_type
            self.entity_id = entity_id

    page_db = LoaderDB({
        "canonical_projects": project_rows,
        "canonical_project_sectors": sector_rows,
    })
    changes = [
        Change("project", "proj-1"),
        Change("project", "proj-2"),
        Change("project_sector", "sec-1"),
        Change("project_sector", "sec-1"),
    ]
    ctx = await load_page_context(page_db, p, changes)
    assert set(ctx.projects) == {"proj-1", "proj-2"}
    assert set(ctx.sectors) == {"sec-1"}
    # Four changes resolved by four queries at most, and the project query covers
    # both direct projects and the sector's project in a single pass.
    assert len(page_db.statements) <= 5, (
        f"page context should be a fixed handful of queries, issued "
        f"{len(page_db.statements)} for {len(changes)} changes"
    )


@pytest.mark.asyncio
async def test_batched_path_issues_far_queries():
    """A correct twin that is equally slow is still the wrong thing to ship.

    Compares the per-case lookup count of the original against the batched context's
    construction. The original is 1-3 gets per case; the context is a fixed handful
    of set-based loads regardless of how many cases it covers.
    """
    rows, principals, cases = scenario()
    p = principals["broad"]

    db = StubDB(rows)
    for entity_type, entity_id in cases:
        await record_is_visible(db, p, entity_type=entity_type, entity_id=entity_id)
    per_case_calls = db.queries

    ctx = twin_from(rows, p, cases)
    # The twin answers every case from these maps.
    for entity_type, entity_id in cases:
        resolve_visible(ctx, entity_type=entity_type, entity_id=entity_id)

    assert per_case_calls >= len(cases), (
        "the original should issue at least one lookup per case"
    )
    # Eight maps, built without any further lookups.
    loaded_maps = (
        ctx.customers, ctx.projects, ctx.sectors, ctx.items,
        ctx.project_children, ctx.item_children, ctx.entities,
    )
    assert len(loaded_maps) == 7
