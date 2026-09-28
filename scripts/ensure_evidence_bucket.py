#!/usr/bin/env python3
"""Create the evidence bucket if it does not already exist.

Replaces the acceptance compose's `mc mb --ignore-existing` step. The `minio/mc`
image that provided `mc` is no longer published anywhere, so bucket creation now
runs against the API image, which already carries boto3. That removes one image
from the set that has to remain pullable and makes the step testable.

Idempotent, matching the `mc mb --ignore-existing` behaviour it replaces: an
existing bucket owned by these credentials is a success, not a failure.
"""
from __future__ import annotations

import os
import sys

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

# S3 returns these when the bucket already exists. `BucketAlreadyOwnedByYou` is
# what a re-run against the same account gets; `BucketAlreadyExists` is the
# global-namespace form, which is what a different-account collision returns and
# which must NOT be treated as success -- someone else owns that name.
ALREADY_PRESENT = ("BucketAlreadyOwnedByYou",)


def ensure_bucket(client, bucket: str) -> str:
    """Create `bucket` if absent. Returns a short word describing what happened."""
    try:
        client.create_bucket(Bucket=bucket)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in ALREADY_PRESENT:
            return "present"
        if code == "BucketAlreadyExists":
            raise SystemExit(
                f"bucket {bucket!r} already exists and is owned by another account"
            ) from error
        raise
    return "created"


def build_client():
    endpoint = os.environ.get("S3_ENDPOINT_URL")
    if not endpoint:
        raise SystemExit("S3_ENDPOINT_URL is required")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        # Path-style addressing: the compose service is a single host, so
        # virtual-host bucket addressing would resolve to an unresolvable name.
        config=Config(s3={"addressing_style": "path"}),
    )


def main() -> int:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise SystemExit("S3_BUCKET is required")
    outcome = ensure_bucket(build_client(), bucket)
    print(f"evidence bucket {bucket}: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
