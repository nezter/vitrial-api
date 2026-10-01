"""Whole-request byte limits, enforced before and during parsing.

Why this exists
---------------
The service bounded request *rate* (see `rate_limit.py`) but only bounded request
*size* on the evidence upload path. Every other endpoint would read an arbitrarily
large body into memory before pydantic saw it.

That makes the rate limit close to decorative for availability purposes: 30 requests
per minute is nothing when each may be 500 MB. Two concrete problems:

1. **Memory exhaustion.** A handful of concurrent large bodies is enough.
2. **Parsing cost paid before validation.** The body is read and JSON-parsed before
   anything rejects it, so an oversized request costs full price and then fails.

This is section 4 of the 0.3.0 plan: "enforce whole-request byte limits before
expensive parsing wherever practical".

The interesting part is "wherever practical". A `Content-Length` check alone is
advisory -- the header can be absent, as with chunked transfer encoding, or
dishonest. So the guard does both:

- reject an oversized `Content-Length` *before* the body is read, which is the cheap
  path and costs nothing; and
- wrap the receive channel so the streamed body is cut off at the same ceiling even
  when the header is missing or understates the real size.

## This must be pure ASGI middleware, and that is not incidental

The first attempt registered the guard with `@app.middleware("http")` and mutated
`request._receive` to install the cap. It compiled, the unit tests for the capping
class passed, and **the cap never fired** -- verified by driving the app with a
request that declared 10 bytes and then streamed 50 KB.

Starlette's `@app.middleware("http")` builds a *new* downstream `Request` around its
own receive callable. Mutating the middleware's request therefore has no effect on
the object whose body the endpoint actually reads. The correct interception point is
the ASGI `receive` argument itself, which only pure ASGI middleware can wrap.

This is the third time in this repository that a correct component turned out not to
be the code that ran (`writeProtected` read by nobody; a protocol default shadowing
the real implementation; now this). Hence the explicit end-to-end test below, which
drives the app through a lying `Content-Length`, and the sabotage check that confirms
it fails when the wrapping is removed.

## Limits

Per-path, and derived from what the clients actually send rather than guessed:

* **Sync push: 4 MiB.** The iOS client chunks at `maxPayloadBytes: 1_500_000` raw
  with at most 200 records, so a push body is roughly 2.1 MB on the wire once base64
  and framing are included. 4 MiB is generous headroom over that and still bounds a
  request that would otherwise be unbounded.
* **Pairing: 16 KiB.** The body is a single `pairingCode` capped at 128 characters --
  three orders of magnitude of headroom.
* **Admin: 64 KiB.** Provisioning payloads are small, and the admin surface is
  disabled unless an admin key is configured.

Every limit is overridable per environment variable, because a fixed ceiling is wrong
the moment a client changes.

Unlisted paths are deliberately **not** limited. Refusing a legitimate request on a
path nobody reasoned about is a worse outcome than accepting an oversized one, and a
blanket default would make every future route a false-rejection risk.
"""

from __future__ import annotations

import os

from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MIB = 1024 * 1024

# (path prefix, default limit). Longest matching prefix wins.
DEFAULT_PATH_LIMITS: tuple[tuple[str, int], ...] = (
    ("/api/v1/auth/pair", 16 * 1024),
    ("/api/v1/auth/refresh", 16 * 1024),
    ("/api/v1/sync/push", 4 * MIB),
    ("/api/v1/sync/v2/push", 4 * MIB),
    ("/api/v1/admin", 64 * 1024),
)


class RequestTooLarge(Exception):
    """Raised internally when a streamed body exceeds its limit."""


