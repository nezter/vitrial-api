"""Tests for the evidence-GC runner's argument handling and safety defaults.

The runner is a thin shell script, but two of its behaviours are load-bearing and
easy to regress:

- the default is a dry run, so an operator who reaches for it by hand cannot delete
  production objects by forgetting a flag;
- a missing or unknown argument fails with a usage message and a non-zero status,
  rather than composing a container run against the wrong environment.

The compose invocation itself is asserted too, by putting stub `docker` and `python`
on PATH and reading back what the script asked for. That is the part most likely to
drift from `scripts/deploy.sh`, which is the pattern it has to match.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "run_evidence_gc.sh"
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose.production.yml"


def _stub_dir(tmp_path: Path, log: Path) -> Path:
    """A PATH directory whose `docker` and `python` record their argv and succeed."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("docker", "python"):
        target = stubs / name
        target.write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" "{name}" "$*" >> "{log}"\n'
            "exit 0\n"
        )
        target.chmod(0o755)
    return stubs


def _run(args: list[str], *, env_file: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("GC_LIMIT", None)
    return subprocess.run(
        ["bash", str(RUNNER), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _run_with_stubs(
    tmp_path: Path, args: list[str], extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    log = tmp_path / "invocations.log"
    env_file = tmp_path / "vitrial.env"
    env_file.write_text("API_HOST=example.invalid\n")
    stubs = _stub_dir(tmp_path, log)
    environment = dict(os.environ)
    environment["PATH"] = f"{stubs}:{environment['PATH']}"
    environment.pop("GC_LIMIT", None)
    if extra_env:
        environment.update(extra_env)
    result = subprocess.run(
        ["bash", str(RUNNER), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    return result, log.read_text().splitlines() if log.exists() else []


def test_runner_exists_and_is_executable() -> None:
    assert RUNNER.is_file(), f"{RUNNER} is missing"
    assert os.access(RUNNER, os.X_OK), f"{RUNNER} is not executable"


def test_missing_env_file_fails_with_usage() -> None:
    result = _run([])
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_nonexistent_env_file_fails_with_usage() -> None:
    result = _run(["/nonexistent/vitrial.env"])
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_unknown_option_is_rejected_rather_than_ignored() -> None:
    result = _run(["--wat", "/tmp/whatever.env"])
    assert result.returncode == 2
    assert "unknown option" in result.stderr


def test_help_exits_zero() -> None:
    result = _run(["--help"])
    assert result.returncode == 0
    assert "usage:" in result.stderr


def test_dry_run_omits_execute(tmp_path: Path) -> None:
    result, calls = _run_with_stubs(tmp_path, [str(tmp_path / "vitrial.env"), "--dry-run"])
    assert result.returncode == 0, result.stderr
    compose = next(line for line in calls if line.startswith("docker "))
    assert "--execute" not in compose, "a dry run must not be able to delete anything"
    assert "gc_evidence.py" in compose


def test_execute_is_forwarded(tmp_path: Path) -> None:
    _, calls = _run_with_stubs(tmp_path, [str(tmp_path / "vitrial.env"), "--execute"])
    compose = next(line for line in calls if line.startswith("docker "))
    assert "--execute" in compose


def test_the_default_is_a_dry_run(tmp_path: Path) -> None:
    """The most important single assertion here.

    `scripts/gc_evidence.py` itself defaults to a dry run, and this runner must not
    quietly opt out of that. An operator reaching for the script by hand should not
    be able to delete production objects by forgetting a flag.
    """
    _, calls = _run_with_stubs(tmp_path, [str(tmp_path / "vitrial.env")])
    compose = next(line for line in calls if line.startswith("docker "))
    assert "--execute" not in compose, "the runner must not default to deleting"


def test_compose_invocation_matches_the_deploy_script(tmp_path: Path) -> None:
    """The runner has to mirror scripts/deploy.sh, or it silently uses a different env.

    `deploy.sh` builds `docker compose --env-file <file> -f deploy/compose.production.yml`
    and then runs one-shot jobs with `run --rm`. If the GC runner drifts from that, it
    can validate one environment and operate on another.
    """
    _, calls = _run_with_stubs(tmp_path, [str(tmp_path / "vitrial.env")])
    compose = next(line for line in calls if line.startswith("docker "))
    assert f"--env-file {tmp_path / 'vitrial.env'}" in compose
    assert f"-f {COMPOSE_FILE}" in compose
    assert "run --rm api" in compose
    assert "compose.production.yml" in compose


def test_environment_is_validated_before_anything_runs(tmp_path: Path) -> None:
    """A dry run against a misconfigured environment is still a wrong answer."""
    _, calls = _run_with_stubs(tmp_path, [str(tmp_path / "vitrial.env")])
    assert calls, "nothing was invoked"
    assert calls[0].startswith("python "), "validate_deployment.py must run first"
    assert "--mode production" in calls[0]


def test_gc_limit_is_configurable(tmp_path: Path) -> None:
    _, calls = _run_with_stubs(
        tmp_path, [str(tmp_path / "vitrial.env")], extra_env={"GC_LIMIT": "7"}
    )
    compose = next(line for line in calls if line.startswith("docker "))
    assert "--limit 7" in compose


def test_default_limit_is_bounded(tmp_path: Path) -> None:
    _, calls = _run_with_stubs(tmp_path, [str(tmp_path / "vitrial.env")])
    compose = next(line for line in calls if line.startswith("docker "))
    assert "--limit 500" in compose


def test_the_service_unit_is_wellformed() -> None:
    path = REPO_ROOT / "deploy" / "gc-evidence.service"
    assert path.is_file(), f"{path} is missing"
    text = path.read_text()
    # A .service needs Unit and Service. It deliberately has no [Install]: it is
    # started by the timer, not enabled on its own.
    assert "[Unit]" in text
    assert "[Service]" in text
    assert "Type=oneshot" in text


def test_the_timer_unit_is_wellformed() -> None:
    path = REPO_ROOT / "deploy" / "gc-evidence.timer"
    assert path.is_file(), f"{path} is missing"
    text = path.read_text()
    assert "[Unit]" in text
    assert "[Timer]" in text
    assert "[Install]" in text
    assert "WantedBy=timers.target" in text


def test_the_timer_is_daily_and_catches_up_after_downtime() -> None:
    text = (REPO_ROOT / "deploy" / "gc-evidence.timer").read_text()
    assert "OnCalendar=" in text
    assert "Persistent=true" in text, "a host that was down must still collect"


def test_the_service_runs_the_collection_and_can_fail_loudly() -> None:
    text = (REPO_ROOT / "deploy" / "gc-evidence.service").read_text()
    assert "run_evidence_gc.sh" in text
    assert "Type=oneshot" in text
