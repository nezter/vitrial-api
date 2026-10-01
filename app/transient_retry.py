"""Bounded retry for PostgreSQL errors that are safe to retry.

The problem
-----------
PostgreSQL aborts a transaction in two well-defined, documented situations:

* ``40001`` ``serialization_failure`` -- concurrent transactions updated rows and the
  order produced a serialization anomaly, so one is rolled back.
* ``40P01`` ``deadlock_detected`` -- two transactions deadlocked and the deadlock
  detector chose one victim.

Both are transient by design. PostgreSQL's own guidance is that the *whole*
transaction may be replayed. The service does not handle either, so today a
perfectly normal concurrent push surfaces to the client as an unhandled 500. The
iOS client cannot distinguish "retry this" from "this will never work", and an
operator sees a generic failure for a condition that resolves itself on the next
attempt.

The plan calls for this: *"add safe transient database retry policy where
PostgreSQL may abort concurrent transactions"*.

Why retry is safe here
----------------------
Only for **idempotent** units of work, and the sync push is idempotent by
construction rather than by luck:

* `SyncChangeLog` carries `UniqueConstraint("organization_id", "client_mutation_id")`.
* A replay of an already-committed mutation is detected and answered with the
  original result (`reason="idempotent_replay"`), rather than being applied twice.

So replaying a push after an abort cannot double-apply work. That is the property
that makes retry safe, and it is why the policy is opt-in per call site instead of
being wrapped around every transaction in the service.

What is deliberately **not** retried
------------------------------------
* ``23505`` unique violation. That is usually the idempotency constraint *working* --
  the application's own handling turns it into a replay acknowledgement, and a
  generic retry would fight that logic.
* Any error not explicitly listed. An unknown SQLSTATE is treated as permanent,
  because retrying a permanent failure converts a fast, clear error into a slow,
  misleading one.
* ``40002`` statement_timeout. It is transient in the sense that the query stops, but
  retrying it by default would multiply load precisely when the database is already
  struggling. It is available as an opt-in for callers that know their work is cheap
  to re-run.

Bounded attempts with short backoff: this is a correctness aid for a race, not a
load-shedding mechanism, and it must not become a retry storm under contention.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from sqlalchemy.exc import DBAPIError

T = TypeVar("T")

# PostgreSQL SQLSTATEs that mean "the transaction was aborted, replay it".
SERIALIZATION_FAILURE = "40001"
DEADLOCK_DETECTED = "40P01"
LOCK_NOT_AVAILABLE = "55P03"
STATEMENT_TIMEOUT = "40002"

# Defaults are deliberately small. A serialization failure is usually resolved by
# simply being next in line, so a long backoff mostly adds latency to the winner.
DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 0.05
DEFAULT_MAX_DELAY_SECONDS = 0.5

TRANSIENT_SQLSTATES = frozenset({SERIALIZATION_FAILURE, DEADLOCK_DETECTED, LOCK_NOT_AVAILABLE})


def sqlstate_of(exc: BaseException) -> str | None:
    """Extract the SQLSTATE from a SQLAlchemy DBAPI error, if there is one."""
    if not isinstance(exc, DBAPIError):
        return None
    code = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if code:
        return str(code)
    # asyncpg exposes pgcode; SQLAlchemy's asyncpg dialect usually maps it to
    # sqlstate, but fall back so a dialect change cannot silently disable retry.
    return str(code) if code is not None else None


def is_transient(exc: BaseException, *, include_timeout: bool = False) -> bool:
    state = sqlstate_of(exc)
    if state is None:
        return False
    if state == STATEMENT_TIMEOUT:
        return include_timeout
    return state in TRANSIENT_SQLSTATES


async def _rollback_quietly(db) -> None:
    """Roll back, tolerating a session that is already unusable.

    After a serialization failure the session is in a failed state and must be
    rolled back before reuse. A rollback that itself fails must not mask the original
    error, so it is swallowed here and the original is re-raised by the caller.
    """
    try:
        await db.rollback()
    except Exception:  # pragma: no cover - defensive
        pass


async def run_with_transient_retry(
    db,
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
    include_timeout: bool = False,
    on_retry: Callable[[BaseException, int], None] | None = None,
) -> T:
    """Run `operation`, replaying it on transient PostgreSQL aborts.

    `operation` must be idempotent. It is passed the session via closure and is
    re-executed in full after a rollback -- never resumed from the failure point,
    because the failed transaction's state is gone.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if not is_transient(exc, include_timeout=include_timeout):
                raise
            if attempt == attempts:
                # Out of attempts. Surface the original database error rather than
                # a wrapper, so the SQLSTATE and the real cause remain visible.
                raise
            await _rollback_quietly(db)
            if on_retry is not None:
                on_retry(exc, attempt)
            delay = min(base_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable: loop always returns or raises")
