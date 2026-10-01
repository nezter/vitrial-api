"""Query cost of a full pull page, measured rather than extrapolated.

#48 seeded 12 changes and reported a per-change figure; the "~167 queries for a
500-change page" quoted in #53 and #59 is that per-change number multiplied by 500. It
was never measured.

That extrapolation is exactly the kind of claim that turns out to be wrong, because a
batched implementation is not linear: the `IN` lists grow, `MAX_BATCH` chunking can
introduce additional queries, and the change-log page select is capped at
`MAX_SYNC_PULL_SCAN_CHANGES` regardless. So the multiplication is only valid if the
per-change cost is genuinely flat, which is precisely what this file checks.

Three page sizes, so the shape is visible rather than assumed:

* small   -- where the per-change bound in #48 applies comfortably
* large   -- an order of magnitude up, still one chunk
* ceiling -- `MAX_SYNC_PULL_SCAN_CHANGES` itself, the worst case that can be requested

The assertion is that cost is **sub-linear in page size**: a full page must cost fewer
queries per change than a small one, which is the property #53's prefetch was written to
deliver. If it ever becomes linear or worse, something has reintroduced a per-record
lookup.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, text

from app.auth import Principal
from app.db import SessionFactory
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalProject,
    Organization,
    SyncChangeLog,
    SyncEntity,
)
from app.schemas import SyncBatch
from app.sync_service import MAX_SYNC_PULL_SCAN_CHANGES, pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ORG = "org-pull-scale"
NOW = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)

SMALL = 12
LARGE = 120
CEILING = MAX_SYNC_PULL_SCAN_CHANGES


def principal() -> Principal:
    return Principal(
        user_id="user-pull-scale",
        organization_id=ORG,
        membership_id="membership-pull-scale",
        session_id="session-pull-scale",
        authorization_revision=1,
        capabilities=frozenset({"sync"}),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )


async def clear() -> None:
    async with SessionFactory() as db:
        for model in (SyncChangeLog, SyncEntity, CanonicalItem):
            await db.execute(
                delete(model).where(model.__table__.c.organization_id == ORG)
            )
        await db.flush()
        for model in (CanonicalProject, CanonicalCustomer):
            await db.execute(
                delete(model).where(model.__table__.c.organization_id == ORG)
            )
        await db.flush()
        await db.execute(delete(Organization).where(Organization.id == ORG))
        await db.execute(text("TRUNCATE sync_change_log RESTART IDENTITY CASCADE"))
        await db.commit()


async def seed(size: int) -> None:
    """Seed `size` items, each on its own project.

    One project per item, deliberately: a shared parent would let SQLAlchemy's identity
    map satisfy later lookups from cache, which flatters the count in the same way a
    reused row would.
    """
    async with SessionFactory() as db:
        db.add(Organization(id=ORG, name="Pull Scale", authorization_revision=1))
        await db.flush()
        db.add(CanonicalCustomer(organization_id=ORG, customer_id="customer-pull-scale"))
        await db.flush()
        for index in range(size):
            project_id = f"project-pull-scale-{index}"
            item_id = f"item-pull-scale-{index}"
            db.add(
                CanonicalProject(
                    organization_id=ORG, project_id=project_id,
                    customer_id="customer-pull-scale",
                )
            )
            await db.flush()
            db.add(
                CanonicalItem(
                    organization_id=ORG, item_id=item_id,
                    project_id=project_id, project_sector_id=None,
                )
            )
            db.add(
                SyncChangeLog(
                    organization_id=ORG, entity_type="item", entity_id=item_id,
                    server_revision=1, client_mutation_id=f"cm-{item_id}",
                    operation="upsert",
                )
            )
            db.add(
                SyncEntity(
                    organization_id=ORG, entity_type="item", entity_id=item_id,
                    server_revision=1, schema_version=1,
                    payload_json={"id": item_id}, updated_at=NOW,
                )
            )
        await db.commit()


class _Counter:
    def __init__(self) -> None:
        self.count = 0

    def __enter__(self):
        from sqlalchemy import event

        from app.db import engine

        self._engine = engine
        event.listen(engine.sync_engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event

        event.remove(self._engine.sync_engine, "before_cursor_execute", self._on_execute)
        return False

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1


async def measure(size: int) -> tuple[int, SyncBatch]:
    await clear()
    await seed(size)
    with _Counter() as counter:
        async with SessionFactory() as db:
            batch = await pull_since(db, principal(), "seq:0")
    return counter.count, batch


@pytest.mark.asyncio
async def test_full_page_query_cost_is_measured_not_extrapolated():
    """Replace the extrapolated "~167 for 500" with a measured figure.

    #48 measured 12 changes and the figure for a full page was obtained by
    multiplication. This measures the real thing at three sizes and reports the
    per-change cost at each, so the shape -- flat, sub-linear, or linear -- is visible
    rather than assumed.
    """
    from app.pull_prefetch import MAX_BATCH

    results = []
    for label, size in (("small", SMALL), ("large", LARGE), ("ceiling", CEILING)):
        queries, batch = await measure(size)
        delivered = len(batch.records)
        per_change = queries / size
        results.append((label, size, queries, per_change, delivered))
        print(
            f"\n{label:8s} page: {size:3d} changes -> {queries:3d} queries "
            f"({per_change:.2f} per change), delivered {delivered}"
        )

    print(
        f"\nchunk ceiling: {MAX_BATCH} (a {CEILING}-change page is "
        f"{'within' if CEILING <= MAX_BATCH else 'beyond'} one chunk)"
    )

    # The fixture must actually deliver, or the numbers describe rejected work. This
    # assertion is the whole point: an earlier measurement in this file's lineage
    # reported a full set of query counts while every record was rejected.
    #
    # `MAX_SYNC_RECORDS` (200) caps a single response, so a 500-change page correctly
    # delivers 200 and stops. Asserting `delivered == size` was wrong about the
    # contract rather than the code -- the per-change cost is still the real figure,
    # because all 500 changes were *scanned* even though 200 were delivered, and the
    # scan is where the queries are spent.
    from app.schemas import MAX_SYNC_RECORDS

    for label, size, queries, per_change, delivered in results:
        assert delivered == min(size, MAX_SYNC_RECORDS), (
            f"{label}: delivered {delivered} of {size} changes, expected "
            f"{min(size, MAX_SYNC_RECORDS)}. The query count describes a page that was "
            "not processed as intended, so the per-change figure is not what it "
            "appears to be."
        )
        assert queries > 0, f"{label}: no queries counted"

    small_per_change = results[0][3]
    ceiling_per_change = results[-1][3]

    # The property #53's prefetch exists to deliver: cost must fall per change as the
    # page grows, because the batched loads are a fixed handful regardless of size.
    # A linear or worsening per-change cost means a per-record lookup is back.
    assert ceiling_per_change < small_per_change, (
        f"per-change cost did not improve with page size: {small_per_change:.2f} at "
        f"{results[0][1]} changes vs {ceiling_per_change:.2f} at {results[-1][1]}. "
        "The batched prefetch should make a full page cost proportionally less, not "
        "the same. Something has reintroduced a per-record query."
    )
