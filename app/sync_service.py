from __future__ import annotations
import base64
import json
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.delivery_execution import (
    DELIVERY_ENTITY_TYPE,
    DeliveryExecutionRejected,
    apply_delivery_execution_ownership,
    authorize_delivery_execution,
    delivery_execution_is_visible,
)
from app.evidence_gc import queue_blob_gc
from app.idempotency import SyncMutationFingerprint, request_fingerprint
from app.transient_retry import run_with_transient_retry, sqlstate_of
from app.lifecycle import LifecycleRejected, validate_lifecycle_mutation
from app.models import CanonicalItemChild, EvidenceBlob, Organization, SyncChangeLog, SyncEntity, SyncMutation
from app.observability import correlation_ref, log_event
from app.ownership import (
    AuthorizationRejected,
    EffectiveScope,
    apply_ownership_plan,
    authorize_record,
    mutation_sort_key,
    record_is_visible,
)
from app.schemas import (
    MAX_SYNC_RECORDS,
    MAX_SYNC_V1_BATCH_PAYLOAD_BYTES,
    SyncBatch,
    SyncRecord,
    SyncResult,
)

# Upper bound on changes scanned for one pull. Bounds the query; it is not the
# response ceiling (see MAX_SYNC_PULL_RESPONSE_BYTES).
MAX_SYNC_PULL_SCAN_CHANGES = 500
# Serialized ceiling for one pull response, checked against the batch as it
# would actually be encoded rather than estimated from payload bytes.
MAX_SYNC_PULL_RESPONSE_BYTES = 2_250_000


class InvalidMutation(Exception):
    pass


def pull_page_accepts(records: list[SyncRecord], record: SyncRecord, sequence: int) -> bool:
    """Return whether one more visible record fits the public pull response contract.

    Checked against the batch as it would actually be serialized, because
    record-count and payload-byte ceilings alone do not bound the encoded
    response once base64 and per-record framing are included.
    """
    if len(records) >= MAX_SYNC_RECORDS:
        return False
    if sum(len(item.payload) for item in records) + len(record.payload) > MAX_SYNC_V1_BATCH_PAYLOAD_BYTES:
        return False
    candidate = SyncBatch(
        deviceID="server",
        cursor=f"seq:{sequence}",
        records=[*records, record],
    )
    return len(candidate.model_dump_json().encode("utf-8")) <= MAX_SYNC_PULL_RESPONSE_BYTES


def decode_payload(record: SyncRecord) -> dict:
    try:
        raw = record.payload
        try:
            decoded = base64.b64decode(raw, validate=True)
            value = json.loads(decoded)
        except Exception:
            value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("typed payload must decode to a JSON object")
        return value
    except Exception as exc:
        raise InvalidMutation(f"invalid payload: {exc}") from exc


async def _next_revision(db: AsyncSession, organization_id: str) -> int:
    value = await db.scalar(
        select(func.coalesce(func.max(SyncEntity.server_revision), 0)).where(
            SyncEntity.organization_id == organization_id
        )
    )
    return int(value or 0) + 1


async def _item_has_immutable_audit_history(
    db: AsyncSession,
    organization_id: str,
    item_id: str,
) -> bool:
    audit_id = await db.scalar(
        select(CanonicalItemChild.entity_id).where(
            CanonicalItemChild.organization_id == organization_id,
            CanonicalItemChild.entity_type == "item_audit_event",
            CanonicalItemChild.item_id == item_id,
            CanonicalItemChild.deleted_at.is_(None),
        ).limit(1)
    )
    return audit_id is not None


def _mutation_row(
    principal: Principal,
    batch: SyncBatch,
    record: SyncRecord,
    *,
    status: str,
    result_revision: int | None,
) -> SyncMutation:
    return SyncMutation(
        organization_id=principal.organization_id,
        client_mutation_id=record.clientMutationID,
        submitted_by_user_id=principal.user_id,
        membership_id=principal.membership_id,
        session_id=principal.session_id,
        authorization_revision=principal.authorization_revision,
        device_id=batch.deviceID,
        entity_type=record.entityType,
        entity_id=record.entityID,
        base_server_revision=record.baseServerRevision,
        result_server_revision=result_revision,
        result_status=status,
    )


