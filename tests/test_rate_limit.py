"""Rate limiting for the unauthenticated auth surface.

These run without PostgreSQL on purpose. The pairing integration tests are skipped
unless `POSTGRES_INTEGRATION=1`, so a security control on the unauthenticated sign-in
path would otherwise have no coverage in the default `pytest` run.
"""

from __future__ import annotations

import os

import pytest
from datetime import datetime, timezone
from httpx import ASGITransport, AsyncClient

from app.pairing import PairingExchangeResponse

from app.main import app
from app.rate_limit import (
    SlidingWindowLimiter,
    client_key_for,
    limiter_for,
    path_is_throttled,
    reset_limiter_for_tests,
)


def _request(client_host: str | None, forwarded: str | None = None):
    class _FakeRequest:
        def __init__(self) -> None:
            self.headers = {}
            self.client = None
            if forwarded is not None:
                self.headers["x-forwarded-for"] = forwarded
            if client_host is not None:
                self.client = type("Peer", (), {"host": client_host})()

    return _FakeRequest()


# --------------------------------------------------------------------------- unit


def test_allows_up_to_the_limit_then_refuses():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60.0)
    assert [limiter.allow("a", now=0.0)[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.allow("a", now=0.0)
    assert allowed is False
    assert retry_after > 0


def test_window_slides_so_a_backoff_client_recovers():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10.0)
    limiter.allow("a", now=0.0)
    limiter.allow("a", now=1.0)
    assert limiter.allow("a", now=2.0)[0] is False
    # Oldest hit leaves the window at t=10.
    assert limiter.allow("a", now=10.0)[0] is True


def test_retry_after_shrinks_as_the_window_drains():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=30.0)
    limiter.allow("a", now=0.0)
    _, first = limiter.allow("a", now=1.0)
    _, later = limiter.allow("a", now=20.0)
    assert first > later > 0


def test_clients_are_independent():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60.0)
    assert limiter.allow("a", now=0.0)[0] is True
    assert limiter.allow("a", now=0.0)[0] is False
    # One noisy client must not throttle everyone else.
    assert limiter.allow("b", now=0.0)[0] is True


def test_limiter_memory_is_bounded_by_capacity():
    limiter = SlidingWindowLimiter(limit=5, window_seconds=60.0, max_clients=4)
    for index in range(50):
        limiter.allow(f"client-{index}", now=0.0)
    assert len(limiter._hits) <= 4


def test_expired_keys_are_dropped_before_live_ones_when_capacity_is_reached():
    """Expiry is only swept when capacity is reached, not on every request.

    Sweeping on every `allow` would be an O(clients) scan per request, which is a
    worse availability trade than retaining a few idle keys. So the guarantee is:
    once the limiter is full, the cheapest keys to forget -- those with no hit inside
    the window -- go first.
    """
    limiter = SlidingWindowLimiter(limit=5, window_seconds=10.0, max_clients=3)

    # One long-idle key, then fill to capacity with live ones.
    limiter.allow("stale", now=0.0)
    limiter.allow("live-a", now=50.0)
    limiter.allow("live-b", now=50.0)
    assert len(limiter._hits) == 3, "capacity not reached yet, so nothing is swept"

    # This arrival is the one that finds the limiter full, so the sweep runs and the
    # expired key is what makes room.
    limiter.allow("live-c", now=50.0)
    assert "stale" not in limiter._hits
    assert {"live-a", "live-b", "live-c"} <= set(limiter._hits)


def test_with_capacity_full_of_live_keys_the_oldest_is_displaced():
    """An active client is never forgotten in favour of a brand-new key.

    Otherwise an attacker cycling source addresses could evict every real client and
    leave the limiter permanently empty of the keys it should be protecting.
    """
    limiter = SlidingWindowLimiter(limit=5, window_seconds=60.0, max_clients=3)
    limiter.allow("oldest", now=0.0)
    limiter.allow("middle", now=10.0)
    limiter.allow("newest", now=20.0)
    limiter.allow("arriving", now=30.0)
    assert "oldest" not in limiter._hits
    assert {"middle", "newest", "arriving"} <= set(limiter._hits)


def test_forwarded_header_is_used_and_peers_are_fallback():
    assert client_key_for(_request(None, forwarded="203.0.113.7")) == "xff:203.0.113.7"
    assert client_key_for(_request(None, forwarded="203.0.113.7, 10.0.0.1")) == "xff:203.0.113.7"
    assert client_key_for(_request("198.51.100.4")) == "peer:198.51.100.4"
    assert client_key_for(_request(None)) == "peer:unknown"


