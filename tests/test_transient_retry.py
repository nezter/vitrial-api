"""Bounded retry for transient PostgreSQL aborts.

The property that matters most is the negative one: retry must not fire for
permanent errors. A retry policy that retries everything converts a fast, clear
failure into a slow, misleading one, and under contention becomes a retry storm.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError

from app.transient_retry import (
    DEADLOCK_DETECTED,
    LOCK_NOT_AVAILABLE,
    SERIALIZATION_FAILURE,
    STATEMENT_TIMEOUT,
    is_transient,
    run_with_transient_retry,
    sqlstate_of,
)


class _FakeOrig:
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate


def _dbapi(sqlstate: str) -> OperationalError:
    return OperationalError("stmt", {}, _FakeOrig(sqlstate))


class _FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize("state", [SERIALIZATION_FAILURE, DEADLOCK_DETECTED, LOCK_NOT_AVAILABLE])
def test_documented_aborts_are_transient(state):
    assert is_transient(_dbapi(state)) is True


def test_unique_violation_is_not_transient():
    """23505 is usually the idempotency constraint working, not a failure to retry.

    Retrying it generically would fight the application's own replay handling.
    """
    assert is_transient(_dbapi("23505")) is False


def test_unknown_sqlstate_is_treated_as_permanent():
    assert is_transient(_dbapi("XX000")) is False


def test_statement_timeout_is_opt_in():
    """Transient, but not by default.

    Retrying a timeout by default would add load precisely when the database is
    already struggling, which is the opposite of what a timeout is telling you.
    """
    assert is_transient(_dbapi(STATEMENT_TIMEOUT)) is False
    assert is_transient(_dbapi(STATEMENT_TIMEOUT), include_timeout=True) is True


def test_non_database_exception_is_not_transient():
    assert is_transient(ValueError("nope")) is False
    assert sqlstate_of(ValueError("nope")) is None


def test_exception_without_sqlstate_is_not_transient():
    class _NoState(Exception):
        pass

    assert is_transient(DBAPIError("stmt", {}, Exception("boom"))) is False


# ------------------------------------------------------------------------- retry


@pytest.mark.asyncio
async def test_transient_failure_is_retried_until_it_succeeds():
    session = _FakeSession()
    calls = {"n": 0}

    async def operation():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _dbapi(SERIALIZATION_FAILURE)
        return "committed"

    result = await run_with_transient_retry(session, operation, base_delay_seconds=0)
    assert result == "committed"
    assert calls["n"] == 3
    # One rollback per replay, so the session is usable again each time.
    assert session.rollbacks == 2


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried():
    session = _FakeSession()
    calls = {"n": 0}

    async def operation():
        calls["n"] += 1
        raise _dbapi("23505")

    with pytest.raises(OperationalError):
        await run_with_transient_retry(session, operation, base_delay_seconds=0)
    assert calls["n"] == 1, "a unique violation must not be replayed"
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_retries_are_bounded_and_surface_the_original_error():
    session = _FakeSession()
    calls = {"n": 0}

    async def operation():
        calls["n"] += 1
        raise _dbapi(DEADLOCK_DETECTED)

    with pytest.raises(OperationalError) as caught:
        await run_with_transient_retry(
            session, operation, attempts=3, base_delay_seconds=0
        )
    assert calls["n"] == 3
    # The database's own error must reach the caller, not a wrapper, so the SQLSTATE
    # and the real cause stay visible.
    assert caught.value.__cause__ is None or isinstance(caught.value, OperationalError)
    assert sqlstate_of(caught.value) == DEADLOCK_DETECTED


@pytest.mark.asyncio
async def test_success_on_first_attempt_does_not_roll_back():
    session = _FakeSession()

    async def operation():
        return "ok"

    assert await run_with_transient_retry(session, operation) == "ok"
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_replay_is_of_the_whole_operation_not_a_resume():
    """A serialization failure destroys the transaction's state.

    So the callable must be re-executed in full. A policy that resumed from the
    failure point would silently operate on an aborted transaction.
    """
    session = _FakeSession()
    trace: list[str] = []

    async def operation():
        trace.append("enter")
        if len(trace) < 3:
            raise _dbapi(SERIALIZATION_FAILURE)
        trace.append("work")
        return "done"

    await run_with_transient_retry(session, operation, base_delay_seconds=0)
    # Two full attempts then a success: enter, enter, enter, work.
    assert trace == ["enter", "enter", "enter", "work"]


@pytest.mark.asyncio
async def test_on_retry_reports_each_replay():
    """Every replay is reported, so contention is observable rather than silent."""
    session = _FakeSession()
    reports: list[tuple[str | None, int]] = []
    calls = {"n": 0}

    async def operation():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _dbapi(SERIALIZATION_FAILURE)
        return "ok"

    def _record(exc, attempt):
        reports.append((sqlstate_of(exc), attempt))

    assert await run_with_transient_retry(
        session, operation, base_delay_seconds=0, on_retry=_record
    ) == "ok"
    assert reports == [(SERIALIZATION_FAILURE, 1), (SERIALIZATION_FAILURE, 2)]


@pytest.mark.asyncio
async def test_backoff_grows_and_is_capped():
    session = _FakeSession()
    delays: list[float] = []
    calls = {"n": 0}

    async def operation():
        calls["n"] += 1
        if calls["n"] < 5:
            raise _dbapi(SERIALIZATION_FAILURE)
        return "ok"

    real_sleep = __import__("app.transient_retry", fromlist=["asyncio"]).asyncio.sleep

    async def _record_sleep(seconds):
        delays.append(seconds)
        await real_sleep(0)

    import app.transient_retry as module

    original = module.asyncio.sleep
    module.asyncio.sleep = _record_sleep
    try:
        await run_with_transient_retry(
            session,
            operation,
            attempts=6,
            base_delay_seconds=0.05,
            max_delay_seconds=0.2,
        )
    finally:
        module.asyncio.sleep = original

    assert delays == sorted(delays), "backoff must not shrink"
    assert all(d <= 0.2 for d in delays), "backoff must respect the cap"
    assert delays[0] < delays[-1], "backoff must actually grow"


@pytest.mark.asyncio
async def test_attempts_must_be_positive():
    session = _FakeSession()

    async def operation():
        return "ok"

    with pytest.raises(ValueError):
        await run_with_transient_retry(session, operation, attempts=0)


# ------------------------------------------------------------- wiring into push


class _Principal:
    organization_id = "org-retry-test"
    user_id = "user-retry-test"
    membership_id = "membership-retry-test"
    session_id = "session-retry-test"


class _Batch:
    deviceID = "device-retry-test"
    cursor = None
    records: list = []



@pytest.mark.asyncio
async def test_apply_push_replays_on_a_serialization_failure(monkeypatch):
    """End to end through the real `apply_push` entry point.

    The unit tests above prove the policy; only this proves the policy is *wired in*.
    A correct helper that nothing calls is the recurring failure in this repository, so
    the wiring is asserted rather than assumed.
    """
    from app import sync_service

    calls = {"n": 0}

    async def _flaky(db, principal, batch):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _dbapi(SERIALIZATION_FAILURE)
        return "committed"

    monkeypatch.setattr(sync_service, "_apply_push_once", _flaky)

    principal = _Principal()
    result = await sync_service.apply_push(_FakeSession(), principal, _Batch())
    assert result == "committed"
    assert calls["n"] == 2, "the first attempt should have been replayed"


@pytest.mark.asyncio
async def test_apply_push_does_not_replay_a_permanent_error(monkeypatch):
    from app import sync_service

    calls = {"n": 0}

    async def _broken(db, principal, batch):
        calls["n"] += 1
        raise _dbapi("23505")

    monkeypatch.setattr(sync_service, "_apply_push_once", _broken)

    with pytest.raises(OperationalError):
        await sync_service.apply_push(_FakeSession(), _Principal(), _Batch())
    assert calls["n"] == 1
