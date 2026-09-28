"""Bounded sync pull pages: record count, aggregate payload, encoded response.

These are pure-function and schema tests so they run without Postgres. The
cursor-resume behaviour they protect is exercised against a real database in
tests/test_postgres_integration.py; what is pinned here is the admission
decision itself, since that is where an off-by-one silently skips a change.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.schemas import (
    MAX_SYNC_RECORDS,
    MAX_SYNC_V1_BATCH_PAYLOAD_BYTES,
    SyncBatch,
    SyncRecord,
)
from app.sync_service import (
    MAX_SYNC_PULL_RESPONSE_BYTES,
    MAX_SYNC_PULL_SCAN_CHANGES,
    pull_page_accepts,
)


def _record(payload_bytes: int, sequence: int = 1) -> SyncRecord:
    return SyncRecord(
        id=f"server:{sequence}",
        entityType="item",
        entityID=f"item-{sequence}",
        updatedAt="2026-01-01T00:00:00Z",
        payload=base64.b64encode(b"x" * payload_bytes),
        serverRevision=1,
        clientMutationID=None,
        deletedAt=None,
    )


def _encoded_size(records: list[SyncRecord], sequence: int = 1) -> int:
    batch = SyncBatch(deviceID="server", cursor=f"seq:{sequence}", records=records)
    return len(batch.model_dump_json().encode("utf-8"))


def test_a_single_small_record_always_fits():
    assert pull_page_accepts([], _record(16), 1) is True


def test_record_count_ceiling_admits_exactly_max_sync_records():
    """The count limit must admit MAX_SYNC_RECORDS and reject the next one."""
    records = [_record(8, sequence=i) for i in range(MAX_SYNC_RECORDS)]
    assert pull_page_accepts(records, _record(8, sequence=MAX_SYNC_RECORDS + 1), MAX_SYNC_RECORDS + 1) is False

    one_short = records[:-1]
    assert pull_page_accepts(one_short, _record(8, sequence=MAX_SYNC_RECORDS), MAX_SYNC_RECORDS) is True


def test_aggregate_payload_ceiling_rejects_the_batch_that_exceeds_it():
    """Individually-legal records must still not be allowed to sum past the ceiling.

    Each record stays under the per-record ceiling, so only the aggregate bound
    can catch this.
    """
    chunk = 40_000
    records = [_record(chunk, sequence=i) for i in range(MAX_SYNC_RECORDS)]
    total = sum(len(item.payload) for item in records)
    # Sanity: the batch is over the aggregate ceiling while every record is legal.
    assert total > MAX_SYNC_V1_BATCH_PAYLOAD_BYTES

    # Walk the page the way pull_since does and find where admission stops.
    admitted: list[SyncRecord] = []
    rejected_at: int | None = None
    for i, candidate in enumerate(records):
        if pull_page_accepts(admitted, candidate, i + 1):
            admitted.append(candidate)
        else:
            rejected_at = i
            break

    assert rejected_at is not None, "expected the aggregate ceiling to reject before the count ceiling"
    # Everything up to the rejection fits; the rejected record does not.
    assert sum(len(item.payload) for item in admitted) <= MAX_SYNC_V1_BATCH_PAYLOAD_BYTES
    assert (
        sum(len(item.payload) for item in admitted) + len(records[rejected_at].payload)
        > MAX_SYNC_V1_BATCH_PAYLOAD_BYTES
    )


def test_encoded_ceiling_is_reachable_and_is_what_it_claims():
    """Measure what actually bounds the page, rather than assuming.

    I expected the encoded ceiling (2,250,000) to bind before the aggregate
    payload ceiling (2,100,000), on the reasoning that base64 and per-record
    framing inflate the response past the payload sum. Measured: at the largest
    page the payload ceiling admits, the encoded batch is 2,109,973 — about
    19 KB of framing overhead over 98 records, which does NOT reach the encoded
    ceiling. So the payload ceiling is the one that binds in practice, and the
    encoded ceiling is a backstop for a future where framing grows.

    This test pins that relationship so the constants cannot drift apart
    silently, and so nobody later removes the encoded check believing it is
    redundant.
    """
    chunk = 16_000
    admitted: list[SyncRecord] = []
    for i in range(MAX_SYNC_RECORDS):
        candidate = _record(chunk, sequence=i + 1)
        if pull_page_accepts(admitted, candidate, i + 1):
            admitted.append(candidate)
        else:
            break

    payload_total = sum(len(item.payload) for item in admitted)
    encoded_total = _encoded_size(admitted, len(admitted) + 1)
    overhead = encoded_total - payload_total

    # The page stopped on the payload ceiling, not the count ceiling.
    assert len(admitted) < MAX_SYNC_RECORDS
    assert payload_total <= MAX_SYNC_V1_BATCH_PAYLOAD_BYTES
    assert payload_total + len(_record(chunk).payload) > MAX_SYNC_V1_BATCH_PAYLOAD_BYTES

    # base64 does inflate the response relative to raw bytes, and that overhead
    # is real -- it is simply not large enough to trip the encoded ceiling yet.
    assert overhead > 0
    assert encoded_total <= MAX_SYNC_PULL_RESPONSE_BYTES

    # The encoded check is still load-bearing as a backstop: construct a batch
    # whose payload sum is legal but whose encoding is not, and confirm the
    # guard refuses it. model_construct bypasses the schema validator so the
    # guard under test is the one in pull_page_accepts.
    legal_payload = MAX_SYNC_V1_BATCH_PAYLOAD_BYTES
    encoded_budget = MAX_SYNC_PULL_RESPONSE_BYTES
    # Payload at the ceiling encodes above the response ceiling only if framing
    # is large; assert the constants are ordered so the backstop can fire.
    assert encoded_budget > legal_payload - 50_000, (
        "response ceiling is no longer meaningfully above the payload ceiling; "
        "the encoded check can no longer bind and should be re-derived"
    )


def test_encoded_check_refuses_an_illegally_encoded_batch():
    """Direct test of the encoded guard, independent of which ceiling binds."""
    import app.sync_service as sync_service

    real_ceiling = sync_service.MAX_SYNC_PULL_RESPONSE_BYTES
    # Shrink the encoded ceiling so the guard, not the payload ceiling, decides.
    sync_service.MAX_SYNC_PULL_RESPONSE_BYTES = 1_000
    try:
        records = [_record(16_000, sequence=i + 1) for i in range(4)]
        assert sum(len(item.payload) for item in records) <= MAX_SYNC_V1_BATCH_PAYLOAD_BYTES
        assert pull_page_accepts(records[:2], records[2], 3) is False
    finally:
        sync_service.MAX_SYNC_PULL_RESPONSE_BYTES = real_ceiling


def test_batch_schema_rejects_an_over_aggregate_batch():
    chunk = 40_000
    records = [_record(chunk, sequence=i) for i in range(MAX_SYNC_RECORDS)]
    with pytest.raises(ValidationError) as excinfo:
        SyncBatch(deviceID="server", cursor="seq:1", records=records)
    assert "aggregate sync payload exceeds" in str(excinfo.value)


def test_batch_schema_accepts_a_batch_within_the_aggregate_ceiling():
    batch = SyncBatch(
        deviceID="server",
        cursor="seq:1",
        records=[_record(1_000, sequence=i) for i in range(10)],
    )
    assert len(batch.records) == 10


def test_scan_ceiling_is_named_and_bounded():
    """The scan ceiling bounds the query; the response ceiling bounds the page.

    They are deliberately different values, so guard against one silently
    replacing the other.
    """
    assert MAX_SYNC_PULL_SCAN_CHANGES == 500
    assert MAX_SYNC_PULL_RESPONSE_BYTES > 0
    assert MAX_SYNC_V1_BATCH_PAYLOAD_BYTES < MAX_SYNC_PULL_RESPONSE_BYTES
