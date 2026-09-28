import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, func, select

from app.auth import Principal, current_principal, token_hash
from app.db import SessionFactory
from app.evidence import put_blob
from app.models import (
    AuthSession,
    CanonicalCustomer,
    CanonicalItem,
    CanonicalItemChild,
    CanonicalProject,
    CanonicalProjectChild,
    CanonicalProjectSector,
    EvidenceBlob,
    Membership,
    Organization,
    SyncChangeLog,
    SyncEntity,
    SyncMutation,
    User,
)
from app.schemas import SyncBatch
from app.settings import settings
from app.sync_service import apply_push, pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-06T14:00:00Z"


async def clear_database(db):
    for model in (
        EvidenceBlob,
        SyncChangeLog,
        SyncMutation,
        SyncEntity,
        CanonicalItemChild,
        CanonicalProjectChild,
        CanonicalItem,
        CanonicalProjectSector,
        CanonicalProject,
        CanonicalCustomer,
        AuthSession,
        Membership,
        User,
        Organization,
    ):
        await db.execute(delete(model))
    await db.commit()


def encoded(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def record(
    record_id: str,
    entity_type: str,
    entity_id: str,
    payload: dict,
    mutation_id: str,
    *,
    base_revision: int | None = None,
    deleted_at: str | None = None,
) -> dict:
    return {
        "id": record_id,
        "entityType": entity_type,
        "entityID": entity_id,
        "updatedAt": NOW,
        "payload": encoded(payload),
        "baseServerRevision": base_revision,
        "clientMutationID": mutation_id,
        "deletedAt": deleted_at,
    }


def principal(
    *,
    capabilities: set[str],
    customer_ids: set[str] | None = None,
    project_ids: set[str] | None = None,
    organization_id: str = "org-1",
    membership_id: str = "membership-1",
    user_id: str = "user-1",
    session_id: str = "session-1",
    revision: int = 1,
) -> Principal:
    return Principal(
        user_id=user_id,
        organization_id=organization_id,
        membership_id=membership_id,
        session_id=session_id,
        authorization_revision=revision,
        capabilities=frozenset(capabilities),
        customer_ids=frozenset(customer_ids or set()),
        project_ids=frozenset(project_ids or set()),
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


async def seed_project_graph(db, *, second_project: bool = False):
    db.add(Organization(id="org-1", name="Vitrial", authorization_revision=1))
    await db.flush()
    db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-1"))
    await db.flush()
    db.add(CanonicalProject(
        organization_id="org-1", project_id="project-1", customer_id="customer-1"
    ))
    if second_project:
        db.add(CanonicalProject(
            organization_id="org-1", project_id="project-2", customer_id="customer-1"
        ))
    await db.flush()
    db.add(CanonicalProjectSector(
        organization_id="org-1",
        project_sector_id="project-sector-1",
        project_id="project-1",
        sector_id="sector-aluminum-glass-steel",
    ))
    if second_project:
        db.add(CanonicalProjectSector(
            organization_id="org-1",
            project_sector_id="project-sector-2",
            project_id="project-2",
            sector_id="sector-aluminum-glass-steel",
        ))
    await db.flush()
    db.add(CanonicalItem(
        organization_id="org-1",
        item_id="item-1",
        project_id="project-1",
        project_sector_id="project-sector-1",
    ))
    if second_project:
        db.add(CanonicalItem(
            organization_id="org-1",
            item_id="item-2",
            project_id="project-2",
            project_sector_id="project-sector-2",
        ))
    await db.commit()


@pytest.mark.asyncio
async def test_session_authentication_and_revocation():
    token = "integration-access-token"
    now = datetime.now(timezone.utc)
    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=9))
        db.add(User(id="user-1", display_name="Operator", email="operator@example.invalid"))
        await db.flush()
        db.add(Membership(
            id="membership-1", organization_id="org-1", user_id="user-1", active=True,
            all_customers=False, all_projects=False,
            customer_ids=["customer-1"], project_ids=["project-1"],
            roles=[{"id": "operator", "displayName": "Operator"}],
            capabilities=["sync", "item.edit"],
        ))
        await db.flush()
        session = AuthSession(
            id="session-1", organization_id="org-1", membership_id="membership-1",
            user_id="user-1", access_token_hash=token_hash(token),
            issued_at=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
        )
        db.add(session)
        await db.commit()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        authenticated = await current_principal(credentials, db)
        assert authenticated.organization_id == "org-1"
        assert authenticated.authorization_revision == 9
        assert authenticated.project_ids == frozenset({"project-1"})
        session.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await current_principal(credentials, db)
        assert exc.value.status_code == 401
        await clear_database(db)


