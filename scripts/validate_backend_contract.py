#!/usr/bin/env python3
"""Dependency-free sanity checks for the portable Connected Operations backend contract pack."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "backend" / "v1"

CAPABILITIES = {
    "customer.create", "customer.edit", "project.create", "project.edit",
    "item.create", "item.edit", "item.readiness.change", "item.readiness.override",
    "item.blockers.manage", "item.measurements.manage", "item.evidence.manage",
    "item.requirements.manage", "item.configuration.manage", "item.configuration.finalize",
    "item.configuration.reopen", "quotation.create", "quotation.send", "quotation.approve",
    "delivery.manage", "catalog.manage", "pricing.manage", "organization.members.manage",
    "organization.roles.manage", "sync",
}
ENTITY_TYPES = {
    "customer", "project", "project_sector", "item", "item_audit_event", "measurement",
    "evidence", "customer_requirement", "configuration", "configuration_version", "blocker",
    "quotation", "delivery_execution",
}
REQUIRED_PATHS = {
    "/health", "/api/v1/version", "/api/v1/auth/me", "/api/v1/sync/push",
    "/api/v1/sync/pull", "/api/v1/sync/evidence-blobs/{documentID}",
}


def load(name: str):
    with (CONTRACT / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"backend contract validation failed: {message}")


def parse_date(value: str) -> datetime:
    require(isinstance(value, str) and value, "date must be a non-empty string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"backend contract validation failed: invalid ISO-8601 date {value!r}: {exc}")


def validate_record(record: dict, *, pulled: bool) -> None:
    for key in ("id", "entityType", "entityID", "updatedAt", "payload"):
        require(record.get(key) not in (None, ""), f"SyncRecord missing {key}")
    require(record["entityType"] in ENTITY_TYPES, f"unknown SyncEntityType {record['entityType']}")
    parse_date(record["updatedAt"])
    try:
        payload = base64.b64decode(record["payload"], validate=True)
        json.loads(payload)
    except Exception as exc:
        raise SystemExit(f"backend contract validation failed: SyncRecord payload is not base64 JSON: {exc}")
    if pulled:
        require(isinstance(record.get("serverRevision"), int) and record["serverRevision"] >= 0,
                "pull record requires non-negative serverRevision")
        require(bool(record.get("clientMutationID")), "pull acknowledgement must echo clientMutationID")
    else:
        require(isinstance(record.get("baseServerRevision"), int) and record["baseServerRevision"] >= 0,
                "push fixture requires non-negative baseServerRevision")
        require(bool(record.get("clientMutationID")), "push mutation requires clientMutationID")


def main() -> None:
    spec = (CONTRACT / "openapi.yaml").read_text(encoding="utf-8")
    for path in REQUIRED_PATHS:
        require(path in spec, f"OpenAPI missing {path}")
    for capability in CAPABILITIES:
        require(f"- {capability}" in spec, f"OpenAPI missing capability {capability}")
    for entity_type in ENTITY_TYPES:
        require(f"- {entity_type}" in spec, f"OpenAPI missing entity type {entity_type}")

    profile = load("auth-me.connected.json")
    for key in (
        "principalID", "displayName", "customerIDs", "allCustomers", "capabilities",
        "issuedAt", "expiresAt", "organizationID", "membershipID", "sessionID",
        "authorizationRevision", "allProjects",
    ):
        require(key in profile, f"auth fixture missing {key}")
    require(profile["principalID"].strip(), "principalID must be non-empty")
    require(profile["organizationID"].strip(), "organizationID must be non-empty")
    require(profile["membershipID"].strip(), "membershipID must be non-empty")
    require(profile["sessionID"].strip(), "sessionID must be non-empty")
    require(isinstance(profile["authorizationRevision"], int) and profile["authorizationRevision"] >= 0,
            "authorizationRevision must be non-negative")
    require(isinstance(profile["allProjects"], bool), "allProjects must be explicit in connected mode")
    require(set(profile["capabilities"]).issubset(CAPABILITIES), "auth fixture contains unknown capabilities")
    require(parse_date(profile["issuedAt"]) < parse_date(profile["expiresAt"]),
            "auth validity window is invalid")

    push = load("sync-push.request.json")
    require(push.get("deviceID"), "push fixture requires deviceID")
    require(len(push.get("records", [])) == 1, "push fixture must contain exactly one canonical record")
    validate_record(push["records"][0], pulled=False)

    push_result = load("sync-push.response.json")
    require(push_result.get("acceptedRecordIDs") == [push["records"][0]["id"]],
            "push result must accept canonical record")
    require(push_result.get("rejectedRecordIDs") == [], "canonical push result must reject nothing")

    pull = load("sync-pull.response.json")
    require(pull.get("deviceID"), "pull fixture requires deviceID")
    require(len(pull.get("records", [])) == 1, "pull fixture must contain one acknowledgement record")
    validate_record(pull["records"][0], pulled=True)
    require(pull["records"][0]["clientMutationID"] == push["records"][0]["clientMutationID"],
            "pull must echo the committed clientMutationID")
    require(pull["records"][0]["entityID"] == push["records"][0]["entityID"],
            "pull acknowledgement must reference the pushed entity")

    print("backend contract validation: PASS")


if __name__ == "__main__":
    main()
