#!/usr/bin/env python3
"""Fail CI if a declared guard prefix matches no live route.

This is the enforcement half of `VITR-V001`: the request-size and rate-limit
maps are hand-written, and a typo or rename makes an entry silently protect
nothing. The current test suite asserts against string literals, so it cannot
catch a route that moved. This script enumerates the real FastAPI route table
and asserts every declared prefix is a prefix of at least one live path.
"""

from __future__ import annotations

import importlib
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))


def _live_paths() -> set[str]:
    app_module = importlib.import_module("app.main")
    app = app_module.app
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
    for mod_name, attr in (
        ("app.auth_session_routes", "router"),
        ("app.reference_routes", "router"),
        ("app.sync_v2_routes", "router"),
    ):
        module = importlib.import_module(mod_name)
        for route in getattr(module, attr).routes:
            path = getattr(route, "path", None)
            if path:
                paths.add(path)
    return paths


def main() -> int:
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")

    from app.rate_limit import THROTTLED_PATH_PREFIXES
    from app.request_size import DEFAULT_PATH_LIMITS

    live = _live_paths()
    declared: list[tuple[str, str]] = []
    for prefix, _limit in DEFAULT_PATH_LIMITS:
        declared.append(("request_size", prefix))
    for prefix in THROTTLED_PATH_PREFIXES:
        declared.append(("rate_limit", prefix))

    failures: list[str] = []
    for source, prefix in declared:
        if not any(path.startswith(prefix) for path in live):
            failures.append(f"{source}: declared prefix {prefix!r} matches no live route")

    only_literals = sorted({p for _s, p in declared} - live)

    if failures:
        print("guard-prefix lint FAILED:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        print("\nlive routes are:", file=sys.stderr)
        for path in sorted(live):
            print(f"  {path}", file=sys.stderr)
        return 1

    print(f"guard-prefix lint ok: {len(declared)} declared prefixes, all match a live route")
    print(f"live route table has {len(live)} paths ({sorted(only_literals)} declared-but-not-a-full-route)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