@pytest.mark.asyncio
async def test_sync_idempotency_stale_rejection_and_pull_acknowledgement():
    fixture = json.loads(
        (ROOT / "contracts/backend/v1/sync-push.request.json").read_text(encoding="utf-8")
    )
    batch = SyncBatch.model_validate(fixture)
    actor = principal(
        capabilities={"sync", "customer.edit"}, customer_ids={"customer-001"}, revision=4
    )
    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=4))
        await db.flush()
        db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-001"))
        db.add(SyncEntity(
            organization_id="org-1", entity_type="customer", entity_id="customer-001",
            server_revision=7, schema_version=1,
            payload_json={"id": "customer-001", "name": "Before"},
            updated_at=datetime.now(timezone.utc), deleted_at=None,
        ))
        await db.commit()
        first = await apply_push(db, actor, batch)
        assert first.acceptedRecordIDs == ["record-001"]
        entity = await db.get(SyncEntity, ("org-1", "customer", "customer-001"))
        assert entity is not None and entity.server_revision == 8
        replay = await apply_push(db, actor, batch)
        assert replay.acceptedRecordIDs == ["record-001"]
        change_count = await db.scalar(
            select(func.count()).select_from(SyncChangeLog).where(
                SyncChangeLog.organization_id == "org-1"
            )
        )
        assert change_count == 1
        stale_payload = fixture.copy()
        stale_payload["records"] = [dict(fixture["records"][0])]
        stale_payload["records"][0]["id"] = "record-stale"
        stale_payload["records"][0]["clientMutationID"] = "mutation-stale"
        stale_payload["records"][0]["baseServerRevision"] = 7
        stale = await apply_push(db, actor, SyncBatch.model_validate(stale_payload))
        assert stale.rejectedRecordIDs == ["record-stale"]
        pulled = await pull_since(db, actor, "seq:0")
        assert len(pulled.records) == 1
        assert pulled.records[0].serverRevision == 8
        assert pulled.records[0].clientMutationID == "mutation-001"
        await clear_database(db)


@pytest.mark.asyncio
async def test_out_of_order_create_builds_customer_project_sector_item_graph_and_scope():
    actor = principal(capabilities={
        "sync", "customer.create", "project.create", "project.edit", "item.create",
    })
    batch = SyncBatch.model_validate({
        "deviceID": "device-create",
        "records": [
            record("record-item", "item", "item-new", {
                "id": "item-new", "projectID": "project-new",
                "projectSectorID": "project-sector-new",
            }, "mutation-item"),
            record("record-sector", "project_sector", "project-sector-new", {
                "id": "project-sector-new", "projectID": "project-new",
                "sectorID": "sector-aluminum-glass-steel",
            }, "mutation-sector"),
            record("record-project", "project", "project-new", {
                "id": "project-new", "customerID": "customer-new", "name": "New Project",
            }, "mutation-project"),
            record("record-customer", "customer", "customer-new", {
                "id": "customer-new", "name": "New Customer",
            }, "mutation-customer"),
        ],
    })
    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=10))
        db.add(User(id="user-1", display_name="Creator", email="creator@example.invalid"))
        await db.flush()
        db.add(Membership(
            id="membership-1", organization_id="org-1", user_id="user-1", active=True,
            all_customers=False, all_projects=False, customer_ids=[], project_ids=[],
            roles=[{"id": "creator", "displayName": "Creator"}],
            capabilities=list(actor.capabilities),
        ))
        await db.commit()
        result = await apply_push(db, actor, batch)
        assert result.acceptedRecordIDs == [
            "record-item", "record-sector", "record-project", "record-customer"
        ]
        sector = await db.get(CanonicalProjectSector, ("org-1", "project-sector-new"))
        item = await db.get(CanonicalItem, ("org-1", "item-new"))
        assert sector is not None and sector.project_id == "project-new"
        assert item is not None and item.project_sector_id == "project-sector-new"
        membership = await db.get(Membership, "membership-1")
        organization = await db.get(Organization, "org-1")
        assert membership.customer_ids == ["customer-new"]
        assert membership.project_ids == ["project-new"]
        assert organization.authorization_revision == 12
        refreshed = principal(
            capabilities=actor.capabilities, customer_ids={"customer-new"},
            project_ids={"project-new"}, revision=12,
        )
        pulled = await pull_since(db, refreshed, "seq:0")
        assert {entry.entityID for entry in pulled.records} == {
            "customer-new", "project-new", "project-sector-new", "item-new"
        }
        await clear_database(db)