def _positive_int(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _env_suffix(prefix: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in prefix).strip("_").upper()


def limits() -> tuple[tuple[str, int], ...]:
    """Effective limits, honouring per-prefix environment overrides."""
    resolved = []
    for prefix, default in DEFAULT_PATH_LIMITS:
        override = os.getenv(f"VITRIAL_MAX_BODY_BYTES_{_env_suffix(prefix)}")
        resolved.append((prefix, _positive_int(override, default) if override else default))
    return tuple(resolved)


def limit_for_path(path: str) -> int | None:
    """Longest matching prefix wins; `None` means this path is not size-limited."""
    best: int | None = None
    best_length = -1
    for prefix, limit in limits():
        if path.startswith(prefix) and len(prefix) > best_length:
            best, best_length = limit, len(prefix)
    return best


def declared_length(scope: Scope) -> int | None:
    """Parse `content-length`, treating anything unusable as unknown.

    Unknown is deliberately *not* zero. Coercing a missing or malformed header to 0
    would mark the request compliant and hand the decision entirely to the stream
    check, which is the only check a client controls.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None
    return None


class RequestSizeLimitMiddleware:
    """Pure ASGI middleware enforcing whole-request byte limits.

    Two enforcement points, because one is not enough:

    * An oversized `Content-Length` is refused before a single body byte is read.
    * Otherwise the body is read *here*, counted, and replayed to the downstream app.
      Reading stops one byte past the limit, so an oversized body is never fully
      buffered and a lying or absent `Content-Length` is caught regardless.

    The guard deliberately becomes the sole reader rather than wrapping `receive` and
    raising. Both earlier attempts failed in ways that looked correct:

    - Registering with `@app.middleware("http")` and mutating `request._receive` did
      nothing at all, because Starlette's decorator middleware builds a *new*
      downstream request around its own receive callable.
    - Wrapping `receive` in pure ASGI middleware and raising did intercept the
      stream, but Starlette's body parser catches the exception and turns it into
      `400 There was an error parsing the body`. A 413 became a 400, and the failure
      looked like a malformed request rather than an oversized one.

    Reading and replaying sidesteps both: no exception has to cross a middleware
    boundary, and the decision is made before the endpoint exists. The cost is
    buffering a body that is already within budget, which is bounded by the limit --
    the same bound the guard exists to enforce.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        limit = limit_for_path(path)
        if limit is None:
            await self.app(scope, receive, send)
            return

        if scope.get("method", "GET") == "GET":
            # No body to bound. Avoids reading a request that has none.
            await self.app(scope, receive, send)
            return

        declared = declared_length(scope)
        if declared is not None and declared > limit:
            await self._reject(send, limit)
            return

        buffered, over = await self._read_capped(receive, limit)
        if over:
            await self._reject(send, limit)
            return

        replayed = _ReplayReceive(buffered)
        try:
            await self.app(scope, replayed, send)
        except ClientDisconnect:
            await self._reject(send, limit)

    async def _read_capped(self, receive: Receive, limit: int) -> tuple[bytes, bool]:
        """Buffer the body, stopping one byte past `limit`.

        Returns `(body, over_limit)`. On overrun the buffered prefix is discarded, so
        an attacker cannot make the service hold an unbounded body in order to reject
        it.
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message.get("type") != "http.request":
                continue
            body = message.get("body", b"") or b""
            total += len(body)
            if total > limit:
                return b"", True
            chunks.append(body)
            if not message.get("more_body"):
                break
        return b"".join(chunks), False

    async def _reject(self, send: Send, limit: int) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-vitrial-max-body-bytes", str(limit).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _ReplayReceive:
    """Serves an already-buffered body to the downstream app, then disconnects."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._sent = False

    async def __call__(self) -> Message:
        if self._sent:
            return {"type": "http.disconnect"}
        self._sent = True
        return {"type": "http.request", "body": self._body, "more_body": False}


def install(app: ASGIApp) -> ASGIApp:
    """Wrap `app` with the size guard.

    Placed *outside* the request-correlation middleware in the stack so a rejection
    still receives a request id and a completed-event log line.
    """
    return RequestSizeLimitMiddleware(app)
