#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-}"
if [[ -z "$ENV_FILE" || ! -f "$ENV_FILE" ]]; then
  echo "usage: $0 /absolute/path/to/vitrial.env" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$ROOT/deploy/compose.production.yml"

python "$ROOT/scripts/validate_deployment.py" --env-file "$ENV_FILE" --mode production

echo "==> running read-only provider preflight against the pinned API image"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" \
  run --rm --no-deps api python scripts/probe_production_preflight.py

echo "provider preflight passed (read-only checks only; backup/PITR and S3 restore durability remain separate gates)"
