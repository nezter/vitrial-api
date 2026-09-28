"""The portable contract validator must not drift from the served vocabulary.

`scripts/validate_backend_contract.py` is deliberately dependency-free so it can
run anywhere, which means it hardcodes the capability and entity-type lists
rather than importing them. That is exactly how the delivery-execution gap
survived: the server rejected a record type the client was built to send, and
the portable validator agreed with the server instead of catching it.

These tests are the seam. They run where pydantic *is* available and fail if the
portable lists and the served Literals ever disagree again.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import get_args

from app.schemas import Capability, EntityType

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_backend_contract.py"


def portable_constant(name: str) -> set[str]:
    """Read a hardcoded set literal out of the portable script without importing it."""
    tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return set(ast.literal_eval(node.value))
    raise AssertionError(f"{VALIDATOR.name} no longer defines {name}")


def test_capability_list_matches_the_served_vocabulary():
    assert portable_constant("CAPABILITIES") == set(get_args(Capability))


def test_entity_type_list_matches_the_served_vocabulary():
    assert portable_constant("ENTITY_TYPES") == set(get_args(EntityType))


def test_the_validator_still_runs_against_the_committed_pack():
    # Keep the drift tests honest: if the portable script cannot execute the
    # committed contract, the two tests above would pass while checking nothing.
    import subprocess

    result = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
