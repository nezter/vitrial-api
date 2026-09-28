# Vitrial Connected Operations API

Dedicated backend for the Vitrial 0.2.0 Connected Operations client. This service intentionally lives outside the iOS application repository.

## Contract boundary

The V1 compatibility boundary is the portable contract pack from `scottjoyner/vitrialuminios-del-valle-ios` at exact client SHA:

`74822ac9e2139ac35ea4fbb35ae3323e6a2745c6`

Backend code must remain compatible with `contracts/backend/v1/` and `scripts/validate_backend_contract.py` from that client checkpoint.

## Stack

- FastAPI
- PostgreSQL
- SQLAlchemy 2.x async
- Alembic
- Pydantic Settings
- pytest
- Docker / Compose
- S3-compatible evidence object storage

## First run

```bash
cp .env.example .env
docker compose up -d postgres
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
uvicorn app.main:app --reload
```

## V1 endpoints

- `GET /health`
- `GET /api/v1/version`
- `GET /api/v1/auth/me`
- `POST /api/v1/sync/push`
- `GET /api/v1/sync/pull`
- `HEAD /api/v1/sync/evidence-blobs/{documentID}`
- `PUT /api/v1/sync/evidence-blobs/{documentID}`
- `GET /api/v1/sync/evidence-blobs/{documentID}`

## Structured observability

Every HTTP request receives an `X-Request-ID` response header. A syntactically safe caller-supplied request ID is preserved; otherwise the service generates one.

Application logs are newline-delimited JSON and correlate requests with stable SHA-256-derived references for organization, actor, membership, session, mutation, entity and device IDs. Raw domain IDs are not required for correlation. Sync mutation outcomes are emitted only after the enclosing transaction commits.

The log formatter recursively redacts credential-bearing fields such as Authorization headers, bearer/access/refresh tokens, cookies, passwords, secrets and the internal admin key. Request/response bodies and bearer credentials are never intentionally logged.

## Internal bootstrap/admin control plane

The provisioning routes are deliberately excluded from the pinned public OpenAPI surface:

- `POST /internal/admin/v1/bootstrap`
- `POST /internal/admin/v1/sessions/{sessionID}/revoke`

They are disabled unless `ADMIN_API_KEY_HASH` is configured. This value is the lowercase SHA-256 digest of a separately managed admin key; the admin key is not a normal user bearer token and is never persisted by the service.

Generate a digest without placing the key itself in shell history:

```bash
python - <<'PY'
import getpass, hashlib
key = getpass.getpass("Admin key: ")
print(hashlib.sha256(key.encode("utf-8")).hexdigest())
PY
```

Set only the resulting digest as `ADMIN_API_KEY_HASH`. Send the original key at provisioning time in `X-Vitrial-Admin-Key` over HTTPS.

Bootstrap is capable of creating or updating an organization/user/membership authority set and issuing a new revocable bearer session. Any membership authority change increments the organization's `authorizationRevision`; existing active clients therefore observe the new authority through `/api/v1/auth/me`. Newly issued access tokens are returned once and stored only as SHA-256 hashes. Session revocation is idempotent and takes effect on the next authenticated operation.

## Deployment readiness

The runtime image executes as an unprivileged UID/GID, runs with a writable evidence home only for local-development fallback, and includes a dependency probe that verifies both PostgreSQL and the configured object store without printing connection strings or credentials.

Deployment assets live in `deploy/`:

- `compose.production.yml` — API + Alembic migration job + Caddy; expects external persistent PostgreSQL and S3-compatible object storage.
- `compose.acceptance.yml` — single-host physical-device environment with isolated persistent PostgreSQL + S3 object storage volumes.
- `Caddyfile` — public automatic HTTPS edge.
- `Caddyfile.smoke` — localhost internal-CA TLS for CI only.
- `env.production.example` — non-secret configuration template.

Before any real deployment, validate the host-only env file:

```bash
python scripts/validate_deployment.py --env-file /etc/vitrial/vitrial.env --mode production
```

The validator fails closed on placeholder/default credentials, group/world-readable secret files, localhost production persistence, insecure external S3 endpoints, malformed admin-key digests, and mutable API images. See `deploy/README.md` for deploy, rollback and acceptance instructions.

## Security invariants

- Access tokens are stored only as SHA-256 hashes.
- Internal admin authentication is separate from normal bearer-session authentication and is disabled by default.
- Roles are informational; explicit capabilities are authority.
- Server mutations resolve canonical tenant/parent ownership from server state.
- `clientMutationID` is idempotent within an organization and fingerprint-bound to the original request.
- Stale `baseServerRevision` never silently overwrites canonical state.
- Audit/history provenance stores actor/org/membership/session/revision, never bearer tokens.
- Evidence uploads are streamed and digest checked before canonical promotion.
- Evidence replacement/tombstone GC rechecks canonical/reference state before physical deletion.
- Production deployment exposes only the TLS edge; databases/object storage are not application-public endpoints.

## Current release boundary

Canonical ownership/authorization, lifecycle/concurrency hardening, S3-compatible evidence persistence/GC, structured observability, admin provisioning, and deployment topology/preflight automation are implemented and CI-proven. A real public deployment still must prove DNS/trusted HTTPS plus the selected persistent PostgreSQL/object-storage/secret-management services. After that, the remaining release gate is the required two-user/two-physical-device iOS acceptance run.
