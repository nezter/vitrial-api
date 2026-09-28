"""Cursor correctness for bounded sync pull pages, against real PostgreSQL.

The pure-function admission decision is pinned in tests/test_sync_pull_bounds.py.
What needs a database is the part that actually caused the bug: whether the
cursor advances past a change the client never received.

Two distinct cases, and the distinction is the whole point:

- A change that is **not deliverable** (invisible to this principal, or whose
  entity is gone) is *consumed*. The cursor must advance past it, or the client
  re-scans it on every pull forever and the cursor never moves.
- A change that **is deliverable but did not fit** the page is *not consumed*.
  The cursor must stop before it, or the client never sees that record and the
  data is silently lost.

Run with POSTGRES_INTEGRATION=1 against a migrated database.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.auth import Principal
from app.db import SessionFactory
from app.models import CanonicalCustomer, Organization, SyncChangeLog, SyncEntity, User
from app.schemas import SyncBatch
from app.sync_service import MAX_SYNC_PULL_SCAN_CHANGES, pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

# Every customer these tests seed must be inside the principal's scope.
# record_is_visible() checks the CanonicalCustomer row AND the effective scope
# (`all_customers or customer_id in customer_ids`), so a customer outside this
# set is correctly filtered out and the page comes back empty.
CUSTOMER_IDS = frozenset(f"customer-{i:04d}" for i in range(600))


def actor() -> Principal:
    return Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=1,
        capabilities=frozenset({"sync", "customer.create", "customer.edit"}),
        customer_ids=CUSTOMER_IDS,
        project_ids=frozenset(),
        all_customers=False,
        all_projects=False,
    )


async def clear(db) -> None:
    from sqlalchemy import delete

    await db.execute(delete(SyncChangeLog))
    await db.execute(delete(SyncEntity))
    await db.execute(delete(CanonicalCustomer))
    await db.execute(delete(User))
    await db.execute(delete(Organization))
    await db.commit()


async def seed_org(db) -> None:
    db.add(Organization(id="org-1", name="Vitrial", authorization_revision=1))
    db.add(User(id="user-1", display_name="Operator", email="operator@example.invalid"))
    await db.commit()


async def seed_deliverable_customers(db, count: int, *, payload_bytes: int = 8) -> None:
    """Seed `count` visible customers, each with a SyncChangeLog entry.

    Rows are written directly rather than through apply_push. apply_push runs
    authorize_record(), and on the create path that calls _grant_created_scope(),
    which demands a canonical, active Membership row -- setup these tests are not
    about. Seeding directly keeps the only dependency on production code the one
    under test: pull_since.
    """
    for i in range(count):
        customer_id = f"customer-{i:04d}"
        db.add(CanonicalCustomer(organization_id="org-1", customer_id=customer_id))
        db.flush()
        db.add(SyncEntity(
            organization_id="org-1",
            entity_type="customer",
            entity_id=customer_id,
            server_revision=1,
            schema_version=1,
            payload_json={"id": customer_id, "name": "C" * payload_bytes, "index": i},
            updated_at=datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc),
            deleted_at=None,
        ))
        db.add(SyncChangeLog(
            organization_id="org-1",
            entity_type="customer",
            entity_id=customer_id,
            server_revision=1,
            operation="upsert",
            client_mutation_id=f"mutation-{i:04d}",
            created_at=datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc),
        ))
        # Flush per row so each change gets its own sequence, keeping cursors
        # aligned 1:1 with customer-0000..n-1 rather than relying on ordering.
        await db.flush()
    await db.commit()


def cursor_of(pulled: SyncBatch) -> int:
    assert pulled.cursor is not None and pulled.cursor.startswith("seq:")
    return int(pulled.cursor[4:])


@pytest.mark.asyncio
async def test_cursor_advances_when_the_page_is_fully_consumed():
    """A normal page advances the cursor to the last delivered sequence."""
    async with SessionFactory() as db:
        await clear(db)
        await seed_org(db)
        await seed_deliverable_customers(db, 5)

        pulled = await pull_since(db, actor(), "seq:0")
        assert len(pulled.records) == 5
        assert cursor_of(pulled) == cursor_of(pulled)  # parses
        assert cursor_of(pulled) > 0

        # A second pull from that cursor returns nothing new.
        again = await pull_since(db, actor(), pulled.cursor)
        assert again.records == []


@pytest.mark.asyncio
async def test_cursor_stops_before_a_deliverable_record_that_does_not_fit(monkeypatch):
    """The regression this change fixes.

    Shrink the response ceiling so the page fills early. The change that does not
    fit must NOT be consumed: the cursor has to stop before it, and the next
    pull must return exactly that record.
    """
    import app.sync_service as sync_service

    async with SessionFactory() as db:
        await clear(db)
        await seed_org(db)
        await seed_deliverable_customers(db, 6, payload_bytes=4_000)

        # Small enough that only a couple of the 4 KB records fit per page.
        # monkeypatch restores the constant even if an assertion fails, which a
        # try/finally in the test body does not guarantee on collection errors.
        monkeypatch.setattr(sync_service, "MAX_SYNC_PULL_RESPONSE_BYTES", 12_000)

        first = await pull_since(db, actor(), "seq:0")
        assert 0 < len(first.records) < 6, "expected a partial page"

        # Drain the rest, one bounded page at a time, and prove the union is
        # exactly the six records with no loss and no duplication. A cursor that
        # skipped an unfitted record would show up as a shortfall here.
        seen: list[str] = []
        cursors: list[int] = []
        cursor = first.cursor
        page = first
        while page.records:
            seen.extend(r.entityID for r in page.records)
            cursors.append(cursor_of(page))
            cursor = page.cursor
            page = await pull_since(db, actor(), cursor)
            if len(cursors) > 20:
                raise AssertionError("pages did not converge; cursor is not advancing")

        assert len(seen) == 6, f"expected all 6 records, saw {len(seen)}: {seen}"
        assert len(set(seen)) == 6, f"a record was delivered twice: {seen}"
        assert cursors == sorted(cursors), f"cursor went backwards: {cursors}"
        assert len(set(cursors)) == len(cursors), f"cursor stalled: {cursors}"


@pytest.mark.asyncio
async def test_cursor_advances_past_an_undeliverable_change():
    """An invisible or orphaned change is consumed, so the cursor cannot stall.

    If the cursor stayed behind a change that can never be delivered, the client
    would rescan it forever and never progress past it.
    """
    async with SessionFactory() as db:
        await clear(db)
        await seed_org(db)

        # A change row whose entity does not exist: undeliverable, always.
        db.add(SyncChangeLog(
            organization_id="org-1",
            sequence=1,
            entity_type="customer",
            entity_id="customer-vanished",
            server_revision=1,
            operation="upsert",
            client_mutation_id="mutation-vanished",
            created_at=datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc),
        ))
        await db.commit()

        pulled = await pull_since(db, actor(), "seq:0")
        assert pulled.records == []
        # Consumed: the cursor moved past it despite delivering nothing.
        assert cursor_of(pulled) == 1

        # A later pull from that cursor does not rescan it.
        again = await pull_since(db, actor(), pulled.cursor)
        assert again.records == []
        assert cursor_of(again) == 1


@pytest.mark.asyncio
async def test_scan_ceiling_bounds_the_query():
    """The scan ceiling is applied to the query, not to the response.

    Creates more changes than one scan can read and confirms a single pull
    returns no more than the ceiling.
    """
    async with SessionFactory() as db:
        await clear(db)
        await seed_org(db)
        # Inserted directly, and deliberately with NO CanonicalCustomer rows:
        # every change is therefore undeliverable but still consumable, which is
        # what proves the scan ceiling bounds the query rather than the page.
        for i in range(MAX_SYNC_PULL_SCAN_CHANGES + 25):
            db.add(SyncChangeLog(
                organization_id="org-1",
                sequence=i + 1,
                entity_type="customer",
                entity_id=f"customer-missing-{i:04d}",
                server_revision=1,
                operation="upsert",
                client_mutation_id=f"mutation-{i:04d}",
                created_at=datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc),
            ))
        await db.commit()
        # Explicit sequence values on a BIGSERIAL primary key leave the
        # sequence counter behind, so a later apply_push in this database would
        # collide. Advance it past everything inserted.
        await db.execute(
            text("SELECT setval(pg_get_serial_sequence('sync_change_log', 'sequence'), "
                 ":last, true)"),
            {"last": MAX_SYNC_PULL_SCAN_CHANGES + 25},
        )
        await db.commit()

        pulled = await pull_since(db, actor(), "seq:0")
        # Every change is undeliverable, so all are consumed, but only up to the
        # scan ceiling.
        assert cursor_of(pulled) == MAX_SYNC_PULL_SCAN_CHANGES
        assert pulled.records == []

        # The remainder comes on the next pull.
        rest = await pull_since(db, actor(), pulled.cursor)
        assert cursor_of(rest) == MAX_SYNC_PULL_SCAN_CHANGES + 25
