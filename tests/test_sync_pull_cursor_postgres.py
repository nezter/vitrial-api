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

import base64
import json
import os
from datetime import datetime, timezone

import pytest

from app.auth import Principal
from app.db import SessionFactory
from app.models import CanonicalCustomer, Organization, SyncChangeLog, SyncEntity, User
from app.schemas import SyncBatch, SyncRecord
from app.sync_service import MAX_SYNC_PULL_SCAN_CHANGES, apply_push, pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

NOW = "2026-09-06T14:00:00Z"


def encoded(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def record(record_id: str, entity_id: str, payload: dict, mutation_id: str) -> dict:
    return {
        "id": record_id,
        "entityType": "customer",
        "entityID": entity_id,
        "updatedAt": NOW,
        "payload": encoded(payload),
        "baseServerRevision": None,
        "clientMutationID": mutation_id,
        "deletedAt": None,
    }


# Every customer this test creates must be inside the principal's scope.
# record_is_visible() checks CanonicalCustomer *and* the effective scope, so a
# pushed record with no canonical row -- or one outside customer_ids -- is
# correctly filtered out and the page comes back empty.
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
    from sqlalchemy import delete, text

    await db.execute(delete(SyncChangeLog))
    await db.execute(delete(SyncEntity))
    await db.execute(delete(User))
    await db.execute(delete(Organization))
    await db.commit()


async def seed_org(db) -> None:
    db.add(Organization(id="org-1", name="Vitrial", authorization_revision=1))
    db.add(User(id="user-1", display_name="Operator", email="operator@example.invalid"))
    await db.commit()


async def push_customers(db, count: int, *, payload_bytes: int = 8) -> None:
    """Create `count` customers, each carrying a payload of roughly `payload_bytes`.

    Also inserts the CanonicalCustomer row each one needs. Without it the record
    is invisible: record_is_visible() requires both the canonical row and scope.
    """
    for i in range(count):
        db.add(CanonicalCustomer(organization_id="org-1", customer_id=f"customer-{i:04d}"))
        await db.flush()
        batch = SyncBatch.model_validate({
            "deviceID": "device-1",
            "records": [
                record(
                    f"record-{i:04d}",
                    f"customer-{i:04d}",
                    {"name": "C" * payload_bytes, "index": i},
                    f"mutation-{i:04d}",
                )
            ],
        })
        await apply_push(db, actor(), batch)
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
        await push_customers(db, 5)

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
        await push_customers(db, 6, payload_bytes=4_000)

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
