from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.schemas import (
    EntityType,
    MAX_SYNC_IDENTIFIER_LENGTH,
    MAX_SYNC_RECORDS,
    StrictModel,
    SyncBatch,
    SyncRecord,
)
from app.sync_service import InvalidMutation, apply_push, decode_payload, pull_since

MAX_SYNC_V2_PAYLOAD_BYTES = 1_500_000


def _canonical_json_bytes(payload: dict[str, JsonValue]) -> bytes:
    return json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")


class SyncRecordV2(StrictModel):
    id: str = Field(min_length=1, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    entityType: EntityType
    entityID: str = Field(min_length=1, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    updatedAt: datetime
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    entitySchemaVersion: int = Field(default=1, ge=1)
    baseServerRevision: int | None = Field(default=None, ge=0)
    serverRevision: int | None = Field(default=None, ge=0)
    clientMutationID: str | None = Field(default=None, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    deletedAt: datetime | None = None

    @field_validator("payload")
    @classmethod
    def payload_must_be_bounded(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(_canonical_json_bytes(value)) > MAX_SYNC_V2_PAYLOAD_BYTES:
            raise ValueError(
                f"canonical payload exceeds {MAX_SYNC_V2_PAYLOAD_BYTES} bytes"
            )
        return value


class SyncBatchV2(StrictModel):
    protocolVersion: Literal[2] = 2
    deviceID: str = Field(min_length=1, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    cursor: str | None = Field(default=None, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    records: list[SyncRecordV2] = Field(default_factory=list, max_length=MAX_SYNC_RECORDS)


class SyncResultV2(StrictModel):
    protocolVersion: Literal[2] = 2
    acceptedRecordIDs: list[str] = Field(default_factory=list)
    rejectedRecordIDs: list[str] = Field(default_factory=list)
    nextCursor: str | None = None


class VersionResponseV2(StrictModel):
    apiVersion: Literal["v2"] = "v2"
    protocolVersion: Literal[2] = 2
    serviceVersion: str


def _to_v1_record(record: SyncRecordV2) -> SyncRecord:
    # The current canonical entity schema is version 1. The explicit wire field
    # exists now so future schema versions can be negotiated without ever
    # reinterpreting V1 bytes. Unsupported versions fail closed.
    if record.entitySchemaVersion != 1:
        raise InvalidMutation(
            f"unsupported entity schema version {record.entitySchemaVersion}; current version is 1"
        )
    return SyncRecord(
        id=record.id,
        entityType=record.entityType,
        entityID=record.entityID,
        updatedAt=record.updatedAt,
        payload=_canonical_json_bytes(record.payload),
        baseServerRevision=record.baseServerRevision,
        serverRevision=record.serverRevision,
        clientMutationID=record.clientMutationID,
        deletedAt=record.deletedAt,
    )


def _from_v1_record(record: SyncRecord) -> SyncRecordV2:
    return SyncRecordV2(
        id=record.id,
        entityType=record.entityType,
        entityID=record.entityID,
        updatedAt=record.updatedAt,
        payload=decode_payload(record),
        entitySchemaVersion=1,
        baseServerRevision=record.baseServerRevision,
        serverRevision=record.serverRevision,
        clientMutationID=record.clientMutationID,
        deletedAt=record.deletedAt,
    )


async def apply_push_v2(
    db: AsyncSession,
    principal: Principal,
    batch: SyncBatchV2,
) -> SyncResultV2:
    v1_batch = SyncBatch(
        deviceID=batch.deviceID,
        cursor=batch.cursor,
        records=[_to_v1_record(record) for record in batch.records],
    )
    result = await apply_push(db, principal, v1_batch)
    return SyncResultV2(
        acceptedRecordIDs=result.acceptedRecordIDs,
        rejectedRecordIDs=result.rejectedRecordIDs,
        nextCursor=result.nextCursor,
    )


async def pull_since_v2(
    db: AsyncSession,
    principal: Principal,
    cursor: str | None,
) -> SyncBatchV2:
    v1_batch = await pull_since(db, principal, cursor)
    return SyncBatchV2(
        deviceID=v1_batch.deviceID,
        cursor=v1_batch.cursor,
        records=[_from_v1_record(record) for record in v1_batch.records],
    )
