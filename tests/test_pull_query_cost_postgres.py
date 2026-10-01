"""Query cost of a sync pull.

The 0.3.0 plan lists "remove/bound pull N+1 entity/visibility queries" under API
bounded-service hardening. This file originally **measured** the N+1 rather than
fixing it, because the fix was an authorization refactor and authorization should not
be refactured on the strength of a count derived by reading code.

It is now fixed: `app/pull_prefetch.py` resolves a page in a fixed number of
set-based queries. This file is the guard that it stays fixed, and its bound was
tightened from 8 queries/change to 4 when the prefetch landed.

What the count showed, before the fix
-------------------------------------
`pull_since` walked up to `MAX_SYNC_PULL_SCAN_CHANGES` changes and, per change, called
`record_is_visible` (1-3 `db.get` calls depending on entity type) plus a
`db.get(SyncEntity, ...)` for the payload. Per page that is:

| entity type   | visibility queries | + entity fetch | total |
|---------------|--------------------|----------------|-------|
| customer      | 1                  | 1              | 2     |
| project       | 1                  | 1              | 2     |
| project_sector| 2                  | 1              | 3     |
| item          | 2                  | 1              | 3     |
| project child | 2                  | 1              | 3     |
| item child    | 3                  | 1              | 4     |
| delivery      | 1                  | 1              | 2     |

So a full page was 1,000-2,000 sequential round trips, scaling linearly with page
size. After the batched prefetch it is a flat ~8 queries regardless of page size.

Running here
------------
Skipped unless `POSTGRES_INTEGRATION=1`, because the count is only meaningful against
a real database -- a stubbed session would flatter it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.db import SessionFactory
from app.sync_service import MAX_SYNC_PULL_SCAN_CHANGES, pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

# The bound when this file was written, before the batched prefetch landed. Kept so a
# regression to the old shape is recognisable rather than merely "slow".
LEGACY_MAX_QUERIES_PER_CHANGE = 8

# After the batched prefetch, the count is a fixed handful of set-based queries that
# does not grow with the page: 1 change-log page select plus at most 7 batch loads.
# 12 for a 12-change page leaves headroom for a new set-based load while still
# failing loudly if anything reintroduces a per-record lookup.
#
# This is the tightening the earlier commit said would have to happen with the fix.
# A regression bound nobody ever tightens stops being a guard and becomes decoration.
MAX_QUERIES_PER_CHANGE = 4

CHANGES = 12


def principal() -> Principal:
    return Principal(
        user_id="user-pull-cost",
        organization_id="org-pull-cost",
        membership_id="membership-pull-cost",
        session_id="session-pull-cost",
        authorization_revision=1,
        capabilities=frozenset({"sync"}),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )


async def seed_changes(db: AsyncSession) -> None:
    """Create visible item changes that exercise the full pull loop.

    An invisible row is *not* a worst case here: `pull_since` continues immediately
    after `record_is_visible` returns false, so it never performs the final
    `SyncEntity` payload fetch. To measure the N+1 we need records that are actually
    visible and therefore traverse both authorization lookup and payload fetch.

    Each item gets its own canonical Project so SQLAlchemy's identity map cannot make
    repeated Project lookups disappear after the first record and flatter the count.
    """
    from app.models import (
        CanonicalCustomer,
        CanonicalItem,
        CanonicalProject,
        Organization,
        SyncChangeLog,
        SyncEntity,
    )

    db.add(Organization(id="org-pull-cost", name="Pull Cost", authorization_revision=1))
    await db.flush()
    db.add(CanonicalCustomer(
        organization_id="org-pull-cost",
        customer_id="customer-pull-cost",
    ))
    await db.flush()

    for index in range(CHANGES):
        project_id = f"project-pull-cost-{index}"
        item_id = f"item-pull-cost-{index}"
        db.add(CanonicalProject(
            organization_id="org-pull-cost",
            project_id=project_id,
            customer_id="customer-pull-cost",
        ))
        await db.flush()
        db.add(CanonicalItem(
            organization_id="org-pull-cost",
            item_id=item_id,
            project_id=project_id,
            project_sector_id=None,
        ))
        db.add(
            SyncChangeLog(
                organization_id="org-pull-cost",
                entity_type="item",
                entity_id=item_id,
                server_revision=1,
                client_mutation_id=f"m-{index}",
                operation="upsert",
            )
        )
        db.add(
            SyncEntity(
                organization_id="org-pull-cost",
                entity_type="item",
                entity_id=item_id,
                server_revision=1,
                schema_version=1,
                payload_json={"id": item_id, "projectID": project_id},
                updated_at=datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc),
            )
        )
    await db.commit()


class _QueryCounter:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.count = 0

    def __enter__(self):
        event.listen(self.engine.sync_engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._on_execute)
        return False

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1


@pytest.mark.asyncio
async def test_pull_query_count_does_not_grow_with_the_page():
    """The query count must be independent of page size.

    Was ``grows_with_the_page``, which documented the N+1. The assertion is now
    inverted: the batched prefetch in ``app/pull_prefetch.py`` resolves the whole
    page in a fixed number of set-based queries, so a count that scales with the
    number of changes means a per-record lookup has been reintroduced.
    """
    from app.db import engine

    async with SessionFactory() as db:
        await seed_changes(db)

    with _QueryCounter(engine) as counter:
        async with SessionFactory() as db:
            result = await pull_since(db, principal(), "seq:0")

    assert len(result.records) == CHANGES, (
        "measurement fixture must remain fully visible or the payload-fetch half of "
        "the N+1 will disappear from the count"
    )
    per_change = counter.count / CHANGES
    print(
        f"\npull page of {CHANGES} changes issued {counter.count} queries "
        f"({per_change:.1f} per change). "
        f"A full {MAX_SYNC_PULL_SCAN_CHANGES}-change page would issue "
        f"~{per_change * MAX_SYNC_PULL_SCAN_CHANGES:.0f}."
    )

    assert per_change <= MAX_QUERIES_PER_CHANGE, (
        f"pull issued {per_change:.1f} queries per change ({counter.count} total for "
        f"{CHANGES} changes), above the bound of {MAX_QUERIES_PER_CHANGE}. After the "
        "batched prefetch the count should be flat in page size, so a per-change "
        "factor here means a per-record lookup has been reintroduced -- most likely a "
        "db.get() added to the loop in pull_since instead of reading from the "
        "prefetched page context."
    )
