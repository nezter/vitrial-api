# vitrial-api

Server authority for the Vitrial iOS configurator. FastAPI + PostgreSQL 17 + S3-compatible
evidence storage. Read `README.md` for the stack, endpoints, and security invariants, and
`deploy/README.md` for deploy/rollback/acceptance.

## Where agents look — in order

1. **This file** — routing and the compliance checklist below.
2. `~/.agents/skills/vitrial-api-guardrails/SKILL.md` — the binding rules for code in this
   repo. It is auto-loaded as a skill; you do not need to paste it.
3. `trackers/vitrial-api-TRACKER.md` — where every issue and defect actually stands
   (`VITR-V001`…`VITR-V006`). The single source of truth for defect state.
4. `docs/CONVENTIONS.md` — where things live and how to name them.
5. `docs/INDEX.md` — full map of the docs.
6. `sessions/` and `memory-seeds/` — continuity for your next session.

Contract boundary: V1 is pinned to iOS client SHA `74822ac9`. Any change to
`contracts/backend/v1/` or the V1/V2 vocabulary must pass
`python scripts/validate_backend_contract.py` and `tests/test_contract_vocabulary_drift.py`.

## Compliance checklist — run before you call it done

**Test environment (cheapest trap first):**
- [ ] Apply migrations first: `alembic upgrade head`. Without them the suite fails 44 tests
      that look like defects but are only `relation "evidence_blobs" does not exist`.
- [ ] Invoke as `python -m pytest`, never the `pytest` console script (the latter aborts
      collection because `tests/test_production_preflight.py` imports `scripts.*`).
- [ ] Set `DATABASE_NULL_POOL=true POSTGRES_INTEGRATION=1`. A pooled engine + fresh-loop-per-test
      = "attached to a different loop" across ~20 tests.
- [ ] Baseline to compare: **293 passed, 1 skipped** (S3 needs Docker).

**Rules that are already held — do not churn them:**
- [ ] Ripgrep only, never grep.
- [ ] Env-gated, default OFF, degrade honestly (admin routes 404 when no key hash; rate limiter
      fails open and logs the bypass).
- [ ] No stubs — but `VITR-V001` is a Rule-5 violation in the guard layer: 4 declared guard
      prefixes match no live route. Fixing that is the top contribution target, not a regression.
- [ ] Measure, don't assert. And verify a guard asserts against the live path — `test_request_size.py`
      currently asserts against a phantom path and passes.

**Hard invariants that look like bugs — pinned by tests, do not "fix":**
- [ ] Soft-deleted records stay visible in a pull (`deletedAt` is delivered; absence is never evidence).
- [ ] `delivery_execution` **does** check `deleted_at` at every hop (state machine), unlike generic types.
- [ ] Evidence PUT has no middleware byte ceiling by design; `evidence_max_bytes` bounds it while streaming.
- [ ] Internal admin routes stay `include_in_schema=False` and disabled unless `ADMIN_API_KEY_HASH` is set.
- [ ] `resolve_visible`/`resolve_delivery_visible` stay line-for-line twins of the visibility probes.

**Before you add a guard, limit, or allow-list:**
- [ ] Pin it with a test that enumerates `app.routes`, not a hardcoded string.
- [ ] If it is a rejection reason, use a stable code, not `type(exc).__name__` (`VITR-V006`).

## Tracking

No in-repo tracker exists. Work lives in three open GitHub issues:
- **#28** — the real 0.3.0 spec (~30 boxes, most already shipped but unchecked). Reconciled
  state is maintained in `trackers/vitrial-api-TRACKER.md`.
- **#30** — MinIO→RustFS, landed; box still open.
- **#2** — 0.2.0 hardening, effectively complete.

Defects found during review are registered as `VITR-V001`…`VITR-V006` in the tracker, each with
severity, repro, and status. Add new verified ones there, not in the skill.
