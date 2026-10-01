"""Why the push pre-read was abandoned, pinned so it is not re-attempted blindly.

#55 measured the push path at 11.2 queries per record and named the cause: a
per-record idempotency probe (`select SyncMutation where client_mutation_id = ...`)
and a per-record revision read (`db.get(SyncEntity, ...)`).

Both are pure data reads, so batching them looked straightforward and safe -- the same
argument that made the read-side prefetch in `app/pull_prefetch.py` safe. It is not.
This file records the two ways that argument is wrong, because both were found by
failing tests rather than by reading the code.

## 1. A record can observe rows written earlier in the same request

Two records in one batch may share a `clientMutationID`. The second must see the
`SyncMutation` row the first just wrote, or a same-batch duplicate is accepted twice.

`db.add` does not make that row visible: it is pending in the session until flush, and
`apply_push` flushes at the *end* of the loop. So a pre-read taken once before the
loop is a snapshot that goes stale partway through.

A write-back into the pre-read map fixes that case and breaks the next one.

## 2. A session may already hold the caller's own uncommitted state

Consecutive `apply_push` calls on one session leave rows that a later pre-read must
see. Adding `await db.flush()` before the pre-read is not enough either: the
fingerprint written for a committed mutation is then *overwritten* by the colliding
record's own fingerprint, so the comparison against "is this the same request?"
degenerates to comparing the incoming request with itself, and the collision is
accepted.

Verified by both halves:

    write-back disabled            -> same-batch duplicate test fails
    write-back + pre-loop flush     -> cross-request collision test fails

Neither the write-back alone nor the flush alone is correct. Together they interact
through SQLAlchemy's identity map, where a re-read returns the same in-memory object
the loop just populated.

## What this means

The per-record cost is not incidental. It is load-bearing for idempotency, which is
the property that stops a retried or duplicated push from applying twice. Optimising it
requires the pre-read to model intra-request and intra-session visibility, which is a
substantially harder problem than "these are just data reads".

So the measurement from #55 stands and the fix is deferred. `app/push_preread.py` is
kept as a working prototype with `resolve_visible`-style documentation of what it does
and does not prove, and these tests are here so the next attempt starts from the two
known failure modes instead of rediscovering them.

The push floor is set by `authorize_record` plus its canonical lookups, which are
intentionally per record on both the read and write sides.
"""

from __future__ import annotations

import datetime
import os

import pytest
from sqlalchemy import delete
from sqlalchemy import func, select, text

from app.db import SessionFactory
from app.idempotency import SyncMutationFingerprint
from app.models import SyncChangeLog, SyncEntity, SyncMutation
from app.schemas import SyncBatch

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

NOW = "2026-09-17T14:00:00Z"


