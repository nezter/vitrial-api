from __future__ import annotations
import logging
import time
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import (
    AdminBootstrapRequest,
    AdminBootstrapResponse,
    AdminSessionRevokeRequest,
    AdminSessionRevokeResponse,
    bootstrap_identity,
    require_admin_key,
    revoke_session,
)
from app.auth import Principal, current_principal, profile_for
from app.auth_session_routes import router as auth_session_router
from app.db import session_scope
from app.evidence import put_blob
from app.models import EvidenceBlob
from app.observability import (
    begin_request,
    bind_request_metadata,
    end_request,
    log_event,
    request_id_for_header,
)
from app.ownership import AuthorizationRejected, require_item_access
from app.readiness import collect_dependency_readiness
from app.reference_routes import router as reference_router
from app.schemas import AuthorizationProfile, HealthResponse, ReadinessResponse, SyncBatch, SyncResult, VersionResponse
from app.settings import settings
from app.storage import StorageError, store_for_provider
from app.sync_service import InvalidMutation, apply_push, pull_since
from app.sync_v2_routes import router as sync_v2_router

app = FastAPI(title="Vitrial Connected Operations API", version=settings.service_version)
app.include_router(auth_session_router)
app.include_router(reference_router)
app.include_router(sync_v2_router)

DocumentID = Annotated[str, Path(min_length=1)]


def _route_template(request: Request) -> str | None:
    route = request.scope.get("route")
    return getattr(route, "path", None)


@app.middleware("http")
async def request_correlation(request: Request, call_next):
    request_id = request_id_for_header(request.headers.get("x-request-id"))
    tokens = begin_request(request_id)
    bind_request_metadata(
        device_id=request.headers.get("x-vitrial-device-id"),
        app_version=request.headers.get("x-vitrial-app-version"),
        app_build=request.headers.get("x-vitrial-app-build"),
    )
    started = time.perf_counter()
    # Do not log the unresolved raw URL here: resource IDs appear in evidence/admin paths.
    # Device identity is stored only as a one-way correlation reference by observability.py.
    log_event("request.started", method=request.method)
    try:
        response = await call_next(request)
    except Exception as exc:
        log_event(
            "request.failed",
            level=logging.ERROR,
            method=request.method,
            routeTemplate=_route_template(request),
            durationMs=round((time.perf_counter() - started) * 1000, 2),
            errorType=type(exc).__name__,
        )
        raise
    else:
        response.headers["X-Request-ID"] = request_id
        log_event(
            "request.completed",
            method=request.method,
            routeTemplate=_route_template(request),
            statusCode=response.status_code,
            durationMs=round((time.perf_counter() - started) * 1000, 2),
        )
        return response
    finally:
        end_request(tokens)


@app.get("/health", response_model=HealthResponse, operation_id="health")
async def health() -> HealthResponse:
    return HealthResponse()


@app.get("/ready", response_model=ReadinessResponse, include_in_schema=False)
async def ready(response: Response) -> ReadinessResponse:
    readiness = await collect_dependency_readiness()
    if readiness.status != "ready":
        response.status_code = 503
    return ReadinessResponse(
        status=readiness.status,
        serviceVersion=settings.service_version,
        database=readiness.database,
        objectStorage=readiness.object_storage,
    )


@app.get("/api/v1/version", response_model=VersionResponse, operation_id="version")
async def version() -> VersionResponse:
    return VersionResponse(serviceVersion=settings.service_version)


