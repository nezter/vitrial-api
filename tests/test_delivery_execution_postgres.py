from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from app.auth import Principal
from app.db import SessionFactory
from app.idempotency import SyncMutationFingerprint
from app.models import (
    CanonicalCustomer,
    CanonicalProject,
    CanonicalProjectChild,
    Organization,
    SyncChangeLog,
    SyncEntity,
    SyncMutation,
)
from app.schemas import SyncBatch
from app.sync_service import apply_push, pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

NOW = "2026-09-17T14:00:00Z"


def principal(*, capabilities: set[str]) -> Principal:
    return Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=7,
        capabilities=frozenset(capabilities),
        customer_ids=frozenset({"customer-1"}),
        project_ids=frozenset({"project-1"}),
        all_customers=False,
        all_projects=False,
    )


def provenance(actor: Principal) -> dict:
    return {
        "actorID": actor.user_id,
        "organizationID": actor.organization_id,
        "membershipID": actor.membership_id,
        "sessionID": actor.session_id,
        "authorizationRevision": actor.authorization_revision,
    }


def encoded(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def record(payload: dict, mutation_id: str, *, base_revision: int | None = None) -> dict:
    return {
        "id": mutation_id,
        "entityType": "delivery_execution",
        "entityID": payload["id"],
        "updatedAt": payload["updatedAt"],
        "payload": encoded(payload),
        "baseServerRevision": base_revision,
        "clientMutationID": mutation_id,
    }


def delivery_payload(actor: Principal) -> dict:
    return {
        "id": "delivery-1",
        "quotationID": "quotation-1",
        "quotationNumber": "Q-2026-001",
        "quotationRevision": 2,
        "projectID": "project-1",
        "customerID": "customer-1",
        "currencyCode": "COP",
        "quotedTotal": 450,
        "pricingVersion": "price-book-v1",
        "startedAt": NOW,
        "updatedAt": NOW,
        "status": "engineeringReview",
        "events": [{
            "id": "delivery-event-1",
            "fromStatus": None,
            "toStatus": "engineeringReview",
            "occurredAt": NOW,
            "note": None,
            **provenance(actor),
        }],
    }


async def clear_database(db) -> None:
    for model in (
        SyncMutationFingerprint,
        SyncChangeLog,
        SyncMutation,
        SyncEntity,
        CanonicalProjectChild,
        CanonicalProject,
        CanonicalCustomer,
        Organization,
    ):
        await db.execute(delete(model))
    await db.commit()


async def seed_approved_quotation(db, actor: Principal) -> None:
    # Flush the FK root before inserting canonical children. These models intentionally do not
    # carry ORM relationship metadata, so SQLAlchemy cannot infer insert ordering for us.
    db.add(Organization(id="org-1", name="Vitrial", authorization_revision=7))
    await db.flush()
    db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-1"))
    await db.flush()
    db.add(CanonicalProject(
        organization_id="org-1",
        project_id="project-1",
        customer_id="customer-1",
    ))
    await db.flush()
    db.add(CanonicalProjectChild(
        organization_id="org-1",
        entity_type="quotation",
        entity_id="quotation-1",
        project_id="project-1",
    ))
    db.add(SyncEntity(
        organization_id="org-1",
        entity_type="quotation",
        entity_id="quotation-1",
        server_revision=10,
        schema_version=1,
        payload_json={
            "id": "quotation-1",
            "projectID": "project-1",
            "customerID": "customer-1",
            "number": "Q-2026-001",
            "status": "approved",
            "currencyCode": "COP",
            "lines": [{
                "id": "line-1",
                "itemID": "item-1",
                "quantity": 3,
                "unitPrice": 150,
                "totalPrice": 450,
            }],
            "revision": 2,
            "pricingVersion": "price-book-v1",
            "events": [{
                "id": "quotation-approved-1",
                "kind": "approved",
                **provenance(actor),
            }],
        },
        updated_at=datetime.now(timezone.utc),
        deleted_at=None,
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_delivery_execution_create_advance_pull_and_authority():
    actor = principal(capabilities={"sync", "delivery.manage"})
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_approved_quotation(db, actor)

        initial = delivery_payload(actor)
        created = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "delivery-device-1",
            "records": [record(initial, "delivery-create")],
        }))
        assert created.acceptedRecordIDs == ["delivery-create"]

        canonical = await db.get(
            CanonicalProjectChild,
            ("org-1", "delivery_execution", "delivery-1"),
        )
        assert canonical is not None and canonical.project_id == "project-1"
        stored = await db.get(
            SyncEntity,
            ("org-1", "delivery_execution", "delivery-1"),
        )
        assert stored is not None and stored.server_revision == 11

        pulled = await pull_since(db, actor, "seq:0")
        delivery_records = [
            value for value in pulled.records
            if value.entityType == "delivery_execution"
        ]
        assert len(delivery_records) == 1
        assert delivery_records[0].entityID == "delivery-1"

        next_time = "2026-09-17T14:05:00Z"
        advanced = dict(initial)
        advanced["status"] = "materialsRequired"
        advanced["updatedAt"] = next_time
        advanced["events"] = initial["events"] + [{
            "id": "delivery-event-2",
            "fromStatus": "engineeringReview",
            "toStatus": "materialsRequired",
            "occurredAt": next_time,
            "note": "Engineering review complete",
            **provenance(actor),
        }]
        result = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "delivery-device-1",
            "records": [record(
                advanced,
                "delivery-materials",
                base_revision=stored.server_revision,
            )],
        }))
        assert result.acceptedRecordIDs == ["delivery-materials"]

        denied_actor = principal(capabilities={"sync"})
        tampered = dict(advanced)
        tampered["quotedTotal"] = 999
        denied = await apply_push(db, denied_actor, SyncBatch.model_validate({
            "deviceID": "delivery-device-2",
            "records": [record(
                tampered,
                "delivery-denied",
                base_revision=12,
            )],
        }))
        assert denied.rejectedRecordIDs == ["delivery-denied"]
        await clear_database(db)


@pytest.mark.asyncio
async def test_delivery_execution_rejects_noncanonical_handoff():
    actor = principal(capabilities={"sync", "delivery.manage"})
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_approved_quotation(db, actor)

        wrong = delivery_payload(actor)
        wrong["quotationNumber"] = "FORGED"
        rejected = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "delivery-device-1",
            "records": [record(wrong, "delivery-forged")],
        }))
        assert rejected.rejectedRecordIDs == ["delivery-forged"]
        assert await db.get(
            CanonicalProjectChild,
            ("org-1", "delivery_execution", "delivery-1"),
        ) is None
        await clear_database(db)
