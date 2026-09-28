"""The acceptance compose's evidence-bucket init step.

This is the replacement for `mc mb --ignore-existing`, which came from the
`minio/mc` image. MinIO's prebuilt images are no longer published, so the step
runs in the API image against boto3 instead. The behaviour that matters is
idempotency: the compose stack is restarted during the acceptance run, and the
init step runs again every time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ensure_evidence_bucket import ensure_bucket  # noqa: E402


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "CreateBucket")


class RecordingClient:
    def __init__(self, *errors: ClientError) -> None:
        self.errors = list(errors)
        self.calls: list[str] = []

    def create_bucket(self, Bucket: str) -> None:  # noqa: N803 - boto3's spelling
        self.calls.append(Bucket)
        if self.errors:
            raise self.errors.pop(0)


def test_bucket_is_created_when_absent():
    client = RecordingClient()
    assert ensure_bucket(client, "vitrial-evidence") == "created"
    assert client.calls == ["vitrial-evidence"]


def test_recreate_is_idempotent_for_our_own_bucket():
    # This is the `mc mb --ignore-existing` behaviour the step replaced. The
    # acceptance stack restarts, so the init step runs more than once.
    client = RecordingClient(client_error("BucketAlreadyOwnedByYou"))
    assert ensure_bucket(client, "vitrial-evidence") == "present"


def test_a_bucket_owned_by_another_account_is_not_silently_accepted():
    # BucketAlreadyExists is the global-namespace form: somebody else holds that
    # name. Treating it as success would point evidence writes at a bucket this
    # deployment does not own, so it has to fail.
    client = RecordingClient(client_error("BucketAlreadyExists"))
    with pytest.raises(SystemExit, match="owned by another account"):
        ensure_bucket(client, "vitrial-evidence")


def test_any_other_error_propagates():
    # A credential or connectivity problem must surface, not be folded into
    # "the bucket is fine".
    client = RecordingClient(client_error("AccessDenied"))
    with pytest.raises(ClientError):
        ensure_bucket(client, "vitrial-evidence")
