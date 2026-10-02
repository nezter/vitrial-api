"""Bounded request rate for the unauthenticated auth surface.

Why this exists
---------------
`POST /api/v1/auth/pair` is reachable without credentials. It is not brute-forceable
-- pairing codes carry 192 bits of entropy from `secrets.token_urlsafe(24)`, and every
rejection is already indistinguishable from a success -- but it was previously
*unthrottled*, which leaves two production problems:

1. **Resource exhaustion.** Every attempt performs an indexed database lookup, and an
   unlimited request rate against a public endpoint is a denial-of-service lever that
   needs no valid credential to pull.
2. **Log amplification.** Each rejected exchange emits
   `auth.pairing_exchange_rejected`. Without a ceiling, rejected traffic is itself an
   unbounded log-volume source, so the observability that would show an attack also
   becomes part of its cost.

Neither problem is about guessing codes. Throttling bounds the blast radius of traffic
that is not guessing anything.

Design constraints, in priority order
------------------------------------
1. **Never weaken authority behaviour.** A rate-limited request is refused *before* the
   handler runs, so no partially applied state is possible.
2. **A 429 must not reveal anything about the credential.** The decision is a pure
   function of "how many requests has this client made", never of whether a code was
   valid. That keeps the existing indistinguishability between invalid, expired, and
   already-consumed codes intact.
3. **Fail open, not closed, on limiter faults.** An internal error in the limiter must
   not become an outage on the sign-in path.
4. **Per client, not per endpoint-wide.** A shared NAT or office egress would
   otherwise throttle every legitimate user behind it.

Scope and limits, stated rather than implied
--------------------------------------------
* The window is in-process. This is correct for the single-instance deployment in
  `deploy/`; behind more than one instance the effective ceiling is
  `limit x instances`. A shared store (Redis, or a DB table) is required for a
  multi-instance ceiling, and is deliberately not faked here.
* The key is the client address as seen by the proxy in front of the service. If the
  deployment terminates TLS without a proxy, every client shares one key. See
  `client_key_for` for the `X-Forwarded-For` handling and the deployment claim it
  depends on.
"""

from __future__ import annotations

import os
import time
from collections import deque
from threading import Lock

from fastapi import Request

# Generous by default. The purpose is to bound a runaway or hostile client, not to
# ration ordinary use: a field operator pairing a device should never see this.
DEFAULT_LIMIT = 30
DEFAULT_WINDOW_SECONDS = 60.0

# Paths on the unauthenticated auth surface. Scoped deliberately rather than applied
# globally: authenticated sync traffic is already bounded by per-organization push
# serialization, and throttling it by client address would penalise everyone behind a
# shared egress for one noisy device.
THROTTLED_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v1/auth/pair",
)

# Never more than this many distinct keys retained, so the limiter's own memory is
# bounded by a hostile client presenting many source addresses.
MAX_TRACKED_CLIENTS = 10_000


def _positive_float(name: str, fallback: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _positive_int(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def limit_for() -> int:
    return _positive_int("VITRIAL_AUTH_RATE_LIMIT", DEFAULT_LIMIT)


def window_seconds_for() -> float:
    return _positive_float("VITRIAL_AUTH_RATE_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)


def client_key_for(request: Request) -> str:
    """Identify the client for throttling purposes.

    Prefers the left-most `X-Forwarded-For` entry, which is the originating client as
    recorded by the outermost trusted proxy. This is only as trustworthy as the
    deployment: if the service is directly internet-facing with no proxy that strips
    client-supplied headers, a caller can spoof the header and evade the limit
    entirely. The service does not terminate TLS directly in `deploy/`, so the header
    is set by infrastructure rather than by the caller. That dependency is deliberate
    and is called out in the module docstring rather than left implicit.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return f"xff:{first}"
    client = request.client
    if client is not None and client.host:
        return f"peer:{client.host}"
    return "peer:unknown"


class SlidingWindowLimiter:
    """Fixed-capacity sliding window over per-client request timestamps.

    Deliberately not a token bucket: a token bucket allows a burst equal to its full
    capacity, which is the wrong shape for bounding rejected-auth traffic.
    """

    def __init__(self, limit: int, window_seconds: float, max_clients: int = MAX_TRACKED_CLIENTS) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_clients = max_clients
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Return `(allowed, retry_after_seconds)`.

        `retry_after_seconds` is 0.0 when allowed.
        """
        moment = time.monotonic() if now is None else now
        cutoff = moment - self._window
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self._max_clients:
                    self._evict_locked(cutoff)
                bucket = deque()
                self._hits[key] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                # Wait until the oldest hit leaves the window, so a client that backs
                # off is admitted as soon as it is actually safe rather than after a
                # fixed penalty.
                return False, max(0.0, bucket[0] + self._window - moment)
            bucket.append(moment)
            return True, 0.0

    def _evict_locked(self, cutoff: float) -> None:
        """Drop keys with no hits inside the window, then the oldest if still full.

        A host that has gone quiet is the cheapest thing to forget, so expiry is tried
        before recency so a long-idle deployment does not pin its whole capacity.
        """
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]
        if len(self._hits) < self._max_clients:
            return
        oldest = min(self._hits.items(), key=lambda item: item[1][-1] if item[1] else 0.0)
        del self._hits[oldest[0]]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter: SlidingWindowLimiter | None = None
_limiter_key: tuple[int, float] | None = None


def limiter_for() -> SlidingWindowLimiter:
    """Process-wide limiter, rebuilt if configuration changed since last use.

    Reading the environment per request would be cheap but makes the window
    uninspectable; caching on the resolved configuration keeps both cheap and
    observable.
    """
    global _limiter, _limiter_key
    config = (limit_for(), window_seconds_for())
    if _limiter is None or _limiter_key != config:
        _limiter = SlidingWindowLimiter(config[0], config[1])
        _limiter_key = config
    return _limiter


def reset_limiter_for_tests() -> None:
    global _limiter, _limiter_key
    _limiter = None
    _limiter_key = None


def path_is_throttled(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in THROTTLED_PATH_PREFIXES)