def _record_mutation(
    db: AsyncSession,
    principal: Principal,
    batch: SyncBatch,
    record: SyncRecord,
    *,
    status: str,
    result_revision: int | None,
) -> None:
    db.add(_mutation_row(
        principal,
        batch,
        record,
        status=status,
        result_revision=result_revision,
    ))
    db.add(SyncMutationFingerprint(
        organization_id=principal.organization_id,
        client_mutation_id=record.clientMutationID,
        request_fingerprint=request_fingerprint(record),
    ))


def _outcome_event(
    batch: SyncBatch,
    record: SyncRecord,
    *,
    status: str,
    reason: str,
    result_revision: int | None = None,
) -> dict:
    return {
        "mutationRef": correlation_ref(record.clientMutationID),
        "deviceRef": correlation_ref(batch.deviceID),
        "entityRef": correlation_ref(record.entityID),
        "entityType": record.entityType,
        "resultStatus": status,
        "reason": reason,
        "baseServerRevision": record.baseServerRevision,
        "resultServerRevision": result_revision,
    }


async def apply_push(db: AsyncSession, principal: Principal, batch: SyncBatch) -> SyncResult:
    """Apply a push batch, replaying it if PostgreSQL aborts the transaction.

    PostgreSQL can abort a transaction with `40001 serialization_failure` or
    `40P01 deadlock_detected` under concurrency, and both mean "replay me". Today
    that reaches the client as an unhandled 500 for a condition that resolves on the
    next attempt.

    Replay is safe because this operation is idempotent by construction:
    `SyncMutation` has a `UniqueConstraint(organization_id, client_mutation_id)`, and
    a replayed mutation is answered with the original result
    (`reason="idempotent_replay"`) rather than applied twice. All per-attempt state
    below is local to `_apply_push_once`, so a retry re-does the batch cleanly rather
    than resuming a half-applied one.

    Retries are logged at warning level with the SQLSTATE. A burst of these is the
    signal that organization-level write serialization is under contention, which is
    a capacity concern the plan tracks separately -- not something to hide behind a
    silent retry.
    """
    return await run_with_transient_retry(
        db,
        lambda: _apply_push_once(db, principal, batch),
        on_retry=lambda exc, attempt: log_event(
            "sync.push_replayed",
            level=logging.WARNING,
            sqlstate=sqlstate_of(exc),
            attempt=attempt,
            organizationRef=correlation_ref(principal.organization_id),
        ),
    )


