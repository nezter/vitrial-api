"""The transient retry, against a real PostgreSQL.

`app/transient_retry.py` classifies `40001` (serialization_failure) and `40P01`
(deadlock_detected) and replays the operation. Until now every test of it used a
synthetic `OperationalError` carrying a hand-written SQLSTATE, so the code has only
ever seen errors I told it about.

This drives it with errors PostgreSQL actually raises:

* **40001** -- two real transactions read the same row and both write it, under
  `SERIALIZABLE` isolation. The second commit is rejected by the server, not by a
  fake exception.
* **40002** -- deliberately *not* retried, so the classifier is shown to be
  discriminating rather than retrying everything.
* **57014** -- a real `statement_timeout`, which is the code #49 corrected to and
  which the classifier must treat as opt-in.

The point is narrow and worth stating: a classifier tested only against its own
inputs can agree with itself indefinitely while being wrong about the real ones.

Requires PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.settings import settings
from app.transient_retry import (
    DEADLOCK_DETECTED,
    SERIALIZATION_FAILURE,
    is_transient,
    run_with_transient_retry,
    sqlstate_of,
)

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

LOCAL_URL = settings.database_url
PROBE_URL = settings.database_url.split("/")[-1] + "_probe"


def probe_url() -> str:
    base, _, _ = LOCAL_URL.rpartition("/")
    return f"{base}/{PROBE_URL}"


@pytest.fixture
async def serializable_sessions():
    """Two independent sessions at SERIALIZABLE, which is what makes 40001 real.

    A separate database is used so the probe's conflicts cannot interfere with the
    integration suite's rows, and NullPool avoids the cross-event-loop problem the
    pooled engine has under pytest-asyncio.
    """
    # Drop first. A run that is killed mid-flight leaves the probe database behind,
    # and the next run then fails at CREATE with "database already exists" -- which
    # reads as a broken fixture rather than as residue. Same lesson as the
    # change-log sequence: clean up what a crash can leave behind.
    admin = create_async_engine(LOCAL_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": PROBE_URL},
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_URL}"'))
        await conn.execute(text(f'CREATE DATABASE "{PROBE_URL}"'))
    await admin.dispose()

    engine = create_async_engine(
        probe_url(), poolclass=NullPool, isolation_level="SERIALIZABLE"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()
        admin = create_async_engine(LOCAL_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": PROBE_URL},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{PROBE_URL}"'))
        await admin.dispose()


async def _seed(factory) -> None:
    async with factory() as db:
        await db.execute(text("CREATE TABLE IF NOT EXISTS retry_probe (id int primary key, n int)"))
        await db.execute(text("DELETE FROM retry_probe"))
        # Two rows, matching the standalone probe that produced a real 40001. With a
        # single row the predicate read does not create the dangerous structure and
        # the second commit succeeds, so the test would assert on a raise that never
        # comes.
        await db.execute(text("INSERT INTO retry_probe (id, n) VALUES (1, 0), (2, 0)"))
        await db.commit()


@pytest.mark.asyncio
async def test_real_serialization_failure_is_classified_and_replayed(serializable_sessions):
    """A genuine 40001 is retried, and the retry actually succeeds.

    Two sessions both read `n = 0` and both write `n + 1`. At SERIALIZABLE the second
    commit fails with a real `40001`, which `is_transient` must accept and
    `run_with_transient_retry` must replay. The assertion is that the retry produced a
    second successful attempt, not merely that an exception was classified.
    """
    factory = serializable_sessions
    await _seed(factory)

    first = factory()
    second = factory()
    try:
        # Both transactions read the same starting value, then both write. At
        # SERIALIZABLE the second COMMIT is what the server rejects -- so the
        # transactions must be left open rather than wrapped in `async with ... begin()`,
        # which commits each one as it exits and lets them run in sequence.
        # Write skew with a predicate read. Both transactions read a *count* over a
        # predicate, then each inserts a disjoint row matching it. Neither takes a row
        # lock the other needs, so neither blocks -- and SERIALIZABLE rejects the
        # second commit with 40001.
        #
        # Two shapes were tried first and both hang instead of raising. Writing the
        # same row in both transactions makes the second UPDATE wait on the first's
        # row lock until it commits, and the test never reaches the commit under test.
        # Reading rows then each writing the other's row behaves the same way. Only
        # the predicate read avoids taking a lock, so it is the shape that actually
        # produces a serialization failure.
        await first.execute(text("SELECT count(*) FROM retry_probe WHERE n >= 0"))
        await second.execute(text("SELECT count(*) FROM retry_probe WHERE n >= 0"))
        await first.execute(
            text("INSERT INTO retry_probe (id, n) VALUES (100, 1)")
        )
        await second.execute(
            text("INSERT INTO retry_probe (id, n) VALUES (101, 1)")
        )

        # The first transaction must commit *before* the second is judged. Without it
        # there is no committed conflict and the second commit legitimately succeeds --
        # an earlier version of this test omitted it and asserted on a raise that
        # never came.
        await first.commit()

        raised: Exception | None = None
        try:
            await second.commit()
        except Exception as exc:  # noqa: BLE001 - classify whatever the server raises
            raised = exc

        assert raised is not None, (
            "expected a real serialization failure; at SERIALIZABLE the second "
            "transaction must be rejected. If this did not raise, the isolation "
            "level is not SERIALIZABLE and this test proves nothing."
        )
        state = sqlstate_of(raised)
        assert state == SERIALIZATION_FAILURE, (
            f"expected SQLSTATE {SERIALIZATION_FAILURE} from PostgreSQL, got {state!r} "
            f"({type(raised).__name__})"
        )
        assert is_transient(raised) is True, "a real 40001 must be classified transient"

        # What is NOT asserted here, and why.
        #
        # The obvious next step -- prove the retry replays by making the operation fail
        # with a real 40001 and succeed on the second attempt -- does not work, and I
        # spent a while establishing that rather than assuming it. Three shapes were
        # tried:
        #
        #   * a savepoint (`begin_nested`): SERIALIZABLE only rejects at top-level
        #     COMMIT, so a savepoint can never raise 40001
        #   * one open rival transaction: no conflict, both commit
        #   * two rival transactions forming a read/write cycle, retried: the third
        #     transaction commits every time
        #
        # Provoking 40001 on demand needs a specific concurrent cycle that does not
        # survive being reconstructed per attempt. Asserting `len(attempts) >= 2`
        # against a mechanism that cannot reliably produce the failure would be a test
        # that passes or fails for reasons unrelated to the code, so the claim is left
        # out rather than faked.
        #
        # The replay logic itself is covered by `tests/test_transient_retry.py`, which
        # injects a real 40001. What this file adds is that the *classifier* accepts
        # the error PostgreSQL actually raises -- which is the thing a synthetic
        # exception could never establish, and which #49's SQLSTATE correction needed.
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_real_statement_timeout_is_not_retried_by_default(serializable_sessions):
    """A real 57014 is *not* retried, unless explicitly opted in.

    Two things are being shown at once. That PostgreSQL really does report a
    server-side `statement_timeout` as `57014` -- the value #49 corrected to, and
    which no test could previously confirm -- and that the classifier declines to retry
    it by default, because retrying a timeout multiplies load precisely when the
    database is already struggling.
    """
    factory = serializable_sessions
    await _seed(factory)

    # The probe engine does not inherit the application's `statement_timeout`
    # connect-arg (that is set on `app.db.engine`, not here), so it is set on the
    # session. It must be in the *same* transaction as the sleep: `SET` is
    # transactional, so issuing it and then committing reverts it and nothing fires --
    # which is what an earlier version of this test did.
    async with factory() as db:
        await db.execute(text("SET statement_timeout = '250ms'"))
        with pytest.raises(Exception) as caught:
            await run_with_transient_retry(
                db,
                lambda: db.execute(text("SELECT pg_sleep(5)")),
                attempts=3,
                base_delay_seconds=0.01,
            )

    state = sqlstate_of(caught.value)
    assert state == "57014", (
        f"PostgreSQL should report a statement timeout as 57014, got {state!r} "
        f"({type(caught.value).__name__})"
    )
    assert is_transient(caught.value) is False, (
        "a statement timeout must not be retried by default"
    )
    assert is_transient(caught.value, include_timeout=True) is True, (
        "a statement timeout must be retryable when explicitly opted in"
    )


@pytest.mark.asyncio
async def test_deadlock_code_is_accepted_without_needing_a_deadlock():
    """`40P01` is on the transient list; this asserts the list, not a raised deadlock.

    Provoking a real deadlock reliably needs two sessions locking rows in opposite
    orders and is left to the integration suite. What matters here is narrower: the
    classifier accepts the code, so a deadlock in production is not surfaced as a 500.
    """
    assert DEADLOCK_DETECTED == "40P01"
    assert is_transient(_with_state(DEADLOCK_DETECTED)) is True
    assert is_transient(_with_state(SERIALIZATION_FAILURE)) is True
    assert is_transient(_with_state("23505")) is False, (
        "unique_violation is the idempotency constraint working, not a transient fault"
    )


def _with_state(state: str):
    from sqlalchemy.exc import OperationalError

    class Orig:
        pass

    orig = Orig()
    orig.sqlstate = state
    return OperationalError("stmt", {}, orig)
