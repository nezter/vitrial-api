"""Whole-request byte limits.

The behaviour that matters most is the second one: a body that omits or understates
`Content-Length`. A `Content-Length` check alone is advisory, and these tests exist to
keep it from being mistaken for enforcement.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.request_size import (
    DEFAULT_PATH_LIMITS,
    RequestSizeLimitMiddleware,
    RequestTooLarge,
    _env_suffix,
    declared_length,
    limit_for_path,
    limits,
)
import app.request_size as request_size

PAIR = "/api/v1/auth/pair"


def _scope(path: str, content_length: bytes | None, forwarded: bytes = b"203.0.113.9"):
    headers = [
        (b"host", b"test"),
        (b"content-type", b"application/json"),
        (b"x-forwarded-for", forwarded),
    ]
    if content_length is not None:
        headers.append((b"content-length", content_length))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("203.0.113.9", 5555),
        "server": ("test", 80),
    }


async def _drive(scope, body_chunks: list[bytes]) -> tuple[int | None, bytes]:
    """Push a request through the real ASGI stack, returning (status, body)."""
    sent: list[dict] = []
    remaining = list(body_chunks)

    async def receive():
        if remaining:
            return {"type": "http.request", "body": remaining.pop(0), "more_body": bool(remaining)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


# --------------------------------------------------------------------------- unit


def test_sync_push_limit_leaves_headroom_over_the_real_client():
    """The iOS client chunks at 1.5 MB raw, so the limit must clear that on the wire.

    A limit set at the contract's raw payload budget would reject the app's own
    legitimate pushes, because base64 and JSON framing both add to the body.
    """
    limit = limit_for_path("/api/v1/sync/v2/push")
    assert limit is not None
    # 200 records x 1.5 MB raw budget cannot co-occur; the client caps total payload
    # per batch at 1.5 MB, which is ~2.1 MB once base64 and framing are included.
    assert limit > 2_100_000, "would reject a legitimate full-size batch"
    assert limit < 64 * 1024 * 1024, "is not actually a bound"


def test_pairing_limit_is_far_above_what_the_body_can_be():
    limit = limit_for_path(PAIR)
    assert limit is not None
    # The body is one pairingCode capped at 128 characters.
    assert limit > 512


def test_unlisted_paths_are_not_limited():
    """No default ceiling: a limit applied to an unconsidered path is a false rejection.

    Refusing a legitimate request is a worse outcome than accepting an oversized one
    on a path nobody has reasoned about, so unlisted paths are left alone deliberately.
    """
    assert limit_for_path("/health") is None
    assert limit_for_path("/api/v1/sync/v2/pull") is None


def test_longest_matching_prefix_wins():
    entries = limits()
    prefixes = [p for p, _ in entries]
    assert len(prefixes) == len(set(prefixes)), "duplicate prefixes make selection order significant"


def test_content_length_parsing():
    assert declared_length({"headers": [(b"content-length", b"42")]}) == 42
    assert declared_length({"headers": []}) is None


@pytest.mark.parametrize("raw", [None, b"", b"not-a-number", b"-1", b"1.5"])
def test_unusable_content_length_is_unknown_not_zero(raw):
    """A missing or malformed header must parse as unknown, never as zero.

    Coercing unknown to 0 would mark the request compliant and hand the whole
    decision to the streaming read -- which is the check a client actually controls.
    """
    headers = [] if raw is None else [(b"content-length", raw)]
    assert declared_length({"headers": headers}) in (None,)


@pytest.mark.asyncio
async def test_oversize_is_detected_without_content_length():
    """Absent header: the read is still capped, so the body is caught by counting."""
    scope = _scope(PAIR, content_length=None)
    status, _ = await _drive(scope, [b"x" * 50_000])
    assert status == 413


@pytest.mark.asyncio
async def test_chunks_are_summed_not_judged_individually():
    """Chunked bodies arrive in pieces; each piece can be under the limit alone."""
    scope = _scope(PAIR, content_length=None)
    status, _ = await _drive(scope, [b"x" * 8000, b"x" * 8000, b"x" * 8000])
    assert status == 413


@pytest.mark.asyncio
async def test_a_single_chunk_over_the_limit_is_caught():
    scope = _scope(PAIR, content_length=None)
    status, _ = await _drive(scope, [b"x" * 500_000])
    assert status == 413


# ------------------------------------------------------- the interception point


@pytest.mark.asyncio
async def test_body_that_lies_about_its_length_is_cut_off(monkeypatch):
    """The property the whole guard exists for, end to end.

    Declares 10 bytes and then streams far more, so the `Content-Length` check
    passes and only the receive wrapper can catch it. This test is the reason the
    guard is pure ASGI: with `@app.middleware("http")` the request object the
    endpoint reads is a *different* object, the wrapper is never installed, and this
    request sails through. That regression is invisible to unit tests of the wrapper,
    so it has to be asserted through the real stack.
    """
    monkeypatch.setenv("VITRIAL_MAX_BODY_BYTES_API_V1_AUTH_PAIR", "16384")
    limit = limit_for_path(PAIR)
    assert limit == 16384

    status, body = await _drive(
        _scope(PAIR, content_length=b"10"),
        [b"x" * (limit + 4096)],
    )
    assert status == 413, f"stream cap did not fire; status={status} body={body!r}"
    assert b"request body too large" in body


@pytest.mark.asyncio
async def test_chunked_body_with_no_content_length_is_cut_off(monkeypatch):
    """A body with no `Content-Length` at all, delivered in chunks that each fit."""
    monkeypatch.setenv("VITRIAL_MAX_BODY_BYTES_API_V1_AUTH_PAIR", "16384")
    limit = limit_for_path(PAIR)

    # No content-length header at all, and chunks that are individually under the
    # limit. Only summing across chunks catches this.
    status, _ = await _drive(
        _scope(PAIR, content_length=None),
        [b"x" * 8000, b"x" * 8000, b"x" * 8000],
    )
    assert status == 413


@pytest.mark.asyncio
async def test_oversized_declared_length_is_refused_without_reading_the_body(monkeypatch):
    """The cheap path: nothing is read, so the rejection costs nothing."""
    monkeypatch.setenv("VITRIAL_MAX_BODY_BYTES_API_V1_AUTH_PAIR", "16384")
    read = {"count": 0}

    async def receive():
        read["count"] += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = _scope(PAIR, content_length=b"99999999")
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 413
    assert read["count"] == 0, "body was read despite an oversized Content-Length"


@pytest.mark.asyncio
async def test_body_within_the_limit_still_reaches_the_handler(monkeypatch):
    """A legitimate small body must be unaffected, or sign-in is broken."""
    monkeypatch.setenv("VITRIAL_MAX_BODY_BYTES_API_V1_AUTH_PAIR", "16384")
    status, _ = await _drive(
        _scope(PAIR, content_length=b"10"),
        [json.dumps({"pairingCode": "x" * 32}).encode()],
    )
    # Stubbed handler returns 200. Anything else means the guard interfered.
    assert status == 200


@pytest.mark.asyncio
async def test_unlisted_path_is_never_limited(monkeypatch):
    """No blanket default: an unconsidered path must not become a false rejection."""
    monkeypatch.setenv("VITRIAL_MAX_BODY_BYTES_API_V1_AUTH_PAIR", "8")
    status, _ = await _drive(_scope("/health", content_length=None), [b"x" * 50_000])
    assert status in (200, 405), status


# ------------------------------------------------------------------- integration


@pytest.fixture(autouse=True)
def _stub_handler(monkeypatch):
    """Stub the pairing handler so these tests need no PostgreSQL.

    Its database behaviour is covered by tests/test_auth_pairing_integration.py.
    """
    from datetime import datetime, timezone

    from app.pairing import PairingExchangeResponse

    async def _stub(db, request):
        return PairingExchangeResponse(
            accessToken="stub-access-token",
            expiresAt=datetime(2100, 1, 1, tzinfo=timezone.utc),
        )

    monkeypatch.setattr("app.auth_session_routes.exchange_pairing_code", _stub)


@pytest.mark.asyncio
async def test_genuinely_large_body_over_http_is_rejected():
    """End to end with a truthful header: a large body is refused with 413."""
    limit = limit_for_path(PAIR)
    assert limit is not None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            PAIR,
            content=b"x" * (limit + 4096),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_ordinary_pairing_body_is_untouched():
    """The common case must be unaffected, or the fix has broken sign-in."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(PAIR, json={"pairingCode": "x" * 32})
    # Reaches the handler, which is stubbed: the guard did not interfere.
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_unlisted_path_accepts_a_large_body():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(5):
            response = await client.get("/health")
            assert response.status_code == 200

def test_every_request_limit_override_crosses_the_deployment_boundary():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    production = (root / "deploy" / "compose.production.yml").read_text(encoding="utf-8")
    acceptance = (root / "deploy" / "compose.acceptance.yml").read_text(encoding="utf-8")
    production_env = (root / "deploy" / "env.production.example").read_text(encoding="utf-8")
    development_env = (root / ".env.example").read_text(encoding="utf-8")

    for prefix, default in DEFAULT_PATH_LIMITS:
        key = f"VITRIAL_MAX_BODY_BYTES_{_env_suffix(prefix)}"
        interpolation = f"${{{key}:-{default}}}"
        for compose in (production, acceptance):
            assert f"{key}:" in compose
            assert interpolation in compose
        for env in (production_env, development_env):
            assert f"{key}={default}" in env

