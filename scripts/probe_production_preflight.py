#!/usr/bin/env python3
"""Read-only production-provider preflight.

This probe intentionally does not run migrations, write an S3 canary, repair
canonical ownership, or exercise backup/restore. It answers only questions that
can be proven without mutating provider state:

* Is the configured PostgreSQL provider reachable?
* Is the database at the repository's Alembic head?
* Does the configured API pool budget fit below provider max_connections?
* Are there any half-canonical delivery_execution rows?
* Is the configured S3 bucket reachable through the application's credential?

Provider backup/PITR policy and object-storage versioning/restore durability
remain separate operator evidence and are not implied by a green result here.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db import engine
from app.readiness import probe_storage
from app.settings import settings


@dataclass(frozen=True)
class Snapshot:
    database_revision: str | None
    expected_revision: str
    max_connections: int
    current_connections: int
    pool_budget: int | None
    orphan_payload_rows: int
    orphan_ownership_rows: int
    tombstoned_payload_rows: int
    tombstoned_ownership_rows: int
    object_storage: str


def evaluate(snapshot: Snapshot) -> list[str]:
    errors: list[str] = []
    if snapshot.database_revision != snapshot.expected_revision:
        errors.append("database migration revision is not at repository head")
    if snapshot.pool_budget is not None and snapshot.pool_budget >= snapshot.max_connections:
        errors.append("configured API pool budget consumes provider max_connections")
    if snapshot.orphan_payload_rows:
        errors.append("delivery_execution payload rows exist without canonical ownership")
    if snapshot.orphan_ownership_rows:
        errors.append("delivery_execution ownership rows exist without payload rows")
    if snapshot.tombstoned_payload_rows:
        errors.append("tombstoned delivery_execution payload rows exist")
    if snapshot.tombstoned_ownership_rows:
        errors.append("tombstoned delivery_execution ownership rows exist")
    if snapshot.object_storage != "ok":
        errors.append("object storage is unavailable")
    return errors


def expected_alembic_head() -> str:
    config = Config("alembic.ini")
    return ScriptDirectory.from_config(config).get_current_head()


async def collect_snapshot() -> Snapshot:
    if settings.evidence_storage_provider != "s3":
        raise RuntimeError("production preflight requires EVIDENCE_STORAGE_PROVIDER=s3")

    async with engine.connect() as connection:
        database_revision = await connection.scalar(text(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ))
        max_connections = int(await connection.scalar(text("SHOW max_connections")))
        current_connections = int(await connection.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )))

        orphan_payload_rows = int(await connection.scalar(text("""
            SELECT count(*)
            FROM sync_entities e
            LEFT JOIN canonical_project_children c
              ON c.organization_id = e.organization_id
             AND c.entity_type = 'delivery_execution'
             AND c.entity_id = e.entity_id
            WHERE e.entity_type = 'delivery_execution'
              AND c.entity_id IS NULL
        """)))
        orphan_ownership_rows = int(await connection.scalar(text("""
            SELECT count(*)
            FROM canonical_project_children c
            LEFT JOIN sync_entities e
              ON e.organization_id = c.organization_id
             AND e.entity_type = 'delivery_execution'
             AND e.entity_id = c.entity_id
            WHERE c.entity_type = 'delivery_execution'
              AND e.entity_id IS NULL
        """)))
        tombstoned_payload_rows = int(await connection.scalar(text("""
            SELECT count(*)
            FROM sync_entities
            WHERE entity_type = 'delivery_execution'
              AND deleted_at IS NOT NULL
        """)))
        tombstoned_ownership_rows = int(await connection.scalar(text("""
            SELECT count(*)
            FROM canonical_project_children
            WHERE entity_type = 'delivery_execution'
              AND deleted_at IS NOT NULL
        """)))

    try:
        await probe_storage()
        object_storage = "ok"
    except Exception:
        object_storage = "unavailable"

    pool_budget = None
    if not settings.database_null_pool:
        pool_budget = settings.database_pool_size + settings.database_max_overflow

    return Snapshot(
        database_revision=str(database_revision) if database_revision is not None else None,
        expected_revision=expected_alembic_head(),
        max_connections=max_connections,
        current_connections=current_connections,
        pool_budget=pool_budget,
        orphan_payload_rows=orphan_payload_rows,
        orphan_ownership_rows=orphan_ownership_rows,
        tombstoned_payload_rows=tombstoned_payload_rows,
        tombstoned_ownership_rows=tombstoned_ownership_rows,
        object_storage=object_storage,
    )


async def run() -> int:
    try:
        snapshot = await collect_snapshot()
    except Exception as exc:
        print(json.dumps({
            "status": "not-ready",
            "errorType": type(exc).__name__,
        }, sort_keys=True))
        return 2

    errors = evaluate(snapshot)
    payload = {
        "status": "ready" if not errors else "not-ready",
        "serviceVersion": settings.service_version,
        "database": {
            "revision": snapshot.database_revision,
            "expectedRevision": snapshot.expected_revision,
            "maxConnections": snapshot.max_connections,
            "currentConnections": snapshot.current_connections,
            "poolBudget": snapshot.pool_budget,
            "poolHeadroomAtConfiguredCeiling": (
                snapshot.max_connections - snapshot.pool_budget
                if snapshot.pool_budget is not None else None
            ),
            "nullPool": settings.database_null_pool,
        },
        "deliveryExecution": {
            "payloadRowsWithoutOwnership": snapshot.orphan_payload_rows,
            "ownershipRowsWithoutPayload": snapshot.orphan_ownership_rows,
            "tombstonedPayloadRows": snapshot.tombstoned_payload_rows,
            "tombstonedOwnershipRows": snapshot.tombstoned_ownership_rows,
        },
        "objectStorage": snapshot.object_storage,
        "durabilityClaimed": False,
    }
    if errors:
        payload["errors"] = errors
    print(json.dumps(payload, sort_keys=True))
    return 0 if not errors else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
