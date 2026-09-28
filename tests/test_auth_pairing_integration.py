import hashlib
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db import SessionFactory
from app.main import app
from app.models import AuthSession, Membership, Organization, User
from app.pairing import PairingGrant
from app.settings import settings

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ORG_ID = "org-pairing-test"
USER_ID = "user-pairing-test"
MEMBERSHIP_ID = "membership-pairing-test"
ADMIN_KEY = "pairing-admin-key-not-a-user-bearer"


def admin_key_hash() -> str:
    return hashlib.sha256(ADMIN_KEY.encode("utf-8")).hexdigest()


async def clear_fixture() -> None:
    async with SessionFactory() as db:
        await db.execute(delete(PairingGrant).where(PairingGrant.organization_id == ORG_ID))
        await db.execute(delete(AuthSession).where(AuthSession.organization_id == ORG_ID))
        await db.execute(delete(Membership).where(Membership.organization_id == ORG_ID))
        await db.execute(delete(User).where(User.id == USER_ID))
        await db.execute(delete(Organization).where(Organization.id == ORG_ID))
        await db.commit()


@pytest.mark.asyncio
async def test_pairing_code_is_single_use_and_logout_revokes_session(monkeypatch):
    await clear_fixture()
    monkeypatch.setattr(settings, "admin_api_key_hash", admin_key_hash())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bootstrap = await client.post(
            "/internal/admin/v1/bootstrap",
            headers={"X-Vitrial-Admin-Key": ADMIN_KEY},
            json={
                "organizationID": ORG_ID,
                "organizationName": "Pairing Integration Org",
                "userID": USER_ID,
                "displayName": "Paired Operator",
                "email": "paired-operator@example.invalid",
                "membershipID": MEMBERSHIP_ID,
                "active": True,
                "allCustomers": True,
                "allProjects": True,
                "customerIDs": [],
                "projectIDs": [],
                "roles": [{"id": "operator", "displayName": "Operator"}],
                "capabilities": ["sync", "item.edit"],
                "sessionID": "session-pairing-bootstrap",
                "sessionTTLSeconds": 3600,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text
        assert bootstrap.headers["Cache-Control"] == "no-store"

        grant_response = await client.post(
            "/internal/admin/v1/pairing-grants",
            headers={"X-Vitrial-Admin-Key": ADMIN_KEY},
            json={
                "membershipID": MEMBERSHIP_ID,
                "grantTTLSeconds": 300,
                "sessionTTLSeconds": 3600,
            },
        )
        assert grant_response.status_code == 200, grant_response.text
        assert grant_response.headers["Cache-Control"] == "no-store"
        pairing_code = grant_response.json()["pairingCode"]
        grant_id = grant_response.json()["pairingGrantID"]
        assert len(pairing_code) >= 16

        async with SessionFactory() as db:
            grant = await db.get(PairingGrant, grant_id)
            assert grant is not None
            assert grant.code_hash == hashlib.sha256(pairing_code.encode("utf-8")).hexdigest()
            assert pairing_code not in grant.code_hash
            assert grant.consumed_at is None

        paired = await client.post(
            "/api/v1/auth/pair",
            json={"pairingCode": pairing_code},
        )
        assert paired.status_code == 200, paired.text
        assert paired.headers["Cache-Control"] == "no-store"
        access_token = paired.json()["accessToken"]
        assert access_token != pairing_code

        profile = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert profile.status_code == 200, profile.text
        assert profile.json()["organizationID"] == ORG_ID
        assert profile.json()["membershipID"] == MEMBERSHIP_ID

        replay = await client.post(
            "/api/v1/auth/pair",
            json={"pairingCode": pairing_code},
        )
        assert replay.status_code == 401
        assert replay.json()["detail"] == "pairing code invalid or expired"

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert logout.status_code == 204, logout.text
        assert logout.headers["Cache-Control"] == "no-store"

        rejected_profile = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert rejected_profile.status_code == 401

    await clear_fixture()