@pytest.mark.asyncio
async def test_item_project_and_project_sector_reparenting_and_sibling_scope_fail_closed():
    actor = principal(
        capabilities={"sync", "item.edit"}, customer_ids={"customer-1"}, project_ids={"project-1"}
    )
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_project_graph(db, second_project=True)
        db.add_all([
            SyncEntity(
                organization_id="org-1", entity_type="item", entity_id="item-1",
                server_revision=3, schema_version=1,
                payload_json={"id": "item-1", "projectID": "project-1", "projectSectorID": "project-sector-1"},
                updated_at=datetime.now(timezone.utc), deleted_at=None,
            ),
            SyncEntity(
                organization_id="org-1", entity_type="item", entity_id="item-2",
                server_revision=4, schema_version=1,
                payload_json={"id": "item-2", "projectID": "project-2", "projectSectorID": "project-sector-2"},
                updated_at=datetime.now(timezone.utc), deleted_at=None,
            ),
        ])
        await db.commit()
        batch = SyncBatch.model_validate({
            "deviceID": "device-authz",
            "records": [
                record("record-sibling", "item", "item-2", {
                    "id": "item-2", "projectID": "project-2", "projectSectorID": "project-sector-2",
                }, "mutation-sibling", base_revision=4),
                record("record-reparent-project", "item", "item-1", {
                    "id": "item-1", "projectID": "project-2", "projectSectorID": "project-sector-2",
                }, "mutation-reparent-project", base_revision=3),
                record("record-reparent-sector", "item", "item-1", {
                    "id": "item-1", "projectID": "project-1", "projectSectorID": "project-sector-2",
                }, "mutation-reparent-sector", base_revision=3),
                record("record-allowed", "item", "item-1", {
                    "id": "item-1", "projectID": "project-1", "projectSectorID": "project-sector-1",
                }, "mutation-allowed", base_revision=3),
            ],
        })
        result = await apply_push(db, actor, batch)
        assert result.acceptedRecordIDs == ["record-allowed"]
        assert result.rejectedRecordIDs == [
            "record-sibling", "record-reparent-project", "record-reparent-sector"
        ]
        item = await db.get(CanonicalItem, ("org-1", "item-1"))
        assert item.project_id == "project-1" and item.project_sector_id == "project-sector-1"
        await clear_database(db)


