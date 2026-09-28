from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Capability = Literal[
    "customer.create", "customer.edit", "project.create", "project.edit",
    "item.create", "item.edit", "item.readiness.change", "item.readiness.override",
    "item.blockers.manage", "item.measurements.manage", "item.evidence.manage",
    "item.requirements.manage", "item.configuration.manage",
    "item.configuration.finalize", "item.configuration.reopen",
    "quotation.create", "quotation.send", "quotation.approve",
    "delivery.manage", "catalog.manage", "pricing.manage",
    "organization.members.manage", "organization.roles.manage", "sync",
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
    id: str
    entityType: EntityType
    entityID: str
    updatedAt: datetime
    payload: bytes
    baseServerRevision: int | None = Field(default=None, ge=0)
    serverRevision: int | None = Field(default=None, ge=0)
    clientMutationID: str | None = None
    deletedAt: datetime | None = None

class SyncBatch(StrictModel):
    deviceID: str
    cursor: str | None = None
    records: list[SyncRecord] = Field(default_factory=list)

class SyncResult(StrictModel):
    acceptedRecordIDs: list[str] = Field(default_factory=list)
    rejectedRecordIDs: list[str] = Field(default_factory=list)
    nextCursor: str | None = None
