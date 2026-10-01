"""Explicit connection-pool configuration.

These assert the *values that reach the engine*, not just that settings exist, because
the failure this guards against is precisely a default silently taking over.
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import NullPool

from app import db
from app.settings import Settings


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for name in (
        "DATABASE_NULL_POOL",
        "DATABASE_POOL_SIZE",
        "DATABASE_MAX_OVERFLOW",
        "DATABASE_POOL_TIMEOUT_SECONDS",
        "DATABASE_POOL_RECYCLE_SECONDS",
        "DATABASE_STATEMENT_TIMEOUT_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def _kwargs(**overrides) -> dict:
    """Evaluate the engine kwargs with `overrides` applied, restoring state after.

    Two things this deliberately does *not* inherit from the environment:

    * `database_null_pool` is pinned to `False` unless a test asks for it. The
      integration job in CI exports `DATABASE_NULL_POOL=true`, so a helper that
      inherited the ambient mode asserted the NullPool branch locally and the pooled
      branch in CI -- green on one machine, red on the other, for the same commit.
      Pool tests must state which mode they are about.
    * `Settings` is a pydantic model behind a module-level singleton, so overrides
      are applied to the instance and restored in a `finally`. An earlier version
      deleted the attribute outright, permanently breaking every later test.
    """
    effective = {"database_null_pool": False, **overrides}
    previous = {name: getattr(db.settings, name) for name in effective}
    try:
        for name, value in effective.items():
            setattr(db.settings, name, value)
        return db._engine_kwargs()
    finally:
        for name, value in previous.items():
            setattr(db.settings, name, value)


def test_pool_mode_is_pinned_by_the_test_not_the_environment(monkeypatch):
    """Guards the guard: the helper must ignore an ambient DATABASE_NULL_POOL.

    Without this, a future test that forgets to pin the mode fails only in CI, which
    is the expensive way to find out.
    """
    monkeypatch.setenv("DATABASE_NULL_POOL", "true")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "7")
    assert "pool_size" in _kwargs(), "pool tests must not inherit the ambient pool mode"

    assert "pool_size" not in _kwargs(database_null_pool=True)


def test_pool_is_sized_explicitly_not_left_to_library_defaults():
    kwargs = _kwargs(
        database_pool_size=7,
        database_max_overflow=3,
        database_pool_timeout_seconds=4.0,
        database_pool_recycle_seconds=120,
    )
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 3
    assert kwargs["pool_timeout"] == 4.0
    assert kwargs["pool_recycle"] == 120
    # The library default was 5, which is what silently capped the service before.
    assert kwargs["pool_size"] != 5


def test_connection_lifetime_is_bounded():
    kwargs = _kwargs(database_pool_recycle_seconds=300)
    assert 0 < kwargs["pool_recycle"] <= 600, "must recycle below a typical managed-PostgreSQL idle timeout"


def test_statement_timeout_is_applied_server_side():
    kwargs = _kwargs(database_statement_timeout_ms=9_000)
    server_settings = kwargs["connect_args"]["server_settings"]
    assert server_settings["statement_timeout"] == "9000"
    # A client-side timeout cannot replace this: it bounds the wait, not the query,
    # so the connection stays pinned either way.
    assert server_settings["idle_in_transaction_session_timeout"] == "9000"


def test_statement_timeout_applies_even_under_null_pool():
    """NullPool discards pool arguments but must not discard the statement timeout.

    The statement timeout is a server-side setting, not a pool behaviour, and it is
    the control that stops a slow query pinning a connection. Losing it under
    NullPool would reintroduce exactly the exhaustion this is meant to prevent.
    """
    kwargs = _kwargs(database_null_pool=True)
    assert kwargs["poolclass"] is NullPool
    assert "pool_size" not in kwargs
    assert "max_overflow" not in kwargs
    assert "pool_timeout" not in kwargs
    assert "pool_recycle" not in kwargs
    assert kwargs["connect_args"]["server_settings"]["statement_timeout"] == "15000"


def test_null_pool_drops_pre_ping_but_pooled_mode_keeps_it():
    assert "pool_pre_ping" not in _kwargs(database_null_pool=True)
    assert _kwargs(database_null_pool=False)["pool_pre_ping"] is True


def test_pooled_mode_does_not_set_null_pool():
    assert "poolclass" not in _kwargs(database_null_pool=False)


def test_default_pool_budget_leaves_room_for_the_server():
    """`pool_size + max_overflow` is a per-process ceiling, not a target.

    PostgreSQL's own default `max_connections` is 100, and the service runs one
    process (Dockerfile has no `--workers`). A budget that consumed most of the
    server would starve migrations, psql, and any second process -- including the
    one an operator starts while investigating an incident.
    """
    settings = Settings()
    budget = settings.database_pool_size + settings.database_max_overflow
    assert budget <= 50, (
        f"default pool budget {budget} is too close to a typical max_connections of 100; "
        "leave headroom for migrations and operator access"
    )


def test_engine_is_built_with_a_statement_timeout():
    """End-to-end: the live engine carries the server-side setting.

    Guards against the arguments being computed correctly and then not passed, which
    is the same shape of defect as a contract that is computed but never consumed.
    """
    kwargs = _kwargs(database_statement_timeout_ms=7_777)
    assert kwargs["connect_args"]["server_settings"]["statement_timeout"] == "7777"