@app.get("/api/v1/auth/me", response_model=AuthorizationProfile, operation_id="authMe")
async def auth_me(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> AuthorizationProfile:
    return await profile_for(principal, db)


@app.post("/api/v1/sync/push", response_model=SyncResult, operation_id="syncPush")
async def sync_push(
    batch: SyncBatch,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> SyncResult:
    try:
        return await apply_push(db, principal, batch)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except InvalidMutation as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/sync/pull", response_model=SyncBatch, operation_id="syncPull")
async def sync_pull(
    cursor: str | None = None,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> SyncBatch:
    try:
        return await pull_since(db, principal, cursor)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except InvalidMutation as exc:
        raise HTTPException(400, str(exc)) from exc


async def _authorized_blob(
    db: AsyncSession,
    principal: Principal,
    document_id: str,
) -> EvidenceBlob:
    model = await db.get(EvidenceBlob, (principal.organization_id, document_id))
    if model is None:
        raise HTTPException(404)
    try:
        await require_item_access(db, principal, model.item_id)
    except AuthorizationRejected as exc:
        raise HTTPException(403, str(exc)) from exc
    return model


@app.head(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidencePresence",
)
async def evidence_head(
    documentID: DocumentID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    model = await _authorized_blob(db, principal, documentID)
    try:
        present = await store_for_provider(model.storage_provider).exists(model.object_key)
    except StorageError as exc:
        raise HTTPException(503, "evidence object storage unavailable") from exc
    if not present:
        raise HTTPException(404)
    return Response(status_code=200, headers={"X-Content-SHA256": model.sha256})


@app.put(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidenceUpload",
    status_code=201,
    openapi_extra={
        # The body is a raw byte stream (request.stream()), not a base64
        # string. Declaring `contentEncoding: binary` would tell a code
        # generator to base64-encode the payload before sending, and the server
        # would then store that base64 text as if it were the file. An empty
        # schema models the raw octet-stream body directly, which is also what
        # the client's contract validator requires.
        "requestBody": {
            "required": True,
            "content": {"application/octet-stream": {}},
        }
    },
)
async def evidence_put(
    documentID: DocumentID,
    request: Request,
    x_vitrial_item_id: Annotated[str, Header(alias="X-Vitrial-Item-ID", min_length=1)],
    x_vitrial_filename: Annotated[str, Header(alias="X-Vitrial-Filename", min_length=1)],
    x_content_sha256: Annotated[
        str,
        Header(alias="X-Content-SHA256", pattern=r"^[0-9a-fA-F]{64}$"),
    ],
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > settings.evidence_max_bytes:
        raise HTTPException(413, "evidence blob exceeds configured upload limit")

    model = await put_blob(
        db,
        principal,
        documentID,
        x_vitrial_item_id,
        x_vitrial_filename,
        request.headers.get("content-type", "application/octet-stream"),
        x_content_sha256,
        request.stream(),
    )
    return Response(status_code=201, headers={"X-Content-SHA256": model.sha256})


@app.get(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidenceDownload",
    response_class=StreamingResponse,
    openapi_extra={
        # Without response_class above, FastAPI documents the 200 as
        # application/json — it defaults to JSONResponse and the handler
        # declares no response_model — and merging would leave that bogus entry
        # beside the real one, so a generated client could parse a blob as JSON.
        # The handler streams the stored bytes back under the blob's own media
        # type, so an empty octet-stream schema is the honest description.
        "responses": {
            "200": {
                "description": "The stored evidence blob as a raw byte stream.",
                "content": {"application/octet-stream": {}},
            }
        },
    },
)
async def evidence_get(
    documentID: DocumentID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
):
    model = await _authorized_blob(db, principal, documentID)
    store = store_for_provider(model.storage_provider)
    try:
        if not await store.exists(model.object_key):
            raise HTTPException(404)
    except StorageError as exc:
        raise HTTPException(503, "evidence object storage unavailable") from exc
    return StreamingResponse(
        store.stream(model.object_key),
        media_type=model.mime_type or "application/octet-stream",
        headers={"X-Content-SHA256": model.sha256},
    )


# Internal operational control plane. These routes are deliberately excluded
# from the public/pinned V1 OpenAPI contract and require a separate admin key.
@app.post(
    "/internal/admin/v1/bootstrap",
    response_model=AdminBootstrapResponse,
    include_in_schema=False,
)
async def admin_bootstrap(
    request: AdminBootstrapRequest,
    response: Response,
    _: None = Depends(require_admin_key),
    db: AsyncSession = Depends(session_scope),
) -> AdminBootstrapResponse:
    response.headers["Cache-Control"] = "no-store"
    return await bootstrap_identity(db, request)


@app.post(
    "/internal/admin/v1/sessions/{sessionID}/revoke",
    response_model=AdminSessionRevokeResponse,
    include_in_schema=False,
)
async def admin_revoke_session(
    sessionID: Annotated[str, Path(min_length=1, max_length=128)],
    request: AdminSessionRevokeRequest,
    _: None = Depends(require_admin_key),
    db: AsyncSession = Depends(session_scope),
) -> AdminSessionRevokeResponse:
    return await revoke_session(db, sessionID, request)
