#!/usr/bin/env bash
# Run the evidence object garbage collector against the deployed environment.
#
# Why this exists: the application *enqueues* evidence blobs for deletion
# (`app.evidence_gc.queue_blob_gc` is called from `app/evidence.py` and
# `app/sync_service.py`), but the thing that actually deletes them
# (`collect_due_evidence_gc`) was only ever called from this CLI and from a test.
# `app/main.py` has no lifespan task and no scheduler, and no GitHub workflow has
# a `schedule:` trigger, so in production rows accumulated in `evidence_object_gc`
# and the S3 objects they name were never removed. The retention behaviour that
# `EVIDENCE_GC_GRACE_SECONDS` implies did not exist.
#
# This is the missing half. It is a one-shot compose run, mirroring how
# `scripts/deploy.sh` runs the `migrate` job, so the container gets the same
# pinned API image and the same environment as the live service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$ROOT/deploy/compose.production.yml"

usage() {
  cat >&2 <<'USAGE'
usage: run_evidence_gc.sh /absolute/path/to/vitrial.env [--dry-run]

  --dry-run   report what would be deleted and change nothing.
              This is the default for this script; pass --execute to delete.
USAGE
}

ENV_FILE=""
# Default is a dry run. The systemd unit passes --execute explicitly, so the
# scheduled collection is unaffected; this only governs a human running it by
# hand, who should have to opt in to deleting production objects.
MODE=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE="" ;;
    --execute) MODE="--execute" ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; usage; exit 2 ;;
    *) ENV_FILE="$arg" ;;
  esac
done

if [[ -z "$ENV_FILE" || ! -f "$ENV_FILE" ]]; then
  usage
  exit 2
fi

# The collector skips rows whose object is still referenced, honours the grace
# period in not_before, and takes FOR UPDATE SKIP LOCKED so overlapping runs are
# safe. It still exits non-zero if any single deletion failed, so a scheduled run
# that silently under-deletes cannot pass unnoticed.
python "$ROOT/scripts/validate_deployment.py" --env-file "$ENV_FILE" --mode production

# shellcheck disable=SC2086
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" run --rm api \
  python scripts/gc_evidence.py --limit "${GC_LIMIT:-500}" ${MODE}
