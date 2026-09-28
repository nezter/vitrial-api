#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
from collections.abc import AsyncIterator
from uuid import uuid4

from app.storage import S3ObjectStore


PROBE_PREFIX = "release-probes/"
DEFAULT_SIZE_BYTES = 6 * 1024 * 1024
MAX_SIZE_BYTES = 64 * 1024 * 1024


def _probe_key(value: str) -> str:
    if not value.startswith(PROBE_PREFIX):
        raise ValueError(f"probe key must start with {PROBE_PREFIX}")
    if len(value) <= len(PROBE_PREFIX):
        raise ValueError("probe key suffix is required")
    return value


def _digest(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    return normalized


async def _chunks(data: bytes, size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


async def _read_verified(store: S3ObjectStore, key: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    async for chunk in store.stream(key):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


async def write_probe(size_bytes: int) -> dict[str, object]:
    if size_bytes < 1 or size_bytes > MAX_SIZE_BYTES:
        raise ValueError(f"size-bytes must be between 1 and {MAX_SIZE_BYTES}")

    store = S3ObjectStore()
    key = f"{PROBE_PREFIX}{uuid4().hex}"
    payload = secrets.token_bytes(size_bytes)
    expected = hashlib.sha256(payload).hexdigest()

    stored = await store.put_verified(key, _chunks(payload), expected)
    actual, actual_size = await _read_verified(store, key)
    if actual != expected or actual_size != size_bytes:
        raise RuntimeError("written durability probe did not round-trip exactly")

    return {
        "status": "written",
        "key": key,
        "sha256": expected,
        "sizeBytes": stored.size_bytes,
    }


async def verify_probe(key: str, expected_sha256: str) -> dict[str, object]:
    store = S3ObjectStore()
    key = _probe_key(key)
    expected = _digest(expected_sha256)

    if not await store.exists(key):
        raise FileNotFoundError(key)

    actual, size = await _read_verified(store, key)
    if actual != expected:
        raise RuntimeError("durability probe digest mismatch")

    return {
        "status": "verified",
        "key": key,
        "sha256": actual,
        "sizeBytes": size,
    }


async def cleanup_probe(key: str) -> dict[str, object]:
    store = S3ObjectStore()
    key = _probe_key(key)
    await store.delete(key)
    if await store.exists(key):
        raise RuntimeError("durability probe cleanup did not remove the object")
    return {"status": "deleted", "key": key}


async def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "write":
        return await write_probe(args.size_bytes)
    if args.command == "verify":
        return await verify_probe(args.key, args.sha256)
    if args.command == "cleanup":
        return await cleanup_probe(args.key)
    raise ValueError(f"unsupported command: {args.command}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create and verify a disposable object through Vitrial's real S3ObjectStore "
            "to capture staging/production durability evidence."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    write = subparsers.add_parser("write")
    write.add_argument("--size-bytes", type=int, default=DEFAULT_SIZE_BYTES)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--key", required=True)
    verify.add_argument("--sha256", required=True)

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--key", required=True)

    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "errorType": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