@pytest.mark.asyncio
async def test_all_v1_child_types_bind_to_canonical_parents_and_pull_by_scope():
    actor = principal(
        capabilities={
            "sync", "item.edit", "item.blockers.manage", "item.measurements.manage",
            "item.evidence.manage", "item.requirements.manage", "item.configuration.manage",
            "item.configuration.finalize", "quotation.create",
        },
        customer_ids={"customer-1"}, project_ids={"project-1"},
    )
    p = provenance(actor)
    evidence = {"id": "evidence-1", "itemID": "item-1", "title": "Photo"}
    blocker = {
        "id": "blocker-1", "itemID": "item-1", "category": "technical",
        "description": "Check mullion", "status": "open", "critical": True,
        "events": [{
            "id": "blocker-event-1", "blockerID": "blocker-1", "itemID": "item-1",
            "kind": "created", "description": "Check mullion", "critical": True, **p,
        }],
    }
    requirement = {
        "id": "requirement-1", "itemID": "item-1",
        "evidenceReferenceIDs": ["evidence-1"],
    }
    configuration = {
        "id": "configuration-1", "itemID": "item-1", "itemTypeID": "item-type-window",
        "version": 1, "status": "complete",
        "customerRequirements": ["requirement-1"], "blockerIDs": ["blocker-1"],
    }
    audit = {
        "id": "audit-1", "itemID": "item-1", "kind": "updated",
        "before": {"id": "item-1", "projectID": "project-1", "projectSectorID": "project-sector-1"},
        "after": {"id": "item-1", "projectID": "project-1", "projectSectorID": "project-sector-1"},
        "readinessOverrideUsed": False, **p,
    }
    quotation = {
        "id": "quotation-1", "projectID": "project-1", "customerID": "customer-1",
        "status": "draft", "lines": [{"id": "line-1", "itemID": "item-1"}],
    }
    batch = SyncBatch.model_validate({
        "deviceID": "device-children",
        "records": [
            record("r-evidence", "evidence", "evidence-1", evidence, "m-evidence"),
            record("r-measurement", "measurement", "measurement-1", {
                "id": "measurement-1", "itemID": "item-1", "version": 1,
                "evidenceReferences": ["evidence-1"],
            }, "m-measurement"),
            record("r-blocker", "blocker", "blocker-1", blocker, "m-blocker"),
            record("r-requirement", "customer_requirement", "requirement-1", requirement, "m-req"),
            record("r-config", "configuration", "configuration-1", configuration, "m-config"),
            record("r-config-version", "configuration_version", "configuration-1#v1", {
                "id": "configuration-1#v1", "configuration": configuration,
            }, "m-config-version"),
            record("r-audit", "item_audit_event", "audit-1", audit, "m-audit"),
            record("r-quote", "quotation", "quotation-1", quotation, "m-quote"),
        ],
    })
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_project_graph(db)
        result = await apply_push(db, actor, batch)
        assert result.rejectedRecordIDs == []
        assert result.acceptedRecordIDs == [r.id for r in batch.records]
        for entity_type, entity_id in (
            ("evidence", "evidence-1"), ("measurement", "measurement-1"),
            ("blocker", "blocker-1"), ("customer_requirement", "requirement-1"),
            ("configuration", "configuration-1"),
            ("configuration_version", "configuration-1#v1"),
            ("item_audit_event", "audit-1"),
        ):
            child = await db.get(CanonicalItemChild, ("org-1", entity_type, entity_id))
            assert child is not None and child.item_id == "item-1"
        quote = await db.get(CanonicalProjectChild, ("org-1", "quotation", "quotation-1"))
        assert quote is not None and quote.project_id == "project-1"
        pulled = await pull_since(db, actor, "seq:0")
        assert {r.entityType for r in pulled.records} == {
            "evidence", "measurement", "blocker", "customer_requirement",
            "configuration", "configuration_version", "item_audit_event", "quotation",
        }
        await clear_database(db)


@pytest.mark.asyncio
async def test_child_capability_scope_and_reparenting_are_enforced():
    allowed = principal(
        capabilities={"sync", "item.measurements.manage"},
        customer_ids={"customer-1"}, project_ids={"project-1"},
    )
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_project_graph(db, second_project=True)
        created = await apply_push(db, allowed, SyncBatch.model_validate({
            "deviceID": "d1", "records": [record(
                "r-create", "measurement", "measurement-1",
                {"id": "measurement-1", "itemID": "item-1", "evidenceReferences": []},
                "m-create",
            )],
        }))
        assert created.acceptedRecordIDs == ["r-create"]
        entity = await db.get(SyncEntity, ("org-1", "measurement", "measurement-1"))
        denied = await apply_push(db, allowed, SyncBatch.model_validate({
            "deviceID": "d1", "records": [record(
                "r-reparent", "measurement", "measurement-1",
                {"id": "measurement-1", "itemID": "item-2", "evidenceReferences": []},
                "m-reparent", base_revision=entity.server_revision,
            )],
        }))
        assert denied.rejectedRecordIDs == ["r-reparent"]
        sibling_result = await apply_push(db, allowed, SyncBatch.model_validate({
            "deviceID": "d1", "records": [record(
                "r-sibling", "measurement", "measurement-2",
                {"id": "measurement-2", "itemID": "item-2", "evidenceReferences": []},
                "m-sibling",
            )],
        }))
        assert sibling_result.rejectedRecordIDs == ["r-sibling"]
        no_capability = principal(
            capabilities={"sync"}, customer_ids={"customer-1"}, project_ids={"project-1"}
        )
        no_cap = await apply_push(db, no_capability, SyncBatch.model_validate({
            "deviceID": "d2", "records": [record(
                "r-no-cap", "measurement", "measurement-3",
                {"id": "measurement-3", "itemID": "item-1", "evidenceReferences": []},
                "m-no-cap",
            )],
        }))
        assert no_cap.rejectedRecordIDs == ["r-no-cap"]
        await clear_database(db)


