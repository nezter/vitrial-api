---
id: VITR-TRACKER-001
title: "vitrial-api — reconciled work tracker"
owner: vitrial-api
category: TRACKER
status: active
date: 2026-10-02
---

# vitrial-api — Work Tracker

> Reconciles GitHub issues against what has **actually shipped** on `main`
> (HEAD `208afa4`, 2026-10-02). Only check a box when a commit in `git log` is
> evidence of it. Use `[~]` for the part-that-works, not `[x]`.

## Legend

- `[x]` shipped and verified
- `[~]` partial — some of the claim holds, rest is tracked as a defect
- `[ ]` not done

## GitHub issues

| # | Title | State |
|---|---|---|
| 28 | [0.3.0] Production foundation | OPEN — the real 0.3.0 spec |
| 30 | [0.3.0] Replace legacy MinIO dependency | OPEN — landed in `#34/#35/#63`, box still open |
| 2 | [0.2.0] Harden backend for device acceptance | Open — effectively complete |

No open PRs. No Jira, no in-repo board, no beads records.

---

## Issue #28 reconciliation (2026-10-02)

### P0 — bound the data plane

- [~] Define a hard **aggregate** sync request ceiling enforced while reading the body.
  V1 has it (`MAX_SYNC_V1_BATCH_PAYLOAD_BYTES` + `RequestSizeLimitMiddleware`).
  **V2 does not** — see `VITR-V001`. Chunked path is buffered by the middleware.
- [x] Hard sync response/page byte budget, deterministic by record count **and** encoded bytes
  (`pull_page_accepts`, `MAX_SYNC_PULL_RESPONSE_BYTES`; `sync_service.py`).
- [x] Evidence uploads on a separate streaming ceiling (`evidence_max_bytes`).
- [~] Content-Length **and** chunked over-limit tests exist for V1
  (`test_request_size.py`); near-limit-record and compressed-transport tests do not.
- [~] Edge/application rate limits. Auth surface only, and only on one route.
  Sync/evidence/admin are unthrottled; pairing limiter is XFF-evadable (`VITR-V003`).
- [ ] Load-test rejection paths so abusive traffic fails cheaply.

### P0/P1 — production auth/session model

- [~] Separate admin control plane from public listener. Admin is
  `include_in_schema=False` and disabled unless `ADMIN_API_KEY_HASH` is set, but it still
  shares the one listener with public routes.