def _payload(note: str) -> str:
    import base64
    import json

    return base64.b64encode(
        json.dumps(
            {"id": "measurement-1", "itemID": "item-1", "evidenceReferences": [], "note": note},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).decode()


def _mutation(record_id: str, note: str, *, mutation_id: str) -> dict:
    return {
        "id": record_id,
        "entityType": "measurement",
        "entityID": "measurement-1",
        "updatedAt": NOW,
        "payload": _payload(note),
        "baseServerRevision": 5,
        "clientMutationID": mutation_id,
        "deletedAt": None,
    }


def _actor():
    from app.auth import Principal

    return Principal(
        user_id="user-preread",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=7,
        # `item.measurements.manage` is the capability that guards a Measurement write.
        # Without it every record is rejected on authorization and the test measures
        # nothing -- found by diffing against the actor in test_idempotency_collision.
        capabilities=frozenset({"sync", "item.measurements.manage"}),
        customer_ids=frozenset({"customer-1"}),
        project_ids=frozenset({"project-1"}),
        all_customers=False,
        all_projects=False,
    )


async def _clear(db) -> None:
    """Clear every table this test writes, in dependency order.

    One function rather than a reset-then-seed pair, because the two orders each had a
    failure mode: resetting after seeding left a canonical Item whose entity the first
    push saw as a stale revision, and not clearing the canonical tables at all produced
    a primary-key IntegrityError. A fixture whose two halves have to be called in one
    specific order is a fixture that is one refactor away from breaking.
    """
    from app.models import (
        CanonicalCustomer,
        CanonicalItem,
        CanonicalItemChild,
        CanonicalProject,
        CanonicalProjectChild,
        CanonicalProjectSector,
        Organization,
    )

    # Order is not sufficient on its own: `canonical_items` has a foreign key to
    # `canonical_projects`, and SQLAlchemy batches the DELETEs into one flush, so a
    # parent can be removed before its children in the same statement. The failure was
    # `ForeignKeyViolationError: update or delete on table "canonical_projects" violates
    # foreign key constraint "fk_canonical_item_project"`, and it only appeared when this
    # file ran alongside another integration file.
    #
    # The same defect was found, and fixed, in `test_delivery_execution_postgres.py` and
    # `test_pull_visibility_matrix_postgres.py` -- both of which looked correct for as
    # long as they were the only writer in their organization.
    for model in (
        SyncMutationFingerprint, SyncChangeLog, SyncMutation, SyncEntity,
        CanonicalItemChild, CanonicalItem, CanonicalProjectSector,
        CanonicalProjectChild,
    ):
        await db.execute(delete(model))
    # An explicit flush is required, not optional. SQLAlchemy coalesces the queued
    # DELETEs into a single flush, so issuing the child deletes and then the parent
    # deletes still emits them together and the parent goes first. Splitting the loops
    # is not enough; the transaction needs to reach the database in between.
    await db.flush()
    for model in (CanonicalProject, CanonicalCustomer, Organization):
        await db.execute(delete(model))
    # The change-log sequence is a database-wide autoincrement primary key, and the
    # push writes one row per record. The other integration tests' clear helpers delete
    # rows without resetting that sequence, so this test would leave it advanced and
    # every later file that seeds a change log would collide on a primary key. Found by
    # running this file before the delivery execution tests and watching them fail; the
    # interaction is invisible in isolation, where this file passes on its own.
    await db.commit()


async def _seed(db) -> None:
    from app.models import (
        CanonicalCustomer,
        CanonicalItem,
        CanonicalItemChild,
        CanonicalProject,
        CanonicalProjectSector,
        Organization,
    )

    db.add(Organization(id="org-1", name="Vitrial", authorization_revision=7))
    await db.flush()
    db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-1"))
    await db.flush()
    db.add(CanonicalProject(organization_id="org-1", project_id="project-1", customer_id="customer-1"))
    await db.flush()
    db.add(
        CanonicalProjectSector(
            organization_id="org-1",
            project_sector_id="sector-1",
            project_id="project-1",
            sector_id="sector-1",
        )
    )
    await db.flush()
    db.add(
        CanonicalItem(
            organization_id="org-1", item_id="item-1",
            project_id="project-1", project_sector_id="sector-1",
        )
    )
    await db.flush()
    db.add(
        CanonicalItemChild(
            organization_id="org-1",
            entity_type="measurement",
            entity_id="measurement-1",
            item_id="item-1",
        )
    )
    await db.flush()
    # The current revision must exist and match the records' `baseServerRevision`, or
    # the very first push is rejected as `stale_revision` and the test measures nothing.
    # This is the same shape `test_idempotency_collision.py` seeds.
    db.add(
        SyncEntity(
            organization_id="org-1",
            entity_type="measurement",
            entity_id="measurement-1",
            server_revision=5,
            schema_version=1,
            payload_json={
                "id": "measurement-1",
                "itemID": "item-1",
                "evidenceReferences": [],
                "note": "baseline",
            },
            updated_at=datetime.datetime(2026, 9, 17, 14, 0, tzinfo=datetime.timezone.utc),
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_same_batch_duplicate_mutation_id_is_rejected():
    """Two records, one `clientMutationID`, one request.

    The first is accepted and the second rejected for colliding request bytes. This is
    the case a pre-read taken once before the loop gets wrong, because the second
    record must observe the row the first wrote -- and `db.add` does not make that
    visible until the flush that happens after the loop.
    """
    from app.sync_service import apply_push

    async with SessionFactory() as db:
        await _clear(db)
        await _seed(db)

        batch = SyncBatch.model_validate({
            "deviceID": "device-preread",
            "records": [
                _mutation("record-first", "first", mutation_id="mutation-same"),
                _mutation("record-second", "second", mutation_id="mutation-same"),
            ],
        })
        result = await apply_push(db, _actor(), batch)

        assert result.acceptedRecordIDs == ["record-first"]
        assert result.rejectedRecordIDs == ["record-second"]

        fingerprints = (
            await db.execute(
                select(func.count()).select_from(SyncMutationFingerprint).where(
                    SyncMutationFingerprint.organization_id == "org-1"
                )
            )
        ).scalar_one()
        # One fingerprint wins, not one per record: the rejected duplicate must not
        # overwrite the accepted record's request fingerprint.
        assert fingerprints == 1, (
            f"expected 1 stored fingerprint, found {fingerprints}. If this is 2, the "
            "colliding record replaced the accepted record's fingerprint and a later "
            "replay would compare the wrong request."
        )


@pytest.mark.asyncio
async def test_committed_mutation_id_rejects_a_different_request():
    """A later push reusing a committed mutation id is rejected, not accepted.

    The cross-request form of the same property, and the one a pre-read plus write-back
    gets wrong in the opposite direction: the stored fingerprint for the *committed*
    record is what the incoming collision is compared against, so overwriting it with
    the incoming record's own fingerprint would make the comparison self-equal.
    """
    from app.sync_service import apply_push

    async with SessionFactory() as db:
        await _clear(db)
        await _seed(db)
        actor = _actor()

        exact = SyncBatch.model_validate({
            "deviceID": "device-original",
            "records": [_mutation("record-original", "original", mutation_id="mutation-fixed")],
        })
        first = await apply_push(db, actor, exact)
        assert first.acceptedRecordIDs == ["record-original"]

        replay = await apply_push(db, actor, exact)
        assert replay.acceptedRecordIDs == ["record-original"], (
            "an exact replay must be accepted idempotently"
        )

        collision = SyncBatch.model_validate({
            "deviceID": "device-other",
            "records": [_mutation("record-collision", "different", mutation_id="mutation-fixed")],
        })
        rejected = await apply_push(db, actor, collision)
        assert rejected.rejectedRecordIDs == ["record-collision"], (
            "a reused mutation id carrying different request bytes must be rejected"
        )

        changes = (
            await db.execute(
                select(func.count()).select_from(SyncChangeLog).where(
                    SyncChangeLog.organization_id == "org-1"
                )
            )
        ).scalar_one()
        assert changes == 1, (
            f"only the original record may reach the change log, found {changes} entries"
        )