@pytest.mark.asyncio
async def test_audit_and_configuration_version_history_are_append_only_and_provenance_bound():
    actor = principal(
        capabilities={"sync", "item.edit", "item.configuration.manage"},
        customer_ids={"customer-1"}, project_ids={"project-1"},
    )
    p = provenance(actor)
    audit = {
        "id": "audit-1", "itemID": "item-1", "kind": "updated",
        "before": {"id": "item-1", "projectID": "project-1", "projectSectorID": "project-sector-1"},
        "after": {"id": "item-1", "projectID": "project-1", "projectSectorID": "project-sector-1"},
        "readinessOverrideUsed": False, **p,
    }
    configuration = {
        "id": "configuration-1", "itemID": "item-1", "itemTypeID": "item-type-window",
        "version": 1, "status": "in_progress", "customerRequirements": [], "blockerIDs": [],
    }
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_project_graph(db)
        base = SyncBatch.model_validate({
            "deviceID": "d-history", "records": [
                record("r-config", "configuration", "configuration-1", configuration, "m-config"),
                record("r-version", "configuration_version", "configuration-1#v1", {
                    "id": "configuration-1#v1", "configuration": configuration,
                }, "m-version"),
                record("r-audit", "item_audit_event", "audit-1", audit, "m-audit"),
            ],
        })
        created = await apply_push(db, actor, base)
        assert created.rejectedRecordIDs == []
        audit_entity = await db.get(SyncEntity, ("org-1", "item_audit_event", "audit-1"))
        version_entity = await db.get(SyncEntity, ("org-1", "configuration_version", "configuration-1#v1"))
        rewritten = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "d-history", "records": [
                record("r-audit-rewrite", "item_audit_event", "audit-1", audit,
                       "m-audit-rewrite", base_revision=audit_entity.server_revision),
                record("r-version-rewrite", "configuration_version", "configuration-1#v1",
                       {"id": "configuration-1#v1", "configuration": configuration},
                       "m-version-rewrite", base_revision=version_entity.server_revision),
            ],
        }))
        assert rewritten.rejectedRecordIDs == ["r-audit-rewrite", "r-version-rewrite"]
        spoofed = dict(audit) | {"id": "audit-2", "sessionID": "forged-session"}
        spoof_result = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "d-history", "records": [record(
                "r-spoof", "item_audit_event", "audit-2", spoofed, "m-spoof"
            )],
        }))
        assert spoof_result.rejectedRecordIDs == ["r-spoof"]
        await clear_database(db)


@pytest.mark.asyncio
async def test_quotation_uses_project_scope_status_capability_and_same_project_lines():
    creator = principal(
        capabilities={"sync", "quotation.create"},
        customer_ids={"customer-1"}, project_ids={"project-1"},
    )
    base_quote = {
        "id": "quotation-1", "projectID": "project-1", "customerID": "customer-1",
        "status": "draft", "lines": [{"id": "line-1", "itemID": "item-1"}],
    }
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_project_graph(db, second_project=True)
        created = await apply_push(db, creator, SyncBatch.model_validate({
            "deviceID": "dq", "records": [record(
                "r-q", "quotation", "quotation-1", base_quote, "m-q"
            )],
        }))
        assert created.acceptedRecordIDs == ["r-q"]
        current = await db.get(SyncEntity, ("org-1", "quotation", "quotation-1"))
        sent_payload = dict(base_quote) | {
            "status": "sent",
            "events": [{"id": "q-event-1", "kind": "sent", **provenance(creator)}],
        }
        denied = await apply_push(db, creator, SyncBatch.model_validate({
            "deviceID": "dq", "records": [record(
                "r-send-denied", "quotation", "quotation-1", sent_payload,
                "m-send-denied", base_revision=current.server_revision,
            )],
        }))
        assert denied.rejectedRecordIDs == ["r-send-denied"]
        wrong_line = dict(base_quote) | {
            "id": "quotation-2",
            "lines": [{"id": "line-2", "itemID": "item-2"}],
        }
        cross_project = await apply_push(db, creator, SyncBatch.model_validate({
            "deviceID": "dq", "records": [record(
                "r-cross", "quotation", "quotation-2", wrong_line, "m-cross"
            )],
        }))
        assert cross_project.rejectedRecordIDs == ["r-cross"]
        sender = principal(
            capabilities={"sync", "quotation.send"},
            customer_ids={"customer-1"}, project_ids={"project-1"},
        )
        sent_payload["events"] = [{"id": "q-event-1", "kind": "sent", **provenance(sender)}]
        sent = await apply_push(db, sender, SyncBatch.model_validate({
            "deviceID": "dq2", "records": [record(
                "r-send", "quotation", "quotation-1", sent_payload,
                "m-send", base_revision=current.server_revision,
            )],
        }))
        assert sent.acceptedRecordIDs == ["r-send"]
        await clear_database(db)


