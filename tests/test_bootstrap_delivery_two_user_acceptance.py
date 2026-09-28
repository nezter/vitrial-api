from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SCRIPT = SCRIPTS / "bootstrap_delivery_two_user_acceptance.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_delivery_two_user_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_delivery_wrapper_adds_required_operator_capabilities():
    expected = {"quotation.send", "quotation.approve", "delivery.manage"}
    assert expected.issubset(set(module.base.DEFAULT_CAPABILITIES))


def test_delivery_profile_verification_fails_closed_without_delivery_authority():
    profile = {
        "organizationID": "org-1",
        "principalID": "user-1",
        "projectIDs": ["project-1"],
        "allProjects": False,
        "capabilities": [
            "sync",
            "project.create",
            "item.create",
            "quotation.send",
            "quotation.approve",
            "delivery.manage",
        ],
    }
    assert module._delivery_profile_matches(
        profile,
        organization_id="org-1",
        user_id="user-1",
        project_id="project-1",
    )

    for capability in module.DELIVERY_ACCEPTANCE_CAPABILITIES:
        reduced = {
            **profile,
            "capabilities": [
                value for value in profile["capabilities"] if value != capability
            ],
        }
        assert not module._delivery_profile_matches(
            reduced,
            organization_id="org-1",
            user_id="user-1",
            project_id="project-1",
        )


def test_delivery_wrapper_keeps_existing_handoff_entry_point():
    assert callable(module.main)
    assert module.base.main is not module.main
