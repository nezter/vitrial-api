from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import require_admin_key
from app.auth import Principal, current_principal
from app.db import session_scope
from app.models import AuthSession
from app.observability import correlation_ref, log_event
from app.pairing import (
    AdminPairingGrantRequest,
    AdminPairingGrantResponse,
    PairingExchangeRequest,
    PairingExchangeResponse,
    exchange_pairing_code,
    issue_pairing_grant,
)

router = APIRouter()


@router.post(
    "/api/v1/auth/pair",
    response_model=PairingExchangeResponse,
    operation_id="authPair",
)
async def auth_pair(
    request: PairingExchangeRequest,
    response: Response,
    db: AsyncSession = Depends(session_scope),
) -> PairingExchangeResponse:
    response.headers["Cache-Control"] = "no-store"
    return await exchange_pairing_code(db, request)


@router.post(
    "/api/v1/auth/logout",
    status_code=204,
    operation_id="authLogout",
)
async def auth_logout(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    auth_session = await db.get(AuthSession, principal.session_id)
    if auth_session is None:
        raise HTTPException(401, "session unavailable")

    if auth_session.revoked_at is None:
        auth_session.revoked_at = datetime.now(timezone.utc)
        await db.commit()

    log_event(
        "auth.session_self_revoked",
        organizationRef=correlation_ref(principal.organization_id),
        revokedSessionRef=correlation_ref(principal.session_id),
    )
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post(
    "/internal/admin/v1/pairing-grants",
    response_model=AdminPairingGrantResponse,
    include_in_schema=False,
)
async def admin_pairing_grant(
    request: AdminPairingGrantRequest,
    response: Response,
    _: None = Depends(require_admin_key),
    db: AsyncSession = Depends(session_scope),
) -> AdminPairingGrantResponse:
    response.headers["Cache-Control"] = "no-store"
    return await issue_pairing_grant(db, request)