@pytest.mark.asyncio
async def test_delete_invariants_block_referenced_evidence_and_parent_tombstones():
    actor = principal(
        capabilities={
            "sync", "item.edit", "project.edit", "item.evidence.manage",
            "item.requirements.manage", "item.measurements.manage",
        },
        customer_ids={"customer-1"}, project_ids={"project-1"},
    )
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_project_graph(db)
        seeded = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "dd", "records": [
                record("r-e", "evidence", "evidence-1",
                       {"id": "evidence-1", "itemID": "item-1"}, "m-e"),
                record("r-r", "customer_requirement", "requirement-1", {
                    "id": "requirement-1", "itemID": "item-1",
                    "evidenceReferenceIDs": ["evidence-1"],
                }, "m-r"),
            ],
        }))
        assert seeded.rejectedRecordIDs == []
        evidence_entity = await db.get(SyncEntity, ("org-1", "evidence", "evidence-1"))
        evidence_delete = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "dd", "records": [record(
                "r-ed", "evidence", "evidence-1", {}, "m-ed",
                base_revision=evidence_entity.server_revision, deleted_at=NOW,
            )],
        }))
        assert evidence_delete.rejectedRecordIDs == ["r-ed"]
        item_delete = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "dd", "records": [record(
                "r-id", "item", "item-1", {}, "m-id", deleted_at=NOW,
            )],
        }))
        assert item_delete.rejectedRecordIDs == ["r-id"]
        sector_delete = await apply_push(db, actor, SyncBatch.model_validate({
            "deviceID": "dd", "records": [record(
                "r-sd", "project_sector", "project-sector-1", {}, "m-sd", deleted_at=NOW,
            )],
        }))
        assert sector_delete.rejectedRecordIDs == ["r-sd"]
        await clear_database(db)


def test_unknown_entity_type_is_rejected_at_schema_boundary():
    with pytest.raises(Exception) as exc:
        SyncBatch.model_validate({
            "deviceID": "dx",
            "records": [record(
                "r-unknown", "future_child", "future-1",
                {"id": "future-1", "itemID": "item-1"}, "m-unknown",
            )],
        })
    assert "entityType" in str(exc.value)


@pytest.mark.asyncio
async def test_evidence_upload_uses_canonical_item_scope_and_capability(tmp_path, monkeypatch):
    allowed = principal(
        capabilities={"sync", "item.evidence.manage"},
        customer_ids={"customer-1"}, project_ids={"project-1"},
    )
    denied = principal(
        capabilities={"sync", "item.evidence.manage"},
        customer_ids={"customer-1"}, project_ids={"project-2"},
    )
    monkeypatch.setattr(settings, "evidence_root", tmp_path)
    body = b"canonical evidence"
    digest = hashlib.sha256(body).hexdigest()
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_project_graph(db, second_project=True)
        stored = await put_blob(
            db, allowed, "doc-1", "item-1", "photo.jpg", "image/jpeg", digest, body
        )
        assert stored.item_id == "item-1"
        with pytest.raises(HTTPException) as exc:
            await put_blob(
                db, denied, "doc-2", "item-1", "photo.jpg", "image/jpeg", digest, body
            )
        assert exc.value.status_code == 403
        assert await db.get(EvidenceBlob, ("org-1", "doc-2")) is None
        await clear_database(db)

