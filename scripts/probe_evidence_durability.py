#!/usr/bin/env python3
"""Prove the evidence bucket survives a container restart.

The acceptance topology used to write a marker file straight into the storage
container's `/data` volume and read it back after a restart. That proved a
Docker named volume persists, which Docker guarantees — it said nothing about
whether the application's own S3 contract holds, which is the thing the
deployment actually needs to be true.

This goes through the same path the application uses: a real S3 client, the same
credentials and endpoint, a real object. `write` puts a canary; `verify` gets it
back and checks the bytes. Run them either side of a restart and the object
surviving is genuine evidence rather than a volume fact.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

import boto3
from botocore.client import Config

# Fixed payload so `write` and `verify` agree on the expected digest without
# having to pass state between two container invocations.
CANARY_KEY = "deployment-smoke/canary.bin"
CANARY_BODY = b"vitrial-deployment-durability-canary\n"


def expected_digest() -> str:
    return hashlib.sha256(CANARY_BODY).hexdigest()


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
        config=Config(s3={"addressing_style": "path"}),
    )


def write_canary(client, bucket: str) -> None:
    client.put_object(
        Bucket=bucket,
        Key=CANARY_KEY,
        Body=CANARY_BODY,
        # A real deployment should not serve a stale canary.
        CacheControl="no-store",
    )
    print(f"wrote {bucket}/{CANARY_KEY} sha256={expected_digest()}")


def verify_canary(client, bucket: str) -> None:
    try:
        stored = client.get_object(Bucket=bucket, Key=CANARY_KEY)["Body"].read()
    except Exception as error:  # noqa: BLE001 - any failure here is the finding
        raise SystemExit(
            f"evidence canary {bucket}/{CANARY_KEY} did not survive: {error}"
        ) from error
    digest = hashlib.sha256(stored).hexdigest()
    if digest != expected_digest():
        raise SystemExit(
            f"evidence canary digest mismatch: expected {expected_digest()}, got {digest}"
        )
    print(f"verified {bucket}/{CANARY_KEY} sha256={digest}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("write", "verify"))
    parser.add_argument("--quiet", action="store_true", help="emit no success output")
    args = parser.parse_args()

    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise SystemExit("S3_BUCKET is required")
    client = build_client()
    if args.action == "write":
        write_canary(client, bucket)
    else:
        verify_canary(client, bucket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
