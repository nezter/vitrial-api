"""Concurrency behaviour of the push path, measured against a real database.

Section 4 of the 0.3.0 plan asks for concurrency benchmarks. `tests/test_sync_concurrency.py`
asserts *correctness* under contention -- one commit, one winner, no double apply -- and
that is the property that matters most. It says nothing about what contention *costs*.

This measures it:

1. **How many concurrent pushes actually run in parallel**, against the configured pool
   budget of `pool_size + max_overflow`. A pool that is smaller than it looks will
   serialise requests that the plan assumed were concurrent, and a benchmark written
   against that assumption measures the queue, not the code.

2. **Whether the per-record query cost from #55 degrades under contention.** The
   measurement there was serial. If push issues 11.2 queries per record and those
   queries serialise behind each other, the cost is quadratic in batch size in wall-clock
   terms even though it is linear in count.

The numbers are printed on every run so they can be compared over time rather than
asserted. A threshold here would be a performance target, and a target nobody can
justify from a single machine's numbers is worse than a measurement.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, event, func, select

from app.db import SessionFactory, engine
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalItemChild,
    CanonicalProject,
    CanonicalProjectSector,
    Organization,
    SyncChangeLog,
    SyncEntity,
    SyncMutation,
)
from app.schemas import SyncBatch, SyncRecord

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ORG = "org-concurrency-probe"
NOW = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
BATCH = 8


def actor():
    from app.auth import Principal

    return Principal(
        user_id="user-concurrency",
        organization_id=ORG,
        membership_id="membership-concurrency",
        session_id="session-concurrency",
        authorization_revision=1,
        capabilities=frozenset({"sync", "item.measurements.manage"}),
        customer_ids=frozenset({"customer-concurrency"}),
        project_ids=frozenset({"project-concurrency"}),
        all_customers=False,
        all_projects=False,
    )


def _payload(entity: str, note: str) -> str:
    """Encoded payload for one measurement.

    `id` must equal the record's `entityID` -- `authorize_record` rejects a mismatch
    with `payload identity does not match canonical entityID`. It was hardcoded to
    `measurement-1` while the records used `measurement-probe-N`, so every push was
    `AuthorizationRejected` and the test printed a full table of timings for work that
    never happened.
    """
    import base64
    import json

    return base64.b64encode(
        json.dumps(
            {"id": entity, "itemID": "item-1", "evidenceReferences": [], "note": note},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).decode()


def entity_id(index: int) -> str:
    """The canonical measurement id, shared by `seed()` and `batch()`.

    Defined once so the two cannot drift. They did, and every record in every push was
    then rejected as a create against a non-canonical entity -- while the test still
    printed a full table of query counts and timings that looked entirely plausible.
    """
    return f"measurement-probe-{index}"


def batch(device: str, *, offset: int = 0, size: int = BATCH) -> SyncBatch:
    """One batch of `size` records, each a distinct measurement under the same item.

    Distinct entities so concurrent pushes contend on the change-log sequence and the
    pool -- which is what this measures -- rather than colliding on a single row, which
    `test_sync_concurrency.py` already covers.

    `offset` gives each concurrent push its own slice of the canonical ids so they are
    not all fighting over the same rows.
    """
    records = []
    for index in range(offset, offset + size):
        records.append(
            SyncRecord(
                id=f"client:{device}:{index}",
                entityType="measurement",
                entityID=entity_id(index),
                updatedAt=NOW,
                payload=_payload(entity_id(index), f"{device}-{index}"),
                serverRevision=0,
                # Omitted, not zero. `baseServerRevision=None` is the create case; an
                # explicit `0` against a non-existent entity is rejected as
                # `stale_revision`, which rejected all 120 records in the first run.
                baseServerRevision=None,
                clientMutationID=f"cm-{device}-{index}",
                deletedAt=None,
            )
        )
    return SyncBatch.model_validate({"deviceID": device, "records": records})


async def clear() -> None:
    from app.idempotency import SyncMutationFingerprint

    async with SessionFactory() as db:
        for model in (
            SyncMutationFingerprint, SyncChangeLog, SyncMutation, SyncEntity,
            CanonicalItemChild,
        ):
            await db.execute(
                delete(model).where(model.__table__.c.organization_id == ORG)
            )
        await db.flush()
        for model in (CanonicalItem, CanonicalProjectSector, CanonicalProject, CanonicalCustomer):
            await db.execute(
                delete(model).where(model.__table__.c.organization_id == ORG)
            )
        await db.flush()
        await db.execute(delete(Organization).where(Organization.id == ORG))
        await db.execute(
            __import__("sqlalchemy").text("TRUNCATE sync_change_log RESTART IDENTITY CASCADE")
        )
        await db.commit()


async def seed() -> None:
    async with SessionFactory() as db:
        db.add(Organization(id=ORG, name="Concurrency Probe", authorization_revision=1))
        await db.flush()
        db.add(CanonicalCustomer(organization_id=ORG, customer_id="customer-concurrency"))
        await db.flush()
        db.add(
            CanonicalProject(
                organization_id=ORG, project_id="project-concurrency",
                customer_id="customer-concurrency",
            )
        )
        await db.flush()
        db.add(
            CanonicalProjectSector(
                organization_id=ORG, project_sector_id="sector-concurrency",
                project_id="project-concurrency", sector_id="sector-concurrency",
            )
        )
        await db.flush()
        db.add(
            CanonicalItem(
                organization_id=ORG, item_id="item-1",
                project_id="project-concurrency", project_sector_id="sector-concurrency",
            )
        )
        await db.flush()
        # Entity ids must match the ones `batch()` requests, or every record is a
        # create against a non-canonical entity and is rejected before any timing is
        # recorded. They did not match for the first run, which produced
        # `accepted=0` for 120 records while still printing a plausible-looking table of
        # numbers.
        for index in range(BATCH * 12):
            db.add(
                CanonicalItemChild(
                    organization_id=ORG, entity_type="measurement",
                    entity_id=entity_id(index), item_id="item-1",
                )
            )
        await db.commit()


class _Counter:
    """Counts statements, and tracks how many connections overlap.

    `checkout` is passed `(dbapi_connection, connection_record, pool)` and `checkin` only
    `(dbapi_connection, connection_record)`. I had them the other way round first, so
    both handlers need their own signature rather than a shared one.
    """

    def __init__(self) -> None:
        self.count = 0
        self.peak_concurrent_connections = 0
        self._live = 0

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1

    def _on_checkout(self, dbapi_connection, connection_record, pool):
        self._live += 1
        self.peak_concurrent_connections = max(
            self.peak_concurrent_connections, self._live
        )

    def _on_checkin(self, dbapi_connection, connection_record):
        self._live -= 1


def instrument(counter: _Counter) -> None:
    event.listen(engine.sync_engine, "before_cursor_execute", counter._on_execute)
    event.listen(engine.pool, "checkout", counter._on_checkout)
    event.listen(engine.pool, "checkin", counter._on_checkin)


def uninstrument(counter: _Counter) -> None:
    event.remove(engine.sync_engine, "before_cursor_execute", counter._on_execute)
    event.remove(engine.pool, "checkout", counter._on_checkout)
    event.remove(engine.pool, "checkin", counter._on_checkin)


@pytest.mark.asyncio
async def test_concurrent_pushes_actually_run_in_parallel():
    """How many pushes overlap, against the configured pool budget.

    If this reports 1, the benchmark measures a serial queue and the pool is the
    bottleneck -- which would mean every concurrency number recorded so far is really a
    throughput number for a single connection. If it approaches the budget, the pool is
    sized correctly for this workload and the number is worth comparing over time.
    """
    from app.settings import settings
    from app.sync_service import apply_push

    await clear()
    await seed()

    budget = settings.database_pool_size + settings.database_max_overflow
    counter = _Counter()
    instrument(counter)
    started = time.perf_counter()
    try:
        results = await asyncio.gather(
            *[
                _one_push(apply_push, f"p{i}", offset=i * BATCH)
                for i in range(budget)
            ]
        )
    finally:
        uninstrument(counter)
    elapsed = time.perf_counter() - started

    accepted = sum(len(r.acceptedRecordIDs) for r in results)
    rejected = sum(len(r.rejectedRecordIDs) for r in results)
    total = accepted + rejected
    per_push = counter.count / max(1, len(results))

    print(
        f"\npool budget: {budget} "
        f"(pool_size={settings.database_pool_size} + "
        f"max_overflow={settings.database_max_overflow})"
        f"\npushes: {len(results)} of {BATCH} records each"
        f"\npeak concurrent connections: {counter.peak_concurrent_connections}"
        f"\nqueries: {counter.count} total, {per_push:.1f} per push"
        f"\nwall clock: {elapsed:.3f}s for {total} records"
        f"\naccepted={accepted} rejected={rejected}"
    )

    # Every record must be accepted. A weaker `accepted > 0` passed on a run where all
    # 120 records were rejected as `stale_revision`, which is how a meaningless
    # measurement got reported as a result. Concurrency *thresholds* are deliberately
    # not asserted -- this machine cannot justify a target -- but the fixture has to be
    # known-good before the numbers mean anything.
    assert rejected == 0, (
        f"{rejected} of {total} records were rejected ({accepted} accepted). The "
        "timings below measure a fixture that is not doing real work."
    )
    assert accepted == total
    assert counter.count > 0, "no queries were counted"


async def _one_push(_unused, device: str, *, offset: int):
    from app.sync_service import apply_push

    async with SessionFactory() as db:
        return await apply_push(db, actor(), batch(device, offset=offset))


@pytest.mark.asyncio
async def test_per_record_query_cost_is_stable_under_contention():
    """The #55 per-record cost, re-measured with several pushes in flight.

    A serial measurement can hide contention: if the per-record queries serialise
    behind each other, the count stays linear while the wall-clock cost does not. This
    reports both, so a future change can be compared on the same terms.
    """
    from app.sync_service import apply_push

    await clear()
    await seed()

    async def timed(device: str, *, offset: int) -> tuple[float, int, int]:
        counter = _Counter()
        instrument(counter)
        started = time.perf_counter()
        try:
            async with SessionFactory() as db:
                result = await apply_push(db, actor(), batch(device, offset=offset))
        finally:
            uninstrument(counter)
        return (
            time.perf_counter() - started,
            counter.count,
            len(result.acceptedRecordIDs),
        )

    solo_seconds, solo_queries, solo_accepted = await timed("solo", offset=0)
    solo_per_record = solo_queries / BATCH

    contended = await asyncio.gather(
        *[timed(f"c{i}", offset=BATCH + i * BATCH) for i in range(4)]
    )
    contended_per_record = sum(q for _, q, _ in contended) / (4 * BATCH)
    contended_seconds = max(s for s, _, _ in contended)

    print(
        f"\nper-record queries, solo:     {solo_per_record:.1f}"
        f"\nper-record queries, 4 at once: {contended_per_record:.1f}"
        f"\nsolo push wall clock:          {solo_seconds:.3f}s"
        f"\nslowest of 4 concurrent:       {contended_seconds:.3f}s"
        f"\ncontention multiplier:         {contended_seconds / solo_seconds:.1f}x"
    )

    assert all(accepted == BATCH for _, _, accepted in contended), (
        "every concurrent push must accept every record; a partial run measures a "
        "fixture that is not doing real work"
    )
    assert solo_accepted == BATCH, (
        f"the solo push accepted {solo_accepted} of {BATCH}; the baseline is invalid"
    )
