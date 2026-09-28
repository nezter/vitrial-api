from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_deployment.py"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"


def run_validator(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--env-file", str(path), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def production_values() -> dict[str, str]:
    return {
        "API_HOST": "api.vitrial.invalid",
        "API_IMAGE": "ghcr.io/vitrial/api@sha256:" + "a" * 64,
        "CADDY_IMAGE": "caddy:2-alpine",
        "SERVICE_VERSION": "0.2.0",
        "DATABASE_URL": "postgresql+asyncpg://vitrial:strong-db-secret@db.internal.invalid:5432/vitrial",
        "S3_ENDPOINT_URL": "https://objects.internal.invalid",
        "S3_ACCESS_KEY_ID": "access-key-1234567890",
        "S3_SECRET_ACCESS_KEY": "secret-key-1234567890",
        "S3_BUCKET": "vitrial-evidence",
        "S3_REGION": "us-east-1",
        "ADMIN_API_KEY_HASH": "b" * 64,
    }


def test_production_validator_accepts_external_persistent_services_without_echoing_secrets(tmp_path: Path):
    path = tmp_path / "prod.env"
    values = production_values()
    write_env(path, values)
    result = run_validator(path, "--mode", "production")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["validated"] is True
    assert payload["databaseHost"] == "db.internal.invalid"
    assert values["S3_SECRET_ACCESS_KEY"] not in result.stdout
    assert "strong-db-secret" not in result.stdout


def test_production_validator_rejects_local_persistence_mutable_api_and_insecure_s3(tmp_path: Path):
    path = tmp_path / "bad.env"
    values = production_values()
    values.update({
        "API_IMAGE": "vitrial-api:latest",
        "DATABASE_URL": "postgresql+asyncpg://vitrial:strong-db-secret@localhost:5432/vitrial",
        "S3_ENDPOINT_URL": "http://localhost:9000",
    })
    write_env(path, values)
    result = run_validator(path, "--mode", "production")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    joined = " | ".join(payload["errors"])
    assert "sha256" in joined
    assert "external persistent PostgreSQL" in joined
    assert "https://" in joined


def test_acceptance_validator_requires_private_postgres_and_bootstrap_authority(tmp_path: Path):
    path = tmp_path / "acceptance.env"
    values = production_values()
    values.update({
        "API_HOST": "localhost",
        "DATABASE_URL": "postgresql+asyncpg://vitrial:acceptance-db-secret@postgres:5432/vitrial",
        "POSTGRES_IMAGE": "postgres:17",
        "POSTGRES_PASSWORD": "acceptance-db-secret-12345",
        "S3_IMAGE": "rustfs/rustfs:1.0.0",
        "S3_ROOT_USER": "acceptance-access-12345",
        "S3_ROOT_PASSWORD": "acceptance-secret-12345",
    })
    write_env(path, values)
    result = run_validator(path, "--mode", "acceptance", "--allow-localhost")
    assert result.returncode == 0, result.stdout + result.stderr

    values["ADMIN_API_KEY_HASH"] = ""
    write_env(path, values)
    rejected = run_validator(path, "--mode", "acceptance", "--allow-localhost")
    assert rejected.returncode == 1
    assert "ADMIN_API_KEY_HASH is required" in rejected.stdout


def test_validator_rejects_group_readable_secret_file(tmp_path: Path):
    if os.name == "nt":
        return
    path = tmp_path / "prod.env"
    write_env(path, production_values())
    path.chmod(0o640)
    result = run_validator(path, "--mode", "production")
    assert result.returncode == 1
    assert "chmod 600" in result.stdout


def test_deploy_script_is_syntax_valid_and_verifies_runtime_release_identity():
    syntax = subprocess.run(
        ["bash", "-n", str(DEPLOY_SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "docker inspect --format '{{.Config.Image}}'" in script
    assert '[[ "$RUNNING_API_IMAGE" != "$API_IMAGE" ]]' in script
    assert '"https://${API_HOST}/health"' in script
    assert '"https://${API_HOST}/ready"' in script
    assert 'payload.get("status") != "ready"' in script
    assert 'payload.get("database") != "ok"' in script
    assert 'payload.get("objectStorage") != "ok"' in script
    assert '"https://${API_HOST}/api/v1/version"' in script
    assert 'payload.get("serviceVersion") != expected' in script