- [x] Pairing/auth attempt throttling. Implemented on `/api/v1/auth/pair` — but weakened by `VITR-V003`.
- [ ] Production session renewal; today a fixed bearer TTL + revoke-only.
- [ ] Device-bound sessions (copied bearer stays useful without proof-of-possession).
- [x] Explicit revocation, membership-removal, authorization-revision behaviour
  (revoke + revoke-by-org; every `auth/me` and session yields the org's current revision).
- [x] One-time grant exchange outcomes indistinguishable to unauthenticated callers.

### P1 — sync scalability & concurrency

- [x] Benchmark org-wide `FOR UPDATE` serialization (`#59`) — measured, cost noted.
- [ ] Replace repeated `max(server_revision)+1` with a safe revision allocator — not replaced; still correct under the org row lock.
- [x] Remove/contain pull N+1 — `pull_prefetch.py` batched it; measured `#61` (604 → 4).
- [x] Configure DB pool explicitly (`#42`).
- [x] Deadlock/serialization retry policy (`#46/#49/#56`).
- [~] Concurrency tests for duplicate mutation IDs (`test_sync_concurrency.py`),
  tombstone resurrection (`#50`), cross-device lifecycle; evidence-replace contention not directly covered.

### P1 — reproducible deploy & supply chain

- [ ] Reproducible Python lock. Runtime deps are version ranges in `pyproject.toml`, no lock file.
- [~] Pin runtime images by digest. `API_IMAGE` is digest-pinned in production; Caddy, Postgres, S3 images are floating (`#63` warns on the S3 fallback).
- [ ] Image/package vulnerability scanning and SBOM in candidate CI.
- [x] Validate production settings on startup/preflight (`validate_deployment.py`, `probe_production_preflight.py`, `deploy/README.md`).
- [~] Migration compatibility + rollback rehearsal. CI proves `upgrade → downgrade base → upgrade`; production rollback intentionally does **not** auto-downgrade DB (documented).

### P1 — resilience & operations

- [~] Metrics for request rate/latency/status, auth failures, sync outcome, DB pool, S3 latency, GC backlog, page sizes. Only structured durationMs/statusCode logs exist; no metrics endpoint, no pool-exponent, no GC backlog surfacing. `reason=type(exc).__name__` (`VITR-V006`).
- [~] Trace/correlation propagation. `X-Request-ID` accepted/preserved and one-way `sha256:` refs for org/actor/membership/session/entity/device. It is not a W3C `traceparent`; nothing crosses the iOS→API→DB→S3 boundary as one trace.
- [ ] SLOs/alerts for availability, p95, sync failure rate, dependency readiness.
- [ ] Graceful shutdown/drain test with in-flight sync and evidence streams.
- [ ] PostgreSQL backup/PITR and restore drill.
- [~] S3 versioning/retention/restore policy and orphan/GC reconciliation runbook — GC exists and is scheduled (`deploy/gc-evidence.timer`), but no documented restore policy.

### P1 — contract discipline

- [ ] Make V2 the production sync contract target with an explicit V1 deprecation window. Both are served; no window defined.
- [~] Aggregate-size, schema-version, pagination semantics in the OpenAPI/contract docs. `entitySchemaVersion` exists; aggregate byte budgets are enforced but not fully described in the pinned contract.
- [x] Federation fixtures consumed by both Python and Swift tests (`contracts/`).
- [~] Old-client-against-new-server and vice-versa acceptance tests. V1 is pinned to a client SHA, but no cross-version acceptance matrix exists.
- [x] Server authority never depends on iOS-only validation — ownership/lifecycle re-derived server-side in `ownership.py`, `lifecycle.py`, `delivery_execution.py`.

---

## Verified defects (VITR-Vxxx)

| ID | Severity | Title | Status |
|---|---|---|---|
| `VITR-V001` | HIGH | V2 request size / aggregate ceiling missing; dead guard prefixes on three routes | **Fixed** in W1 (prefixes corrected, `SyncBatchV2` aggregate ceiling, guard lint in CI) |
| `VITR-V002` | MED | Evidence header params unbounded while destination columns are bounded → 500 not 4xx | Open |
| `VITR-V003` | HIGH | Auth rate limiter evadable via spoofed left-most `X-Forwarded-For` | Open |
| `VITR-V004` | LOW | Reference-data GET endpoints write + commit (`ensure_baseline_publications`) | Open |
| `VITR-V005` | MED | Evidence reference safety scan is O(all measurements + requirements) per delete | Open |
| `VITR-V006` | MED | Rejection outcome is `reason=<exception class name>`, not a code — 183 decisions collapse to ~5 reasons | Open |

### `VITR-V001` — dead guard prefixes

`request_size.py:79-82` and `rate_limit.py` declare prefixes that do not match any
live route (`/api/v1/sync/v2/push`, `/api/v1/admin`, `/api/v1/auth/refresh`). Measured:
`/api/v2/sync/push` accepts a 20 MB body (→422), while `/api/v1/sync/push` rejects at 4 MiB (→413).
**Root cause:** guard tests assert against string literals, none enumerate `app.routes`.
Same class recorded in-repo three times (`request_size.py:40-44`, `pull_prefetch.py`, `Makefile:16-18`).

### `VITR-V002` — evidence header bound gap

`X-Vitrial-Item-ID` / `X-Vitrial-Filename` require `min_length=1`, no `max_length`, in both the pinned
contract and the handler, but `evidence_blobs.item_id` is `String(256)` and `filename` `String(512)`.
An over-long value streams and digest-verifies fully, then fails at `INSERT` → 500.
Should be `max_length` + a 4xx, fixed via a V1 contract revision.

### `VITR-V003` — XFF-evadable rate limit

`client_key_for` keys on the **left-most** `X-Forwarded-For` entry. `deploy/Caddyfile` strips nothing;
`--forwarded-allow-ips=*` and no `trusted_proxies` set. Reproduced: 40 unique spoofed XFFs → 40 unthrottled.
Real client needs Caddy `header_up` / `trusted_proxies` or a right-most key.

### `VITR-V004` — reference GET writes

Every reference-data GET calls `ensure_baseline_publications`, which inserts and `db.commit()`s.
A `GET` mutates, and `/manifest` does it twice per call.

### `VITR-V005` — evidence scan O(N)

`ownership.py:343` (`_ensure_evidence_delete_safe`) and `evidence_gc.py:42`
(`_is_referenced`) load every `measurement`/`customer_requirement` payload and scan in Python.
Linear: 100 rows 11 ms → 5,000 rows 79 ms.

### `VITR-V006` — rejection reason collapses to class name

`sync_service.py:368` sets `reason=type(exc).__name__`. Sync outcome logs carry ~5 reasons while the
code expresses 183. Nothing aggregates them. Needs a rejection-code vocabulary; belongs with the
metrics box on #28.

---

## Work queue — ordered for long sessions

Pick top-down. Each task has: goal, files, tests to run, done-when. Run the suite
(see `AGENTS.md` compliance checklist for the exact invocation) after each task.

### W1 — `VITR-V001` guard-layer dead rules (top target, spec'd on #28 P0)
- [x] `app/request_size.py` + `app/rate_limit.py`: dead prefixes replaced with the real
      `/api/v2/sync/push` and `/internal/admin/v1`; `/api/v1/auth/refresh` dropped.
- [x] Aggregate ceiling added to `SyncBatchV2` (`app/sync_v2.py`), mirroring V1's budget.
- [x] `scripts/check_guard_prefixes.py` enumerates `app.routes` and fails if a declared
      guard prefix matches no live path; wired into the `unit` CI job as a lint step.
- **Tests:** guard lint ok (5 declared prefixes, all live); suite green at 293/1.
- **Done when:** every declared guard maps to a live route, and the V2 batch is rejected
      over the V1 budget. *(Both met.)*

### W2 — `VITR-V002` evidence header bound
- [ ] Add `max_length=256`/`512` to `X-Vitrial-Item-ID` / `X-Vitrial-Filename` in
      `app/main.py:257-258` and in the pinned contract `contracts/backend/v1/openapi.yaml`.
- **Tests:** contract validation + a 413-on-over-long-header test in `tests/`.
- **Done when:** an over-long header is a 4xx before any body is read, and the contract and
      handler agree.

### W3 — `VITR-V003` XFF trust boundary
- [ ] Decide and document: `deploy/Caddyfile` sets `header_up X-Forwarded-For` correctly and
      `--forwarded-allow-ips` is pinned to the Caddy upstream, not `*`. After that,
      `client_key_for`'s left-most key is trustworthy.
- [ ] Add an acceptance assertion so a mis-set trust proxy fails fast.
- **Done when:** 40 unique spoofed XFFs no longer yield 40 unthrottled pair attempts behind
      the documented deployment shape.

### W4 — `VITR-V004` reference GETs writing
- [ ] Move `ensure_baseline_publications` out of the read path: make it a one-time seeding
      step (migration or bootstrap), not a side effect of every manifest/current/publications
      GET.
- **Done when:** a `GET` produces zero rows; a test asserts `select(count())` is unchanged
      across a read.

### W5 — `VITR-V005` evidence safety scan O(N)
- [ ] Replace the full-org `measurement`/`customer_requirement` payload scan in
      `ownership.py:343` and `evidence_gc.py:42` with an indexed reference lookup (the same
      prefetch discipline `pull_prefetch.py` uses).
- **Done when:** evidence delete and GC recheck cost is independent of total org measurement count.

### W6 — `VITR-V006` rejection reasons
- [ ] Introduce a stable rejection-code vocabulary; replace `reason=type(exc).__name__` at
      `app/sync_service.py:368` with it.
- [ ] Make `reason` consumable (aggregatable in logs, counted in a future metric).
- **Done when:** the sync outcome stream distinguishes the distinct authz/lifecycle failure
      classes rather than collapsing them to one token.

### Smaller / hygiene
- [ ] Close issue #30's checkbox (MinIO→RustFS already landed); reconcile #2/#28 in GitHub.
- [ ] The four guard-prefix entries are a Rule-5 violation — after W1, add a lint rule so a
      matching-by-class failure fails, per `AGENTS.md` "measure, don't assert".

## What is done that is spec'd in #28 (leave checked only if true)

This section mirrors the reconciled boxes above; do not remove them. See the per-section
`[x]/[~]` list for the authoritative state.
