from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://vitrial:vitrial@localhost:5432/vitrial"
    database_null_pool: bool = False
    service_version: str = "0.3.0"

    # Connection pool, sized explicitly.
    #
    # Previously every pool parameter was left at a library default: pool_size 5,
    # max_overflow 10, pool_timeout 30, no recycling, and no server-side statement
    # timeout. With one uvicorn process (see Dockerfile) that is a hard ceiling of 15
    # connections for the whole service, and -- because there was no statement
    # timeout -- a slow query could hold a connection indefinitely. A handful of slow
    # requests therefore exhausts the pool and turns every other request into a
    # pool_timeout failure. That is a cascading outage with no single failing
    # component, which is the worst shape for diagnosis.
    #
    # The defaults here are sized for a single process. `pool_size + max_overflow`
    # must stay below the server's `max_connections` *per process*, so if the worker
    # count is ever raised, this budget has to be divided, not multiplied.
    database_pool_size: int = 10
    database_max_overflow: int = 5
    # Deliberately shorter than the 30s default: a caller waiting 30s for a
    # connection is already a failed request, and a fast, explicit failure surfaces
    # pool exhaustion instead of looking like general slowness.
    database_pool_timeout_seconds: float = 10.0
    # Recycle below the ~10 minute idle timeout typical of managed PostgreSQL and of
    # connection-breaking intermediaries, so the pool never hands out a socket the
    # server has already closed.
    database_pool_recycle_seconds: int = 300
    # Server-side ceiling, so a pathological query is killed by PostgreSQL rather
    # than holding a connection until the client gives up. This is the control that
    # actually prevents pool exhaustion; the client-side pool timeout only bounds the
    # waiting.
    database_statement_timeout_ms: int = 15_000
    api_version: str = "v1"

    # Internal provisioning is disabled unless a SHA-256 digest of a separate
    # admin key is explicitly configured. Normal bearer sessions never satisfy
    # this boundary.
    admin_api_key_hash: str | None = None

    evidence_storage_provider: Literal["local", "s3"] = "local"
    evidence_root: Path = Path(".evidence")
    evidence_gc_grace_seconds: int = 86400
    # Defense-in-depth cap enforced while streaming, including chunked requests
    # that omit Content-Length. Production may lower this value to match field policy.
    evidence_max_bytes: int = 100 * 1024 * 1024

    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "vitrial-evidence"
    s3_region: str = "us-east-1"
    s3_part_size_bytes: int = 5 * 1024 * 1024

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
