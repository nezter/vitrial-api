"""The production dependency surface, and why it is exactly this surface.

`uvicorn[standard]` is a development convenience: it pulls httptools,
python-dotenv, pyyaml, uvloop, watchfiles and websockets in one hop. Two of
those have no business in a production API image:

- **watchfiles** is a development file-watching tool, used for `--reload`.
- **websockets** has no consumer here; the service has no WebSocket route.

Shipping them means every image rebuild pulls packages nothing imports, and
every transitive bump in either of them is a change to the production surface
that no test would otherwise notice.

That is not hypothetical. `Acceptance deployment topology` failed on `main` with

    ERROR: Cannot install uvicorn because these package versions have conflicting
    dependencies
    uvicorn 0.54.0 depends on click>=7.0
    uvicorn 0.53.0 depends on click>=7.0
    ... (every version back to 0.32)
    ResolutionImpossible

after `WARNING: Retrying` lines, and the identical resolution succeeded on
retry and on a local machine. A smaller dependency surface is a smaller blast
radius for that class of failure, which is the second reason this file exists.

These assertions read `pyproject.toml` with `tomllib` rather than importing
anything, so they run with no dependencies installed at all.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def declared_dependencies() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["dependencies"]


def requirement_name(requirement: str) -> str:
    """`uvicorn[standard]>=0.32,<1` -> `uvicorn`."""
    head = requirement.split(";")[0].strip()
    for separator in ("[", ">", "<", "=", "!", "~", " "):
        head = head.split(separator)[0]
    return head.strip().lower()


def test_uvicorn_does_not_request_the_standard_extra():
    offenders = [
        requirement
        for requirement in declared_dependencies()
        if requirement_name(requirement) == "uvicorn" and "[" in requirement
    ]
    assert offenders == [], (
        "uvicorn[standard] pulls watchfiles (a development reloader) and "
        "websockets (unused here) into the production image: "
        f"{offenders}"
    )


def test_no_development_only_package_is_a_production_dependency():
    """These are development tools. None should be a runtime requirement."""
    development_only = {
        "watchfiles",
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "ruff",
        "mypy",
        "black",
        "flake8",
        "ipython",
        "notebook",
    }
    declared = {requirement_name(r) for r in declared_dependencies()}
    assert declared & development_only == set(), (
        f"development-only packages declared as runtime dependencies: "
        f"{sorted(declared & development_only)}"
    )


def test_performance_and_config_packages_are_declared_explicitly():
    """Dropping [standard] means anything still needed must be named.

    `python-dotenv` in particular is load-bearing: `app/settings.py` sets
    `env_file=".env"`, and pydantic-settings raises at construction if the
    package that reads it is absent.
    """
    declared = {requirement_name(r) for r in declared_dependencies()}
    for required in ("uvicorn", "uvloop", "httptools", "python-dotenv"):
        assert required in declared, f"{required} must be declared explicitly"


def test_settings_actually_depends_on_dotenv():
    """Guards the claim above: if this stops being true, drop the dependency."""
    settings = (ROOT / "app" / "settings.py").read_text(encoding="utf-8")
    assert "env_file" in settings, (
        "app/settings.py no longer reads env_file; python-dotenv may be "
        "droppable from the production dependencies"
    )


def test_service_declares_no_websocket_route():
    """The reason websockets is dropped. If this becomes false, add it back."""
    routes: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "websocket" in source.lower():
            routes.append(str(path.relative_to(ROOT)))
    assert routes == [], (
        f"WebSocket usage found in {routes}; the production dependencies must "
        "declare websockets explicitly"
    )


@pytest.mark.parametrize(
    "requirement",
    [
        "fastapi>=0.115,<1",
        "uvicorn>=0.32,<1",
        "sqlalchemy[asyncio]>=2.0.36,<3",
        "asyncpg>=0.30,<1",
        "alembic>=1.14,<2",
        "pydantic-settings>=2.7,<3",
        "python-multipart>=0.0.20,<1",
        "boto3>=1.35,<2",
    ],
)
def test_service_requirements_are_unchanged(requirement: str):
    """The service's own requirements must stay put.

    The `uvicorn[standard]` -> explicit-set change should be the only edit to
    this list; this pins the rest so a well-meaning bump is deliberate.
    """
    assert requirement in declared_dependencies()
