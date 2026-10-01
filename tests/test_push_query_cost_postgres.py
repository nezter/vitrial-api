"""Query cost of a sync push, measured against a real database.

The pull path had the same shape and cost ~1,000-2,000 round trips for a full page
before `app/pull_prefetch.py`. Push has the same per-record loop and the same bound
of `MAX_SYNC_RECORDS` (200).

This measures rather than extrapolates, because the number that matters is the one a
database produces, not the one the loop appears to imply.

Also the first thing in this repo that records a *ratio* rather than an absolute, so
a later optimisation has something to compare against.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, event, text

from app.auth import Principal
from app.db import SessionFactory
from app.idempotency import SyncMutationFingerprint
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
from app.sync_service import apply_push

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

NOW = "2026-09-17T14:00:00Z"

# Per-record round trips observed in `_apply_push_once`:
#   SyncMutation   (idempotency check, always)
#   SyncEntity     (current revision, always)
#   flush          (per record)
# plus a SyncMutationFingerprint and EvidenceBlob on their branches.
# `MAX_SYNC_RECORDS` bounds the batch, so the page-scaled figure is the honest one.
RECORDS = 8

# Per-record round trips, confirmed by counting the SQL a real push emits. Every
# statement below appears exactly RECORDS times -- nothing is batched:
#
#   2x  canonical_items            (authorize_record + revision read)
#   1x  sync_mutations             (idempotency check)
#   1x  sync_entities              (current revision)
#   1x  canonical_projects         (scope resolution)
#   1x  canonical_project_sectors  (sector is canonical for project)
#   1x  max(server_revision)       (revision allocation)
#   1x  INSERT fingerprint / change_log / entity
#
# 90 statements for 8 records = 11.2 each. MAX_SYNC_RECORDS is 200, so a full push is
# ~2,250 round trips -- worse than the pull page that #53 brought down to ~167.

# Set from the measured value (11.2), rounded up. This is a tripwire that will fail
# loudly the moment the per-record pattern changes shape, not a target: the batched
# alternative is not written yet. When it is, this should fall sharply and be
# tightened with it, the same way #53 tightened the pull bound from 8 to 4.
MAX_QUERIES_PER_RECORD = 12


def principal() -> Principal:
    return Principal(
        user_id="user-push-cost",
        organization_id="org-push-cost",
        membership_id="membership-push-cost",
        session_id="session-push-cost",
        authorization_revision=1,
        # Both capabilities, because the fixture creates items: `item.create` for the
        # create branch (ownership.py:473) and `item.edit` for the update branch. With
        # only `item.edit`, every record is rejected with AuthorizationRejected and the
        # count measures nothing.
        capabilities=frozenset({"sync", "item.create", "item.edit"}),
        customer_ids=frozenset({"customer-push-cost"}),
        project_ids=frozenset({"project-push-cost"}),
        all_customers=False,
        all_projects=False,
    )


def record(index: int) -> SyncRecord:
    import base64
    import json

    # `projectID` and `projectSectorID` are required, not optional: an Item without a
    # canonical ProjectSector is rejected with "existing Item lacks canonical
    # ProjectSector ownership". Found by calling `authorize_record` directly for the
    # message, which the mutation_result log event does not carry.
    payload = json.dumps(
        {
            "id": f"item-push-cost-{index}",
            "projectID": "project-push-cost",
            "projectSectorID": "project-sector-push-cost",
        },
        separators=(",", ":"),
    ).encode()
    return SyncRecord(
        id=f"client:{index}",
        entityType="item",
        entityID=f"item-push-cost-{index}",
        updatedAt=NOW,
        payload=base64.b64encode(payload),
        serverRevision=0,
        clientMutationID=f"cm-push-cost-{index}",
        deletedAt=None,
    )


async def clear() -> None:
    """Delete this file's rows, children before parents, in two flushed groups.

    `CanonicalItemChild` was missing from the original list, so a sibling test that
    seeded one under this organization left a child row behind and the parent deletes
    below then failed on the item foreign key. The explicit flush is load-bearing:
    SQLAlchemy coalesces queued DELETEs into one flush, so without it the parents are
    emitted alongside the children and removed first.
    """
    async with SessionFactory() as db:
        for model in (
            SyncMutationFingerprint, SyncChangeLog, SyncMutation, SyncEntity,
            CanonicalItemChild, CanonicalItem, CanonicalProjectSector,
        ):
            await db.execute(
                delete(model).where(
                    model.__table__.c.organization_id == "org-push-cost"
                )
            )
        await db.flush()
        for model in (CanonicalProject, CanonicalCustomer, Organization):
            await db.execute(
                delete(model).where(model.__table__.c.organization_id == "org-push-cost")
                if "organization_id" in model.__table__.c
                else delete(model).where(Organization.id == "org-push-cost")
            )
        await db.execute(text("TRUNCATE sync_change_log RESTART IDENTITY CASCADE"))
        await db.commit()


async def seed() -> None:
    async with SessionFactory() as db:
        db.add(Organization(id="org-push-cost", name="Push Cost", authorization_revision=1))
        await db.flush()
        db.add(CanonicalCustomer(organization_id="org-push-cost", customer_id="customer-push-cost"))
        await db.flush()
        db.add(
            CanonicalProject(
                organization_id="org-push-cost",
                project_id="project-push-cost",
                customer_id="customer-push-cost",
            )
        )
        await db.flush()
        db.add(
            CanonicalProjectSector(
                organization_id="org-push-cost",
                project_sector_id="project-sector-push-cost",
                project_id="project-push-cost",
                sector_id="sector-push-cost",
            )
        )
        await db.flush()
        for index in range(RECORDS):
            db.add(
                CanonicalItem(
                    organization_id="org-push-cost",
                    item_id=f"item-push-cost-{index}",
                    project_id="project-push-cost",
                    # Not optional. The items are seeded, so `authorize_record` takes
                    # the *update* branch, which rejects a row whose
                    # `project_sector_id` is empty with "existing Item lacks canonical
                    # ProjectSector ownership". Found by reading the branch rather than
                    # by another round of guessing at capabilities.
                    project_sector_id="project-sector-push-cost",
                )
            )
        await db.commit()
        # Reset the change-log sequence. `sync_change_log.sequence` is an autoincrement
        # primary key shared across the whole database, and the other integration
        # tests' `clear_database` helpers delete rows without resetting it. A reused
        # local database therefore carries a drifted sequence between runs, which
        # surfaces as unrelated delivery tests failing. CI provisions a fresh database
        # and never sees this; a local run does.
        await db.execute(text("TRUNCATE sync_change_log RESTART IDENTITY CASCADE"))


class _Counter:
    def __init__(self, engine):
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
async def test_push_query_count_is_measured_against_a_real_database():
    """Records the actual query count, so the push N+1 is evidence not assertion."""
    from app.db import engine

    await clear()
    await seed()

    batch = SyncBatch(
        deviceID="device-push-cost",
        cursor=None,
        records=[record(i) for i in range(RECORDS)],
    )

    with _Counter(engine) as counter:
        async with SessionFactory() as db:
            result = await apply_push(db, principal(), batch)

    assert len(result.acceptedRecordIDs) == RECORDS, (
        f"fixture must push cleanly, accepted {len(result.acceptedRecordIDs)} of "
        f"{RECORDS} ({result.rejectedRecordIDs}); an unmeasurable count is worse than none"
    )

    per_record = counter.count / RECORDS
    print(
        f"\npush of {RECORDS} records issued {counter.count} queries "
        f"({per_record:.1f} per record). A full {200}-record push would issue "
        f"~{per_record * 200:.0f}."
    )

    assert per_record <= MAX_QUERIES_PER_RECORD, (
        f"push issued {per_record:.1f} queries per record, above the bound of "
        f"{MAX_QUERIES_PER_RECORD}. The per-record SyncMutation idempotency lookup and "
        "SyncEntity revision read are the expected cause; a batched prefetch is the fix."
    )
