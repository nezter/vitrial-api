from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.evidence_gc import queue_blob_gc
from app.models import CanonicalItemChild, EvidenceBlob, Organization
from app.ownership import AuthorizationRejected, require_item_access
from app.settings import settings
from app.storage import (
    StorageDigestMismatch,
    StorageError,
    current_store,
    object_key_for,
)


class EvidenceBlobTooLarge(Exception):
    pass


async def bytes_chunks(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def bounded_chunks(
    chunks: AsyncIterable[bytes],
    max_bytes: int,
) -> AsyncIterator[bytes]:
    limit = max(1, max_bytes)
    consumed = 0
    async for chunk in chunks:
        if not chunk:
            continue
        consumed += len(chunk)
        if consumed > limit:
            raise EvidenceBlobTooLarge(f"evidence blob exceeds {limit} bytes")
        yield chunk


async def _evidence_metadata(
    db: AsyncSession,
    organization_id: str,
    document_id: str,
    *,
    refresh: bool = False,
) -> CanonicalItemChild | None:
    statement = select(CanonicalItemChild).where(
        CanonicalItemChild.organization_id == organization_id,
        CanonicalItemChild.entity_type == "evidence",
        CanonicalItemChild.entity_id == document_id,
    )
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    return await db.scalar(statement)


async def _discard_candidate(store, key: str, *, preserve: bool) -> None:
    if preserve:
        return
    try:
        await store.delete(key)
    except StorageError:
        pass


async def put_blob(
    db: AsyncSession,
    principal: Principal,
    document_id: str,
    item_id: str,
    filename: str,
    mime_type: str,
    expected_sha256: str,
    chunks: AsyncIterable[bytes] | bytes,
) -> EvidenceBlob:
    initial = await db.get(EvidenceBlob, (principal.organization_id, document_id))
    if initial is not None and initial.item_id != item_id:
        raise HTTPException(409, "evidence ownership mismatch")
    metadata = await _evidence_metadata(db, principal.organization_id, document_id)
    if metadata is not None and metadata.deleted_at is not None:
        raise HTTPException(409, "canonical evidence metadata is deleted")

    canonical_item_id = initial.item_id if initial is not None else item_id
    try:
        await require_item_access(
            db,
            principal,
            canonical_item_id,
            capability="item.evidence.manage",
        )
    except AuthorizationRejected as exc:
        raise HTTPException(403, str(exc)) from exc

    # Do not pin a database transaction/connection for the duration of a potentially large stream.
    # Authority and canonical state are re-checked under the organization lock before publication.
    await db.rollback()
    if isinstance(chunks, bytes):
        chunks = bytes_chunks(chunks)
    chunks = bounded_chunks(chunks, settings.evidence_max_bytes)

    store = current_store()
    new_key = object_key_for(principal.organization_id, document_id, expected_sha256)
    try:
        stored = await store.put_verified(new_key, chunks, expected_sha256)
    except EvidenceBlobTooLarge as exc:
        raise HTTPException(413, "evidence blob exceeds configured upload limit") from exc
    except StorageDigestMismatch as exc:
        raise HTTPException(409, "content digest mismatch") from exc
    except StorageError as exc:
        # S3ObjectStore deliberately wraps arbitrary streaming failures after aborting
        # multipart state. Preserve the size-limit semantic through that cleanup layer.
        if isinstance(exc.__cause__, EvidenceBlobTooLarge):
            raise HTTPException(413, "evidence blob exceeds configured upload limit") from exc
        raise HTTPException(503, "evidence object storage unavailable") from exc

    # Serialize only the canonical pointer switch with sync/tombstones. Concurrent uploads may
    # stream in parallel, but every published version sees the previous committed version and
    # queues it for GC instead of leaking an orphan.
    organization = await db.scalar(
        select(Organization).where(Organization.id == principal.organization_id).with_for_update()
    )
    if organization is None:
        await db.rollback()
        await _discard_candidate(store, new_key, preserve=False)
        raise HTTPException(403, "organization unavailable")

    model = await db.scalar(
        select(EvidenceBlob)
        .where(
            EvidenceBlob.organization_id == principal.organization_id,
            EvidenceBlob.document_id == document_id,
        )
        .execution_options(populate_existing=True)
    )
    metadata = await _evidence_metadata(
        db,
        principal.organization_id,
        document_id,
        refresh=True,
    )
    candidate_is_canonical = (
        model is not None
        and model.storage_provider == store.provider
        and model.object_key == new_key
    )

    if metadata is not None and metadata.deleted_at is not None:
        await db.rollback()
        await _discard_candidate(store, new_key, preserve=candidate_is_canonical)
        raise HTTPException(409, "canonical evidence metadata is deleted")
    if model is not None and model.item_id != item_id:
        await db.rollback()
        await _discard_candidate(store, new_key, preserve=candidate_is_canonical)
        raise HTTPException(409, "evidence ownership mismatch")

    canonical_item_id = model.item_id if model is not None else item_id
    try:
        await require_item_access(
            db,
            principal,
            canonical_item_id,
            capability="item.evidence.manage",
        )
    except AuthorizationRejected as exc:
        await db.rollback()
        await _discard_candidate(store, new_key, preserve=candidate_is_canonical)
        raise HTTPException(403, str(exc)) from exc

    created = model is None
    old_provider = model.storage_provider if model is not None else None
    old_key = model.object_key if model is not None else None
    if model is None:
        model = EvidenceBlob(
            organization_id=principal.organization_id,
            document_id=document_id,
            item_id=canonical_item_id,
            filename=filename,
            mime_type=mime_type,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            storage_provider=store.provider,
            object_key=new_key,
        )
        db.add(model)
    else:
        if old_provider != store.provider or old_key != new_key:
            await queue_blob_gc(db, model, reason="replaced")
        model.filename = filename
        model.mime_type = mime_type
        model.sha256 = stored.sha256
        model.size_bytes = stored.size_bytes
        model.storage_provider = store.provider
        model.object_key = new_key

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await _discard_candidate(
            store,
            new_key,
            preserve=(not created and old_provider == store.provider and old_key == new_key),
        )
        raise
    return model
