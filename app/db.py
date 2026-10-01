from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.settings import settings

# Pool arguments are only meaningful for a real pooling implementation. NullPool opens
# a connection per checkout, so pool_size/max_overflow/pool_timeout have nothing to
# bound and passing them is at best noise. Recycling is also meaningless because no
# connection is retained.
#
# The statement timeout is applied in both modes: it is a server-side setting, not a
# pool behaviour, and it is the control that stops a slow query from pinning a
# connection regardless of which pooling strategy is in use.
def _pool_kwargs() -> dict:
    return {
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_timeout": settings.database_pool_timeout_seconds,
        "pool_recycle": settings.database_pool_recycle_seconds,
    }


def _connect_args() -> dict:
    """Server-side statement timeout, via asyncpg's connection parameters.

    Set at connect time rather than per session so it applies to every statement on
    every connection, including ones opened by background work that never builds a
    `Session`. A client-side timeout cannot substitute: it aborts the wait, not the
    query, so the connection stays pinned.
    """
    return {
        "server_settings": {
            "statement_timeout": str(settings.database_statement_timeout_ms),
            # Bound idle-in-transaction sessions too. A session that opens a
            # transaction and abandons it holds its connection *and* its snapshot,
            # which blocks vacuum and can stall the whole database, not just this
            # process.
            "idle_in_transaction_session_timeout": str(
                settings.database_statement_timeout_ms
            ),
        }
    }


def _engine_kwargs() -> dict:
    kwargs: dict = {
        "pool_pre_ping": True,
        "connect_args": _connect_args(),
    }
    if settings.database_null_pool:
        kwargs["poolclass"] = NullPool
        # pre_ping is pointless without reuse: every connection is brand new.
        kwargs.pop("pool_pre_ping", None)
    else:
        kwargs.update(_pool_kwargs())
    return kwargs


engine = create_async_engine(settings.database_url, **_engine_kwargs())
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
