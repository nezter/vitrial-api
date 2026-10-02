SHELL := /bin/bash

.PHONY: install postgres postgres-native migrate migrate-native test test-integration test-all run check

install:
	python -m pip install -e '.[dev]'

# Container runtime. Convenient, and what CI uses.
postgres:
	docker compose up -d postgres

# Homebrew PostgreSQL, for machines without a container runtime.
#
# The PostgreSQL integration suite is skipped unless POSTGRES_INTEGRATION=1, so
# without a reachable database it is easy to believe the whole suite passes when a
# third of it never ran. That is not hypothetical: the batched pull prefetch (#53)
# shipped three rounds of locally-green, CI-red results because every defect was in
# code only the integration job executes.
postgres-native:
	@command -v pg_ctl >/dev/null 2>&1 || { \
		echo "PostgreSQL not on PATH. Install it first:"; \
		echo "  brew install postgresql@17"; \
		echo "  export PATH=/opt/homebrew/opt/postgresql@17/bin:\$$PATH"; \
		exit 1; }
	@pg_ctl -D /opt/homebrew/var/postgresql@17 -l /tmp/postgresql@17.log start \
		2>/dev/null || echo "(server may already be running)"
	@psql -d postgres -c "CREATE ROLE vitrial LOGIN PASSWORD 'vitrial' SUPERUSER;" 2>/dev/null || true
	@psql -d postgres -c "CREATE DATABASE vitrial OWNER vitrial;" 2>/dev/null || true
	@echo "vitrial/vitrial@localhost:5432/vitrial ready (matches .github/workflows/ci.yml)"

migrate:
	alembic upgrade head

test:
	pytest -q

# The suite CI actually runs, against a real database.
#
# Invoked as `python -m pytest`, not the `pytest` console script, because the latter
# does not put the repository root on sys.path and `tests/test_production_preflight.py`
# imports `scripts.probe_production_preflight`. With the console script that file
# fails to import and collection aborts, which reads as a broken repo rather than a
# broken invocation. CI uses the module form for the same reason.
#
# DATABASE_NULL_POOL=true is not optional. The pooled engine retains connections
# across tests while pytest-asyncio gives each test a fresh event loop, and the
# result is "attached to a different loop" across ~20 tests. CI sets it for the same
# reason; without it this target fails in ways that look like application defects.
test-integration:
	DATABASE_NULL_POOL=true POSTGRES_INTEGRATION=1 \
		$${PYTHON:-$$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python)} -m pytest -q

# Unit plus integration, which is what CI does.
test-all: test-integration

run:
	uvicorn app.main:app --reload

check:
	python -m compileall -q app migrations tests
	python scripts/validate_backend_contract.py
	pytest -q

# UBS rapid bug gate — the same verdict CI enforces, locally, in seconds.
#
# Fails on any NEW critical/warning finding versus the committed baseline
# (ubs-baseline.json); the baseline's residual findings are the documented
# false-positive floor (see trackers/vitrial-api-TRACKER.md "UBS loop status").
# A scan that cannot run is a failure, not a pass — see scripts/ubs_gate.sh.
.PHONY: ubs
ubs:
	bash scripts/ubs_gate.sh ubs-baseline.json
