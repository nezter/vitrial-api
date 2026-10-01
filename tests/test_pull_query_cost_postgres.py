"""Query cost of a sync pull.

The 0.3.0 plan lists "remove/bound pull N+1 entity/visibility queries" under API
bounded-service hardening. This file does not fix that; it **measures** it, because the
fix is an authorization refactor and authorization should not be refactured on the
strength of a count derived by reading code.

What the count shows
--------------------
`pull_since` walks up to `MAX_SYNC_PULL_SCAN_CHANGES` changes and, per change, calls
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

So a full page is 1,000-2,000 sequential round trips, and it scales linearly with
page size.

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

# Generous on purpose. This is a *regression* bound that fails loudly if the shape
# changes, not a performance target -- the actual optimisation is tracked separately
# and this number should fall when it lands.
MAX_QUERIES_PER_CHANGE = 8

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
    """Create change-log rows for entities that do not resolve.

    Invisible rows are sufficient to measure the per-change query cost, and they are
    also the *worst* case for the N+1: every change is examined and none is skipped
    early, so nothing terminates the walk before the page is exhausted.

    Column shapes are copied from `apply_push` rather than guessed, since this test
    runs only in the integration job where a wrong column is an error nobody sees
    locally.
    """
    from app.models import Organization, SyncChangeLog, SyncEntity

    db.add(Organization(id="org-pull-cost", name="Pull Cost", authorization_revision=1))
    await db.flush()
    for index in range(CHANGES):
        db.add(
            SyncChangeLog(
                organization_id="org-pull-cost",
                entity_type="item",
                entity_id=f"missing-item-{index}",
                server_revision=1,
                client_mutation_id=f"m-{index}",
                operation="upsert",
            )
        )
        db.add(
            SyncEntity(
                organization_id="org-pull-cost",
                entity_type="item",
                entity_id=f"missing-item-{index}",
                server_revision=1,
                schema_version=1,
                payload_json={"id": f"missing-item-{index}"},
                # `SyncEntity.updated_at` is NOT NULL with no default, so a direct
                # construction must set it. The integration job is the only place
                # this omission would have surfaced.
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
async def test_pull_query_count_grows_with_the_page():
    """Records the actual query count, so the N+1 is evidence rather than assertion."""
    from app.db import engine

    async with SessionFactory() as db:
        await seed_changes(db)

    with _QueryCounter(engine) as counter:
        async with SessionFactory() as db:
            await pull_since(db, principal(), "seq:0")

    per_change = counter.count / CHANGES
    print(
        f"\npull page of {CHANGES} changes issued {counter.count} queries "
        f"({per_change:.1f} per change). "
        f"A full {MAX_SYNC_PULL_SCAN_CHANGES}-change page would issue "
        f"~{per_change * MAX_SYNC_PULL_SCAN_CHANGES:.0f}."
    )

    assert per_change <= MAX_QUERIES_PER_CHANGE, (
        f"pull issued {per_change:.1f} queries per change, above the bound of "
        f"{MAX_QUERIES_PER_CHANGE}. The per-change N+1 in record_is_visible plus the "
        "SyncEntity fetch is the expected cause; the fix is a batched prefetch."
    )
