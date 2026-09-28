from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SYNC_RECORDS = 200

# Aggregate ceiling matching the iOS V1 chunking contract. Deliberately separate
# from the per-record ceiling: accepting 200 near-limit records would otherwise
# allow hundreds of megabytes in a single logical sync batch.
MAX_SYNC_V1_BATCH_PAYLOAD_BYTES = 2_100_000
MAX_SYNC_IDENTIFIER_LENGTH = 256
# Swift's V1 client budgets 1.5 MB of raw payload per batch. Data is represented as
# base64 on the JSON wire, so leave enough room for that expansion while rejecting
# anomalously large individual records.
MAX_SYNC_V1_WIRE_PAYLOAD_BYTES = 2_100_000

Capability = Literal[
    "customer.create", "customer.edit", "project.create", "project.edit",
    "item.create", "item.edit", "item.readiness.change", "item.readiness.override",
    "item.blockers.manage", "item.measurements.manage", "item.evidence.manage",
    "item.requirements.manage", "item.configuration.manage",
    "item.configuration.finalize", "item.configuration.reopen",
    "quotation.create", "quotation.send", "quotation.approve", "delivery.manage",
    "catalog.manage", "pricing.manage", "organization.members.manage",
    "organization.roles.manage", "sync",
]
EntityType = Literal[
    "customer", "project", "project_sector", "item", "item_audit_event",
    "measurement", "evidence", "customer_requirement", "configuration",
    "configuration_version", "blocker", "quotation", "delivery_execution",
]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"

class ReadinessResponse(StrictModel):
    status: Literal["ready", "not-ready"]
    serviceVersion: str
    database: Literal["ok", "unavailable"]
    objectStorage: Literal["ok", "unavailable"]

class VersionResponse(StrictModel):
    apiVersion: Literal["v1"] = "v1"
    serviceVersion: str

class AuthorizationRole(StrictModel):
    id: str
    displayName: str

class AuthorizationProfile(StrictModel):
    principalID: str
    displayName: str
    email: str | None = None
    customerIDs: list[str] = Field(default_factory=list)
    allCustomers: bool
    capabilities: list[Capability] = Field(default_factory=list)
    issuedAt: datetime
    expiresAt: datetime
    organizationID: str
    membershipID: str
    sessionID: str
    authorizationRevision: int = Field(ge=0)
    projectIDs: list[str] = Field(default_factory=list)
    allProjects: bool
    roles: list[AuthorizationRole] = Field(default_factory=list)

class SyncRecord(StrictModel):
    id: str = Field(min_length=1, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    entityType: EntityType
    entityID: str = Field(min_length=1, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    updatedAt: datetime
    payload: bytes = Field(max_length=MAX_SYNC_V1_WIRE_PAYLOAD_BYTES)
    baseServerRevision: int | None = Field(default=None, ge=0)
    serverRevision: int | None = Field(default=None, ge=0)
    clientMutationID: str | None = Field(default=None, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    deletedAt: datetime | None = None

class SyncBatch(StrictModel):
    deviceID: str = Field(min_length=1, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    cursor: str | None = Field(default=None, max_length=MAX_SYNC_IDENTIFIER_LENGTH)
    records: list[SyncRecord] = Field(default_factory=list, max_length=MAX_SYNC_RECORDS)

    @model_validator(mode="after")
    def aggregate_payload_must_be_bounded(self) -> "SyncBatch":
        total = sum(len(record.payload) for record in self.records)
        if total > MAX_SYNC_V1_BATCH_PAYLOAD_BYTES:
            raise ValueError(
                f"aggregate sync payload exceeds {MAX_SYNC_V1_BATCH_PAYLOAD_BYTES} bytes"
            )
        return self


class SyncResult(StrictModel):
    acceptedRecordIDs: list[str] = Field(default_factory=list)
    rejectedRecordIDs: list[str] = Field(default_factory=list)
    nextCursor: str | None = None