async def _apply_push_once(db: AsyncSession, principal: Principal, batch: SyncBatch) -> SyncResult:
    if "sync" not in principal.capabilities:
        raise PermissionError("sync capability required")

    organization = await db.scalar(
        select(Organization).where(Organization.id == principal.organization_id).with_for_update()
    )
    if organization is None:
        raise PermissionError("organization unavailable")

    scope = EffectiveScope.from_principal(principal)
    outcomes: dict[int, bool] = {}
    outcome_events: list[dict] = []
    indexed_records = list(enumerate(batch.records))
    indexed_records.sort(key=lambda pair: mutation_sort_key(pair[1].entityType, pair[0]))

    for original_index, record in indexed_records:
        if not record.clientMutationID:
            outcomes[original_index] = False
            outcome_events.append(_outcome_event(
                batch, record, status="rejected", reason="missing_client_mutation_id"
            ))
            continue

        prior = await db.scalar(
            select(SyncMutation).where(
                SyncMutation.organization_id == principal.organization_id,
                SyncMutation.client_mutation_id == record.clientMutationID,
            )
        )
        if prior is not None:
            prior_fingerprint = await db.get(
                SyncMutationFingerprint,
                (principal.organization_id, record.clientMutationID),
            )
            if prior_fingerprint is not None:
                same_request = prior_fingerprint.request_fingerprint == request_fingerprint(record)
            else:
                same_request = (
                    prior.entity_type == record.entityType
                    and prior.entity_id == record.entityID
                    and prior.base_server_revision == record.baseServerRevision
                )
            accepted_replay = same_request and prior.result_status == "accepted"
            outcomes[original_index] = accepted_replay
            outcome_events.append(_outcome_event(
                batch,
                record,
                status="accepted" if accepted_replay else "rejected",
                reason="idempotent_replay" if same_request else "mutation_id_collision",
                result_revision=prior.result_server_revision,
            ))
            continue

        current = await db.get(
            SyncEntity,
            (principal.organization_id, record.entityType, record.entityID),
        )
        current_revision = current.server_revision if current else None

        if (
            (current is not None and record.baseServerRevision != current_revision)
            or (current is None and record.baseServerRevision is not None)
        ):
            _record_mutation(
                db, principal, batch, record,
                status="rejected", result_revision=current_revision,
            )
            outcomes[original_index] = False
            outcome_events.append(_outcome_event(
                batch,
                record,
                status="rejected",
                reason="stale_revision",
                result_revision=current_revision,
            ))
            continue

        try:
            payload = decode_payload(record)
            if (
                record.entityType == "item"
                and record.deletedAt is not None
                and await _item_has_immutable_audit_history(
                    db, principal.organization_id, record.entityID
                )
            ):
                raise AuthorizationRejected(
                    "Item cannot be deleted while immutable audit history remains"
                )
            # delivery_execution has a dedicated, stricter lifecycle/authority
            # validator in authorize_delivery_execution. Running the older generic
            # delivery validator as a second authority gate let the two copies drift.
            if record.entityType != DELIVERY_ENTITY_TYPE:
                await validate_lifecycle_mutation(
                    db,
                    principal,
                    entity_type=record.entityType,
                    entity_id=record.entityID,
                    payload=payload,
                    deleted_at=record.deletedAt,
                    current=current,
                )
            if record.entityType == DELIVERY_ENTITY_TYPE:
                project_id = await authorize_delivery_execution(
                    db,
                    principal,
                    scope,
                    entity_id=record.entityID,
                    payload=payload,
                    deleted_at=record.deletedAt,
                    current=current,
                )
                await apply_delivery_execution_ownership(
                    db,
                    principal,
                    entity_id=record.entityID,
                    project_id=project_id,
                )
            else:
                plan = await authorize_record(
                    db,
                    principal,
                    scope,
                    entity_type=record.entityType,
                    entity_id=record.entityID,
                    payload=payload,
                    deleted_at=record.deletedAt,
                    generic_entity_exists=current is not None,
                )
                await apply_ownership_plan(db, principal, scope, plan)
            if record.entityType == "evidence" and record.deletedAt is not None:
                blob = await db.get(
                    EvidenceBlob,
                    (principal.organization_id, record.entityID),
                )
                if blob is not None:
                    # Queue physical deletion in the same transaction as the canonical metadata
                    # tombstone. The collector rechecks references before touching object storage.
                    await queue_blob_gc(db, blob, reason="metadata_tombstone")
        except (
            InvalidMutation,
            AuthorizationRejected,
            LifecycleRejected,
            DeliveryExecutionRejected,
        ) as exc:
            _record_mutation(
                db, principal, batch, record,
                status="rejected", result_revision=current_revision,
            )
            outcomes[original_index] = False
            outcome_events.append(_outcome_event(
                batch,
                record,
                status="rejected",
                reason=type(exc).__name__,
                result_revision=current_revision,
            ))
            continue

        revision = await _next_revision(db, principal.organization_id)

        if current is None:
            current = SyncEntity(
                organization_id=principal.organization_id,
                entity_type=record.entityType,
                entity_id=record.entityID,
                server_revision=revision,
                payload_json=payload if record.deletedAt is None else None,
                updated_at=record.updatedAt,
                deleted_at=record.deletedAt,
            )
            db.add(current)
        else:
            current.server_revision = revision
            current.payload_json = payload if record.deletedAt is None else None
            current.updated_at = record.updatedAt
            current.deleted_at = record.deletedAt

        _record_mutation(
            db, principal, batch, record,
            status="accepted", result_revision=revision,
        )
        db.add(SyncChangeLog(
            organization_id=principal.organization_id,
            entity_type=record.entityType,
            entity_id=record.entityID,
            server_revision=revision,
            client_mutation_id=record.clientMutationID,
            operation="delete" if record.deletedAt else "upsert",
        ))
        outcomes[original_index] = True
        outcome_events.append(_outcome_event(
            batch,
            record,
            status="accepted",
            reason="committed",
            result_revision=revision,
        ))
        await db.flush()

    await db.commit()

    for event in outcome_events:
        log_event("sync.mutation_result", **event)

    seq = await db.scalar(
        select(func.coalesce(func.max(SyncChangeLog.sequence), 0)).where(
            SyncChangeLog.organization_id == principal.organization_id
        )
    )
    result = SyncResult(
        acceptedRecordIDs=[record.id for index, record in enumerate(batch.records) if outcomes.get(index) is True],
        rejectedRecordIDs=[record.id for index, record in enumerate(batch.records) if outcomes.get(index) is not True],
        nextCursor=f"seq:{int(seq or 0)}",
    )
    log_event(
        "sync.push_completed",
        deviceRef=correlation_ref(batch.deviceID),
        recordCount=len(batch.records),
        acceptedCount=len(result.acceptedRecordIDs),
        rejectedCount=len(result.rejectedRecordIDs),
        nextSequence=int(seq or 0),
    )
    return result


