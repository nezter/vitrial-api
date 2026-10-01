from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.probe_production_preflight import Snapshot, evaluate


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_production_preflight.sh"
PROBE = ROOT / "scripts" / "probe_production_preflight.py"
PRODUCTION_COMPOSE = ROOT / "deploy" / "compose.production.yml"
ACCEPTANCE_COMPOSE = ROOT / "deploy" / "compose.acceptance.yml"


def snapshot(**overrides) -> Snapshot:
    values = {
        "database_revision": "0007",
        "expected_revision": "0007",
        "max_connections": 100,
        "current_connections": 7,
        "pool_budget": 15,
        "orphan_payload_rows": 0,
        "orphan_ownership_rows": 0,
        "object_storage": "ok",
    }
    values.update(overrides)
    return Snapshot(**values)


def test_green_snapshot_is_accepted_without_claiming_durability():
    assert evaluate(snapshot()) == []
    source = PROBE.read_text(encoding="utf-8")
    assert '"durabilityClaimed": False' in source


def test_preflight_rejects_migration_drift_and_delivery_half_rows():
    errors = evaluate(snapshot(
        database_revision="0006",
        orphan_payload_rows=1,
        orphan_ownership_rows=2,
    ))
    assert any("migration revision" in error for error in errors)
    assert any("without canonical ownership" in error for error in errors)
    assert any("without payload rows" in error for error in errors)


def test_preflight_rejects_pool_budget_at_provider_ceiling():
    errors = evaluate(snapshot(max_connections=15, pool_budget=15))
    assert any("max_connections" in error for error in errors)


def test_null_pool_has_no_reuse_budget_to_compare():
    assert evaluate(snapshot(max_connections=5, pool_budget=None)) == []


def test_preflight_rejects_unreachable_object_storage():
    errors = evaluate(snapshot(object_storage="unavailable"))
    assert any("object storage" in error for error in errors)


def test_runner_is_shell_valid_and_does_not_run_migrations():
    result = subprocess.run(
        ["bash", "-n", str(RUNNER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    source = RUNNER.read_text(encoding="utf-8")
    assert "--mode production" in source
    assert "run --rm --no-deps api python scripts/probe_production_preflight.py" in source
    assert "alembic upgrade" not in source
    assert "scripts/deploy.sh" not in source


def test_probe_is_read_only_at_the_provider_boundary():
    source = PROBE.read_text(encoding="utf-8").lower()
    for forbidden in (
        "insert into",
        "update ",
        "delete from",
        "put_object",
        "create_bucket",
        "alembic upgrade",
    ):
        assert forbidden not in source


def test_database_tuning_is_forwarded_through_both_compose_topologies():
    keys = (
        "DATABASE_POOL_SIZE",
        "DATABASE_MAX_OVERFLOW",
        "DATABASE_POOL_TIMEOUT_SECONDS",
        "DATABASE_POOL_RECYCLE_SECONDS",
        "DATABASE_STATEMENT_TIMEOUT_MS",
    )
    for path in (PRODUCTION_COMPOSE, ACCEPTANCE_COMPOSE):
        source = path.read_text(encoding="utf-8")
        for key in keys:
            assert f"{key}:" in source
