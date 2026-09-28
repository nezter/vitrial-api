"""The acceptance topology's durability claims must be real claims.

The topology used to assert two things survived a container restart:

1. a row written through Postgres — genuine, it goes through the database;
2. a marker file written straight into the storage container's `/data` volume —
   not genuine. That proved a Docker named volume persists, which Docker
   guarantees, and said nothing about the S3 contract the application actually
   depends on. A test asserting that would keep passing while the real property
   was never checked.

These pin the replacement: durability is now proven by writing and reading back
an object through the application's own S3 client.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
COMPOSE = ROOT / "deploy" / "compose.acceptance.yml"

VOLUME_MARKER = "/data/.vitrial-deployment-smoke"


def test_the_volume_marker_assertion_is_gone_from_the_workflow():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert VOLUME_MARKER not in text, (
        "the /data volume marker proves Docker volume persistence, not S3 "
        "durability; use scripts/probe_evidence_durability.py instead"
    )


def test_the_volume_marker_is_gone_from_the_compose_file():
    assert VOLUME_MARKER not in COMPOSE.read_text(encoding="utf-8")


def test_durability_is_proven_through_the_s3_client():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/probe_evidence_durability.py write" in text
    assert "scripts/probe_evidence_durability.py verify" in text
    # The write has to happen before the restart and the verify after it,
    # otherwise the pair proves nothing about surviving a restart.
    assert text.index("durability.py write") < text.index("$COMPOSE restart")
    assert text.index("$COMPOSE restart") < text.index("durability.py verify")


def test_database_durability_is_still_asserted():
    # The Postgres half of the original step was a real claim and must survive.
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS deployment_smoke" in text
    assert "SELECT count(*) FROM deployment_smoke" in text


def test_the_probe_verifies_content_not_just_presence():
    # `head_object` would pass on a truncated or zero-length object. Comparing
    # the sha256 of the bytes is what makes this a durability check.
    probe = (ROOT / "scripts" / "probe_evidence_durability.py").read_text(
        encoding="utf-8"
    )
    assert "hashlib.sha256" in probe
    assert "get_object" in probe
    assert "head_object" not in probe
