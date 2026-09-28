#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
PLACEHOLDER_PARTS = ("REPLACE_", "example.com", "changeme", "password", "minioadmin")


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid env line: {line[:32]}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def require(values: dict[str, str], key: str, errors: list[str]) -> str:
    value = values.get(key, "").strip()
    if not value:
        errors.append(f"{key} is required")
    return value


def looks_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(part.lower() in lowered for part in PLACEHOLDER_PARTS)


def host_from_database_url(value: str) -> str | None:
    try:
        parsed = urlparse(value.replace("postgresql+asyncpg://", "postgresql://", 1))
        return parsed.hostname
    except Exception:
        return None


def validate(args: argparse.Namespace) -> tuple[list[str], dict[str, object]]:
    path = Path(args.env_file)
    errors: list[str] = []
    if not path.is_file():
        return ["env file does not exist"], {"mode": args.mode}

    if os.name != "nt" and not args.allow_insecure_permissions:
        permissions = stat.S_IMODE(path.stat().st_mode)
        if permissions & 0o077:
            errors.append("env file must not be group/world readable; chmod 600")

    values = parse_env(path)
    api_host = require(values, "API_HOST", errors)
    api_image = require(values, "API_IMAGE", errors)
    caddy_image = require(values, "CADDY_IMAGE", errors)
    service_version = require(values, "SERVICE_VERSION", errors)
    database_url = require(values, "DATABASE_URL", errors)
    s3_bucket = require(values, "S3_BUCKET", errors)
    s3_access = require(values, "S3_ACCESS_KEY_ID", errors) if args.mode == "production" else values.get("S3_ACCESS_KEY_ID", "")
    s3_secret = require(values, "S3_SECRET_ACCESS_KEY", errors) if args.mode == "production" else values.get("S3_SECRET_ACCESS_KEY", "")

    if api_host and not args.allow_localhost and api_host in {"localhost", "127.0.0.1", "::1"}:
        errors.append("API_HOST must be a routable hostname outside smoke tests")
    if api_host and looks_placeholder(api_host):
        errors.append("API_HOST still contains a placeholder")

    if api_image:
        if looks_placeholder(api_image):
            errors.append("API_IMAGE still contains a placeholder")
        if "@sha256:" not in api_image and not args.allow_mutable_images:
            errors.append("API_IMAGE must be pinned by sha256 digest")
    if caddy_image.endswith(":latest") and not args.allow_mutable_images:
        errors.append("CADDY_IMAGE must not use :latest")

    if database_url and not database_url.startswith("postgresql+asyncpg://"):
        errors.append("DATABASE_URL must use postgresql+asyncpg://")
    db_host = host_from_database_url(database_url) if database_url else None
    if args.mode == "production" and db_host in {None, "localhost", "127.0.0.1", "postgres"}:
        errors.append("production DATABASE_URL must target external persistent PostgreSQL")
    if database_url and looks_placeholder(database_url):
        errors.append("DATABASE_URL still contains a placeholder")

    endpoint = values.get("S3_ENDPOINT_URL", "").strip()
    if args.mode == "production" and endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" and not args.allow_insecure_s3:
            errors.append("production S3_ENDPOINT_URL must use https://")
        if parsed.hostname in {"localhost", "127.0.0.1", "minio", "s3"}:
            errors.append("production S3 endpoint must be external persistent object storage")
    if endpoint and looks_placeholder(endpoint):
        errors.append("S3_ENDPOINT_URL still contains a placeholder")

    for key, value in (("S3_ACCESS_KEY_ID", s3_access), ("S3_SECRET_ACCESS_KEY", s3_secret)):
        if value and looks_placeholder(value):
            errors.append(f"{key} still contains a placeholder/default credential")
        if args.mode == "production" and value and len(value) < 16:
            errors.append(f"{key} is unexpectedly short")

    admin_hash = values.get("ADMIN_API_KEY_HASH", "").strip()
    if admin_hash and not HEX64.fullmatch(admin_hash):
        errors.append("ADMIN_API_KEY_HASH must be a 64-character SHA-256 hex digest")

    if args.mode == "acceptance":
        for key in ("POSTGRES_IMAGE", "S3_IMAGE"):
            image = require(values, key, errors)
            if image.endswith(":latest") and not args.allow_mutable_images:
                errors.append(f"{key} must not use :latest")
        for key in ("POSTGRES_PASSWORD", "S3_ROOT_USER", "S3_ROOT_PASSWORD"):
            secret = require(values, key, errors)
            if secret and (len(secret) < 16 or looks_placeholder(secret)):
                errors.append(f"{key} must be a non-default random value of at least 16 characters")
        if not admin_hash:
            errors.append("ADMIN_API_KEY_HASH is required for acceptance provisioning")
        if db_host not in {"postgres"}:
            errors.append("acceptance DATABASE_URL must target the private postgres service")

    summary = {
        "mode": args.mode,
        "apiHost": api_host or None,
        "databaseHost": db_host,
        "s3BucketConfigured": bool(s3_bucket),
        "adminProvisioningEnabled": bool(admin_hash),
        "serviceVersion": service_version or None,
        "validated": not errors,
    }
    return errors, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed deployment configuration validation")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--mode", choices=("production", "acceptance"), default="production")
    parser.add_argument("--allow-localhost", action="store_true")
    parser.add_argument("--allow-mutable-images", action="store_true")
    parser.add_argument("--allow-insecure-permissions", action="store_true")
    parser.add_argument("--allow-insecure-s3", action="store_true")
    args = parser.parse_args()

    try:
        errors, summary = validate(args)
    except Exception as exc:
        print(json.dumps({"validated": False, "errorType": type(exc).__name__}, sort_keys=True))
        return 2

    if errors:
        summary["errors"] = errors
        print(json.dumps(summary, sort_keys=True))
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
