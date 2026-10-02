---
kind: candidate
recordKind: continuity
category: goal-set
seed_type: goal-set
name: vitrial-api-state-2026-10-02
description: "vitrial-api at clean main 208afa4: 293 tests green, 1 skipped. Six verified defects VITR-V001..V006 filed in trackers/vitrial-api-TRACKER.md. First-contribution target: VITR-V001 guard-prefix dead rules."
agent: nez
date: 2026-10-02
tasks: []
projects: [vitrial-api]
repos: [vitrial-api]
---

## State

`/home/nez/Projects/vitrial-api` @ `208afa4` (HEAD, clean tree). PostgreSQL 17-backed
suite: **293 passed, 1 skipped** (S3, needs Docker). No open PRs. Three open issues: #28
(the real 0.3.0 spec), #30 (MinIO→RustFS, landed but box still open), #2 (effectively done).

## UBS status — loop closed 2026-10-02

Real fixes shipped: 6 `security-assert` criticals in `ownership.py` (asserts →
`AuthorizationRejected`), and the blocking read on `LocalObjectStore.stream` now uses
`asyncio.to_thread`. `ubs --only=python` (with `.ubsignore` scoping) dropped criticals
48→16, warnings 65→3. The residual 16/3 are documented false positives for this codebase
and are recorded in the tracker so the loop has a defined floor, not an infinite one —
running `ubs` to literally 0 means distorting `auth.py`'s `token_hash ==` lookup,
`ownership.py`'s int `revision !=` comparison, or the validated `X-Request-ID`, all of
which would be a regression. Next session: treat any *new* class of warning as a real-fix
prompt, and do not re-litigate the false-positive set.

## Good first contribution — W1 (`VITR-V001` guard-layer dead rules), top of the tracker queue

Dead guard prefixes: `request_size.py:79-82` has `/api/v1/sync/v2/push`, `/api/v1/admin`,
`/api/v1/auth/refresh` (none exist); real V2 is `/api/v2/sync/push` with **no** size ceiling
and no `SyncBatchV2` aggregate validator. 20 MB body → 422 on V2 vs 413 on V1. Fix both the
prefixes and the missing aggregate ceiling, and rewrite guard tests to enumerate `app.routes`
rather than assert on literals. Spec'd on #28 P0.

## What to not touch without a deliberate decision

- Soft-delete pull visibility asymmetry (`ownership.py:970-1001`) — `deletedAt` is delivered
  on purpose; a tombstone must be delivered, absence is never evidence. Pinned by
  `test_soft_delete_visibility_probe.py`.
- `delivery_execution` **does** check `deleted_at` at every hop — different rule, different question.
- Evidence PUT intentionally has no middleware ceiling; `evidence_max_bytes` (100 MiB default)
  bounds it while streaming.
- Internal admin routes stay `include_in_schema=False`; disabled unless `ADMIN_API_KEY_HASH` set.
- `resolve_visible`/`resolve_delivery_visible` must stay line-for-line twins of
  `record_is_visible`/`delivery_execution_is_visible` — pinned by
  `test_pull_prefetch_equivalence.py`.

## Burned lessons (each cost a pipeline cycle — do not repeat)

- Run migrations before `python -m pytest` or you get 44 failures that look like defects but
  are only `relation "evidence_blobs" does not exist`.
- Invoke as `python -m pytest`, not the `pytest` console script — the latter aborts collection
  because `tests/test_production_preflight.py` imports `scripts.*`.
- `DATABASE_NULL_POOL=true` is required — pooled engine + fresh-loop-per-test = "attached to a
  different loop" across ~20 tests.
- Three occurrences, same class as `VITR-V001`: a correct component that was not the code that
  ran (`request_size.py:40-44`); `pull_prefetch` excluding delivery under a comment claiming
  the opposite; prefetch "locally green, CI red" for three rounds because the defect was in code
  only the integration job executes.
