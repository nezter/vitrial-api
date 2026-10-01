from __future__ import annotations

import tomllib
from pathlib import Path

from app.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RELEASE_VERSION = "0.3.0"


def test_release_version_metadata_is_consistent():
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert package["project"]["version"] == EXPECTED_RELEASE_VERSION
    assert Settings.model_fields["service_version"].default == EXPECTED_RELEASE_VERSION

    development_env = (ROOT / ".env.example").read_text(encoding="utf-8")
    production_env = (ROOT / "deploy" / "env.production.example").read_text(encoding="utf-8")
    ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert f"SERVICE_VERSION={EXPECTED_RELEASE_VERSION}" in development_env
    assert f"SERVICE_VERSION={EXPECTED_RELEASE_VERSION}" in production_env
    assert f"SERVICE_VERSION={EXPECTED_RELEASE_VERSION}-ci" in ci_workflow
