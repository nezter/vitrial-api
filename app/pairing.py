from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.auth import token_hash
from app.models import AuthSession, Base, Membership, uid
from app.observability import correlation_ref, log_event
from app.schemas import StrictModel


class PairingGrant(Base):
    """Single-use, short-lived credential used only to mint a device session.

    The plaintext pairing code is returned once to the administrator/operator and is
    never persisted. Only its SHA-256 digest is stored. Consumption is serialized with
    a row lock so the same code cannot mint two sessions under concurrent requests.
    """

    __tablename__ = "pairing_grants"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    membership_id: Mapped[str] = mapped_column(
        ForeignKey("memberships.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminPairingGrantRequest(StrictModel):
    membershipID: str = Field(min_length=1, max_length=128)
    grantTTLSeconds: int = Field(default=15 * 60, ge=60, le=60 * 60)
    sessionTTLSeconds: int = Field(default=8 * 60 * 60, ge=300, le=30 * 24 * 60 * 60)


class AdminPairingGrantResponse(StrictModel):
    pairingGrantID: str
    pairingCode: str
    expiresAt: datetime


class PairingExchangeRequest(StrictModel):
    pairingCode: str = Field(min_length=16, max_length=128)


class PairingExchangeResponse(StrictModel):
    accessToken: str
    expiresAt: datetime


def _normalized_code(value: str) -> str:
    return value.strip()


async def issue_pairing_grant(
    db: AsyncSession,
    request: AdminPairingGrantRequest,
) -> AdminPairingGrantResponse:
    membership = await db.get(Membership, request.membershipID)
    if membership is None:
        raise HTTPException(404, "membership not found")
    if not membership.active:
        raise HTTPException(409, "membership inactive")

    now = datetime.now(timezone.utc)
    # token_urlsafe(24) carries 192 bits before URL-safe base64 encoding. The code is
    # still convenient to paste/scan, but is not a low-entropy human PIN that needs a
    # separate online guessing defense to remain secure.
    pairing_code = secrets.token_urlsafe(24)
    grant = PairingGrant(
        organization_id=membership.organization_id,
        membership_id=membership.id,
        user_id=membership.user_id,
        code_hash=token_hash(pairing_code),
        expires_at=now + timedelta(seconds=request.grantTTLSeconds),
        session_ttl_seconds=request.sessionTTLSeconds,
    )
    db.add(grant)
    await db.commit()

    log_event(
        "auth.pairing_grant_issued",
        organizationRef=correlation_ref(grant.organization_id),
        membershipRef=correlation_ref(grant.membership_id),
        pairingGrantRef=correlation_ref(grant.id),
        expiresAt=grant.expires_at.isoformat(),
    )
    return AdminPairingGrantResponse(
        pairingGrantID=grant.id,
        pairingCode=pairing_code,
        expiresAt=grant.expires_at,
    )


async def exchange_pairing_code(
    db: AsyncSession,
    request: PairingExchangeRequest,
) -> PairingExchangeResponse:
    code = _normalized_code(request.pairingCode)
    # Keep all invalid/replayed/expired outcomes indistinguishable to callers.
    if len(code) < 16:
        raise HTTPException(401, "pairing code invalid or expired")

    now = datetime.now(timezone.utc)
    grant = await db.scalar(
        select(PairingGrant)
        .where(PairingGrant.code_hash == token_hash(code))
        .with_for_update()
    )
    if grant is None or grant.consumed_at is not None or grant.expires_at <= now:
        log_event("auth.pairing_exchange_rejected", reason="invalid_or_expired")
        raise HTTPException(401, "pairing code invalid or expired")

    membership = await db.get(Membership, grant.membership_id)
    if (
        membership is None
        or not membership.active
        or membership.organization_id != grant.organization_id
        or membership.user_id != grant.user_id
    ):
        log_event(
            "auth.pairing_exchange_rejected",
            reason="membership_unavailable",
            pairingGrantRef=correlation_ref(grant.id),
        )
        raise HTTPException(401, "pairing code invalid or expired")

    access_token = secrets.token_urlsafe(48)
    auth_session = AuthSession(
        id=uid(),
        organization_id=grant.organization_id,
        membership_id=grant.membership_id,
        user_id=grant.user_id,
        access_token_hash=token_hash(access_token),
        issued_at=now,
        expires_at=now + timedelta(seconds=grant.session_ttl_seconds),
    )
    grant.consumed_at = now
    db.add(auth_session)
    await db.commit()

    log_event(
        "auth.pairing_exchange_succeeded",
        organizationRef=correlation_ref(grant.organization_id),
        membershipRef=correlation_ref(grant.membership_id),
        pairingGrantRef=correlation_ref(grant.id),
        issuedSessionRef=correlation_ref(auth_session.id),
    )
    return PairingExchangeResponse(
        accessToken=access_token,
        expiresAt=auth_session.expires_at,
    )