@pytest.mark.asyncio
async def test_pull_resume_cursor_consumes_invisible_rows_without_skipping_oversize_visible_record(monkeypatch):
    actor = principal(
        capabilities={"sync"},
        customer_ids={"customer-visible-a", "customer-visible-b"},
    )
    now = datetime.now(timezone.utc)

    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=1))
        await db.flush()
        db.add_all([
            CanonicalCustomer(organization_id="org-1", customer_id="customer-hidden-x"),
            CanonicalCustomer(organization_id="org-1", customer_id="customer-visible-a"),
            CanonicalCustomer(organization_id="org-1", customer_id="customer-visible-b"),
        ])
        db.add_all([
            SyncEntity(
                organization_id="org-1",
                entity_type="customer",
                entity_id="customer-hidden-x",
                server_revision=1,
                schema_version=1,
                payload_json={"id": "customer-hidden-x", "name": "Hidden XXX"},
                updated_at=now,
                deleted_at=None,
            ),
            SyncEntity(
                organization_id="org-1",
                entity_type="customer",
                entity_id="customer-visible-a",
                server_revision=2,
                schema_version=1,
                payload_json={"id": "customer-visible-a", "name": "Visible A"},
                updated_at=now,
                deleted_at=None,
            ),
            SyncEntity(
                organization_id="org-1",
                entity_type="customer",
                entity_id="customer-visible-b",
                server_revision=3,
                schema_version=1,
                payload_json={"id": "customer-visible-b", "name": "Visible B"},
                updated_at=now,
                deleted_at=None,
            ),
        ])
        await db.flush()

        changes = [
            SyncChangeLog(
                organization_id="org-1",
                entity_type="customer",
                entity_id="customer-hidden-x",
                server_revision=1,
                client_mutation_id="mutation-hidden-x",
                operation="upsert",
            ),
            SyncChangeLog(
                organization_id="org-1",
                entity_type="customer",
                entity_id="customer-visible-a",
                server_revision=2,
                client_mutation_id="mutation-visible-a",
                operation="upsert",
            ),
            SyncChangeLog(
                organization_id="org-1",
                entity_type="customer",
                entity_id="customer-visible-b",
                server_revision=3,
                client_mutation_id="mutation-visible-b",
                operation="upsert",
            ),
        ]
        db.add_all(changes)
        await db.flush()
        hidden_sequence, first_sequence, second_sequence = [
            int(change.sequence) for change in changes
        ]
        assert hidden_sequence < first_sequence < second_sequence
        await db.commit()

        baseline = await pull_since(db, actor, "seq:0")
        assert [entry.entityID for entry in baseline.records] == [
            "customer-visible-a",
            "customer-visible-b",
        ]

        single_page_sizes = [
            len(
                SyncBatch(
                    deviceID="server",
                    cursor=f"seq:{sequence}",
                    records=[entry],
                ).model_dump_json().encode("utf-8")
            )
            for sequence, entry in (
                (first_sequence, baseline.records[0]),
                (second_sequence, baseline.records[1]),
            )
        ]
        response_limit = max(single_page_sizes) + 1
        two_record_size = len(
            SyncBatch(
                deviceID="server",
                cursor=f"seq:{second_sequence}",
                records=baseline.records,
            ).model_dump_json().encode("utf-8")
        )
        assert two_record_size > response_limit
        monkeypatch.setattr(
            "app.sync_service.MAX_SYNC_PULL_RESPONSE_BYTES",
            response_limit,
        )

        first_page = await pull_since(db, actor, "seq:0")
        assert [entry.entityID for entry in first_page.records] == ["customer-visible-a"]
        assert first_page.cursor == f"seq:{first_sequence}"

        second_page = await pull_since(db, actor, first_page.cursor)
        assert [entry.entityID for entry in second_page.records] == ["customer-visible-b"]
        assert second_page.cursor == f"seq:{second_sequence}"

        exhausted = await pull_since(db, actor, second_page.cursor)
        assert exhausted.records == []
        assert exhausted.cursor == f"seq:{second_sequence}"
        await clear_database(db)
