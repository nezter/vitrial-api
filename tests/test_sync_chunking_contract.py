"""The chunking contract between the iOS client and this server.

Two independent numbers bound a sync push, and they live in different repositories:

* the iOS client chunks at `SyncPushBatchPolicy.maxPayloadBytes`, default 1_500_000
  (`Sources/VitrialAluminiosKit/SyncBatchChunking.swift`), measured as
  `record.payload.count` -- **raw** `Data`, before base64
* this server rejects a batch whose records sum above
  `MAX_SYNC_V1_BATCH_PAYLOAD_BYTES` (2_100_000), and a single record above
  `MAX_SYNC_V1_WIRE_PAYLOAD_BYTES` (the same 2_100_000)

Nothing on either side asserts the relationship, so the two numbers can drift apart and
a client can start producing batches the server rejects -- with the failure appearing
only on a real push, at the largest batch size, in front of a user.

This asserts the relationship holds, and pins the arithmetic that makes it hold. The
non-obvious part, and the reason a naive reading gets it wrong:

* the client counts **raw** bytes
* the server sums `len(record.payload)` where Pydantic has already **decoded** base64

So both measure the same quantity. The 4/3 base64 expansion applies only to the HTTP
body, never to the aggregate cap. An earlier note claimed the expansion needed ~2.1 MB
of headroom against a 2.1 MB cap; it does not -- the expansion is 2.00 MB at the very
worst case, and the aggregate cap has 600,000 bytes of headroom over the client budget.
"""

from __future__ import annotations

import base64

import pytest

from app.schemas import (
    MAX_SYNC_RECORDS,
    MAX_SYNC_V1_BATCH_PAYLOAD_BYTES,
    MAX_SYNC_V1_WIRE_PAYLOAD_BYTES,
    SyncBatch,
    SyncRecord,
)

# The client's default, mirrored from
# `Sources/VitrialAluminiosKit/SyncBatchChunking.swift`. Duplicated deliberately: this
# test fails if either side changes, which is the point. A shared constant would hide
# exactly the drift it is meant to catch.
IOS_CLIENT_PAYLOAD_BUDGET = 1_500_000
IOS_CLIENT_RECORD_BUDGET = 200

NOW = "2026-09-17T14:00:00Z"


def _record(index: int, size: int) -> dict:
    return {
        "id": f"client:{index}",
        "entityType": "item",
        "entityID": f"item-{index}",
        "updatedAt": NOW,
        "payload": b"\0" * size,
        "clientMutationID": f"cm-{index}",
    }


def test_a_batch_at_the_client_budget_is_accepted_by_the_server():
    """The whole client budget, in as few records as the client would use.

    The client fills a chunk until the cumulative raw payload reaches its budget, so the
    worst case is a small number of large records -- or, if one record exceeds the
    budget on its own, that record alone. Both are covered below; this is the common
    case.
    """
    # 200 records is the client's record cap, so the budget is spread across at most
    # that many. Spreading evenly is what the chunker does.
    per_record = IOS_CLIENT_PAYLOAD_BUDGET // IOS_CLIENT_RECORD_BUDGET
    assert IOS_CLIENT_PAYLOAD_BUDGET < MAX_SYNC_V1_BATCH_PAYLOAD_BYTES, (
        "the client's raw payload budget exceeds the server's aggregate cap, so a "
        "chunk the client considers valid would be rejected"
    )
    assert IOS_CLIENT_RECORD_BUDGET <= MAX_SYNC_RECORDS, (
        f"the client allows {IOS_CLIENT_RECORD_BUDGET} records per batch but the server "
        f"accepts at most {MAX_SYNC_RECORDS}"
    )

    batch = SyncBatch.model_validate({
        "deviceID": "device-contract",
        "records": [_record(i, per_record) for i in range(IOS_CLIENT_RECORD_BUDGET)],
    })
    assert len(batch.records) == IOS_CLIENT_RECORD_BUDGET
    total = sum(len(r.payload) for r in batch.records)
    assert total <= MAX_SYNC_V1_BATCH_PAYLOAD_BYTES, (
        f"a batch at the client's budget totals {total} bytes, above the server's "
        f"{MAX_SYNC_V1_BATCH_PAYLOAD_BYTES}"
    )


def test_a_single_record_filling_the_client_budget_is_accepted():
    """One record consuming the entire client budget on its own.

    The chunker emits an oversized record alone rather than looping, so the server must
    still accept a single record as large as the client's whole budget. This is the
    case where the base64 expansion actually applies: the record's own wire size grows
    by 4/3 even though the aggregate cap does not.
    """
    raw = IOS_CLIENT_PAYLOAD_BUDGET
    batch = SyncBatch.model_validate({
        "deviceID": "device-contract",
        "records": [_record(0, raw)],
    })
    assert sum(len(r.payload) for r in batch.records) <= MAX_SYNC_V1_BATCH_PAYLOAD_BYTES

    # The expanded wire size, which is what the per-record ceiling is there to bound.
    on_wire = len(base64.b64encode(b"\0" * raw))
    assert on_wire <= MAX_SYNC_V1_WIRE_PAYLOAD_BYTES, (
        f"a record filling the client budget is {on_wire:,} bytes base64-encoded, above "
        f"the server's per-record ceiling of {MAX_SYNC_V1_WIRE_PAYLOAD_BYTES:,}"
    )


def test_the_aggregate_cap_does_not_see_the_base64_expansion():
    """The cap is measured on decoded bytes on both sides.

    This is the part that is easy to get wrong by reading. `SyncRecord.payload` is
    typed `bytes`, so Pydantic has already decoded base64 by the time the validator
    sums `len(record.payload)`. The client counts `Data.count`, which is pre-encoding.
    Both are raw bytes, so the 4/3 expansion never applies to the aggregate.

    Asserted explicitly because the alternative -- believing the expansion applies to
    the cap -- would suggest the client had far less headroom than it does, and might
    prompt someone to "fix" the client budget downward for no reason.
    """
    raw = 1_000_000
    expanded = len(base64.b64encode(b"\0" * raw))
    assert expanded > raw, "base64 must expand, or this test is not testing anything"

    # One million raw bytes passes the aggregate cap...
    assert raw <= MAX_SYNC_V1_BATCH_PAYLOAD_BYTES

    # ...and the cap is not compared against the expanded size. Two such records total
    # 2,000,000 raw, which is under the 2,100,000 cap even though their base64 form
    # would be ~2,666,000.
    two = SyncBatch.model_validate({
        "deviceID": "device-contract",
        "records": [_record(0, raw), _record(1, raw)],
    })
    assert sum(len(r.payload) for r in two.records) == 2_000_000
    assert sum(len(r.payload) for r in two.records) <= MAX_SYNC_V1_BATCH_PAYLOAD_BYTES
    expanded_total = 2 * len(base64.b64encode(b"\0" * raw))
    assert expanded_total > MAX_SYNC_V1_BATCH_PAYLOAD_BYTES, (
        "this test relies on the aggregate cap being smaller than the expanded total, "
        "otherwise it is not demonstrating the point"
    )


def test_the_server_rejects_a_batch_beyond_the_client_budget():
    """The other direction: the server's cap is not merely a formality.

    Without this, the tests above would pass even if the aggregate ceiling were
    effectively unlimited, and the contract they describe would be vacuous.
    """
    with pytest.raises(ValueError, match="aggregate sync payload exceeds"):
        SyncBatch.model_validate({
            "deviceID": "device-contract",
            "records": [
                _record(i, MAX_SYNC_V1_BATCH_PAYLOAD_BYTES) for i in range(2)
            ],
        })