def test_only_the_unauthenticated_auth_surface_is_throttled():
    assert path_is_throttled("/api/v1/auth/pair")
    assert path_is_throttled("/api/v1/auth/refresh")
    # Authenticated traffic is bounded by push serialization, not by client address.
    assert not path_is_throttled("/api/v1/sync/v2/pull")
    assert not path_is_throttled("/health")
    # A prefix match must not be satisfiable by a longer unrelated path.
    assert not path_is_throttled("/api/v1/authentication/thing")


# ------------------------------------------------------------------- integration


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reset_limiter_for_tests()

    # Stub the pairing handler so these tests do not require PostgreSQL, which the
    # pairing integration suite otherwise requires. The limiter is what is under test;
    # the handler's database behaviour is covered by tests/test_auth_pairing_integration.py.
    async def _stub_exchange(db, request):
        return PairingExchangeResponse(
            accessToken="stub-access-token",
            expiresAt=datetime(2100, 1, 1, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(
        "app.auth_session_routes.exchange_pairing_code",
        _stub_exchange,
    )
    yield
    reset_limiter_for_tests()


@pytest.mark.asyncio
async def test_excess_pairing_requests_are_refused_with_429(monkeypatch):
    monkeypatch.setenv("VITRIAL_AUTH_RATE_LIMIT", "5")
    reset_limiter_for_tests()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        statuses = []
        for _ in range(8):
            response = await client.post(
                "/api/v1/auth/pair",
                json={"pairingCode": "x" * 32},
                headers={"x-forwarded-for": "203.0.113.10"},
            )
            statuses.append(response.status_code)

    assert statuses.count(429) == 3, statuses
    # Requests inside the budget reach the handler unchanged, so throttling does not
    # alter normal sign-in behaviour.
    assert all(status in (200, 429) for status in statuses)


@pytest.mark.asyncio
async def test_throttled_response_does_not_distinguish_a_valid_code(monkeypatch):
    """The 429 must leak nothing about whether a code is real.

    This is the property that keeps throttling from becoming a credential oracle. The
    limiter's decision is a pure function of request count, so an attacker learns
    nothing about the code beyond what the 401 already refused to reveal.
    """
    monkeypatch.setenv("VITRIAL_AUTH_RATE_LIMIT", "3")
    reset_limiter_for_tests()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Burn the budget from an unrelated client address.
        for _ in range(3):
            await client.post(
                "/api/v1/auth/pair",
                json={"pairingCode": "x" * 32},
                headers={"x-forwarded-for": "203.0.113.20"},
            )

        # A well-formed code from a fresh address is served normally, proving the
        # limiter is per-client and does not encode any knowledge of the code.
        fresh = await client.post(
            "/api/v1/auth/pair",
            json={"pairingCode": "y" * 32},
            headers={"x-forwarded-for": "203.0.113.21"},
        )
        assert fresh.status_code == 200

        # And the exhausted address is refused identically regardless of what it sends.
        exhausted_valid_looking = await client.post(
            "/api/v1/auth/pair",
            json={"pairingCode": "y" * 32},
            headers={"x-forwarded-for": "203.0.113.20"},
        )
        exhausted_invalid_looking = await client.post(
            "/api/v1/auth/pair",
            json={"pairingCode": "z" * 32},
            headers={"x-forwarded-for": "203.0.113.20"},
        )
        assert exhausted_valid_looking.status_code == 429
        assert exhausted_invalid_looking.status_code == 429
        assert exhausted_valid_looking.json() == exhausted_invalid_looking.json()
        assert exhausted_valid_looking.headers["Retry-After"] >= "1"


@pytest.mark.asyncio
async def test_non_throttled_paths_are_untouched(monkeypatch):
    monkeypatch.setenv("VITRIAL_AUTH_RATE_LIMIT", "2")
    reset_limiter_for_tests()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Health is never throttled, however many times it is called.
        for _ in range(10):
            response = await client.get(
                "/health",
                headers={"x-forwarded-for": "203.0.113.30"},
            )
            assert response.status_code == 200


def test_limiter_is_rebuilt_when_configuration_changes(monkeypatch):
    monkeypatch.setenv("VITRIAL_AUTH_RATE_LIMIT", "7")
    reset_limiter_for_tests()
    assert limiter_for()._limit == 7
    monkeypatch.setenv("VITRIAL_AUTH_RATE_LIMIT", "9")
    assert limiter_for()._limit == 9


def test_invalid_configuration_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("VITRIAL_AUTH_RATE_LIMIT", "not-a-number")
    reset_limiter_for_tests()
    from app.rate_limit import DEFAULT_LIMIT, limit_for

    assert limit_for() == DEFAULT_LIMIT
    monkeypatch.setenv("VITRIAL_AUTH_RATE_LIMIT", "-4")
    reset_limiter_for_tests()
    assert limit_for() == DEFAULT_LIMIT
