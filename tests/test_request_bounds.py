from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import MAX_SYNC_RECORDS, SyncBatch, SyncRecord
from app.sync_v2 import MAX_SYNC_V2_PAYLOAD_BYTES, SyncBatchV2, SyncRecordV2

NOW = datetime.now(timezone.utc)


def v1_record(index: int) -> SyncRecord:
    return SyncRecord(
        id=f"record-{index}",
        entityType="customer",
        entityID=f"customer-{index}",
        updatedAt=NOW,
        payload=b"e30=",
        clientMutationID=f"mutation-{index}",
    )


def v2_record(index: int) -> SyncRecordV2:
    return SyncRecordV2(
        id=f"record-{index}",
        entityType="customer",
        entityID=f"customer-{index}",
        updatedAt=NOW,
        payload={"name": f"Customer {index}"},
        clientMutationID=f"mutation-{index}",
    )


def test_v1_sync_batch_accepts_client_contract_ceiling():
    batch = SyncBatch(deviceID="device-1", records=[v1_record(i) for i in range(MAX_SYNC_RECORDS)])
    assert len(batch.records) == MAX_SYNC_RECORDS


def test_v1_sync_batch_rejects_more_than_client_contract_ceiling():
    with pytest.raises(ValidationError):
        SyncBatch(deviceID="device-1", records=[v1_record(i) for i in range(MAX_SYNC_RECORDS + 1)])


def test_v2_sync_batch_rejects_more_than_client_contract_ceiling():
    with pytest.raises(ValidationError):
        SyncBatchV2(deviceID="device-1", records=[v2_record(i) for i in range(MAX_SYNC_RECORDS + 1)])


def test_v2_record_rejects_oversized_native_json_payload():
    with pytest.raises(ValidationError):
        SyncRecordV2(
            id="record-large",
            entityType="customer",
            entityID="customer-large",
            updatedAt=NOW,
            payload={"blob": "x" * (MAX_SYNC_V2_PAYLOAD_BYTES + 1)},
            clientMutationID="mutation-large",
        )