async def pull_since(db: AsyncSession, principal: Principal, cursor: str | None) -> SyncBatch:
    if "sync" not in principal.capabilities:
        raise PermissionError("sync capability required")

    start = 0
    if cursor:
        if not cursor.startswith("seq:") or not cursor[4:].isdigit():
            raise InvalidMutation("invalid cursor")
        start = int(cursor[4:])

    changes = (
        await db.scalars(
            select(SyncChangeLog)
            .where(
                SyncChangeLog.organization_id == principal.organization_id,
                SyncChangeLog.sequence > start,
            )
            .order_by(SyncChangeLog.sequence.asc())
            .limit(MAX_SYNC_PULL_SCAN_CHANGES)
        )
    ).all()

    records: list[SyncRecord] = []
    # The cursor advances only past changes actually consumed. An invisible
    # change, or one whose entity is gone, IS consumed: it can never be
    # delivered, so leaving it unconsumed would stall the cursor forever. A
    # change that IS deliverable but does not fit the page is NOT consumed, so
    # the next pull observes it again rather than skipping it.
    max_seq = start
    for change in changes:
        if change.entity_type == DELIVERY_ENTITY_TYPE:
            visible = await delivery_execution_is_visible(
                db,
                principal,
                entity_id=change.entity_id,
            )
        else:
            visible = await record_is_visible(
                db,
                principal,
                entity_type=change.entity_type,
                entity_id=change.entity_id,
            )
        if not visible:
            max_seq = max(max_seq, change.sequence)
            continue
        entity = await db.get(
            SyncEntity,
            (principal.organization_id, change.entity_type, change.entity_id),
        )
        if not entity:
            max_seq = max(max_seq, change.sequence)
            continue
        payload = json.dumps(entity.payload_json or {}, separators=(",", ":"), sort_keys=True).encode()
        encoded_payload = base64.b64encode(payload)
        record = SyncRecord(
            id=f"server:{change.sequence}",
            entityType=change.entity_type,
            entityID=change.entity_id,
            updatedAt=entity.updated_at,
            payload=encoded_payload,
            serverRevision=entity.server_revision,
            clientMutationID=change.client_mutation_id,
            deletedAt=entity.deleted_at,
        )

        # Do not advance the cursor past a visible record that cannot fit this
        # page. The next request must observe that same change rather than
        # silently skip it.
        if not pull_page_accepts(records, record, change.sequence):
            break

        records.append(record)
        max_seq = max(max_seq, change.sequence)

    result = SyncBatch(deviceID="server", cursor=f"seq:{max_seq}", records=records)
    log_event(
        "sync.pull_completed",
        startSequence=start,
        endSequence=max_seq,
        scannedChangeCount=len(changes),
        visibleRecordCount=len(records),
    )
    return result
