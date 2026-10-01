# Deployment

This directory separates the real production topology from the single-host acceptance topology.

## Immutable application image

Every push to `main` runs `.github/workflows/release-image.yml`. It builds that exact Git SHA, publishes it to GitHub Container Registry as `ghcr.io/scottjoyner/vitrial-api:sha-<git-sha>`, resolves the registry digest, and saves a `release-image-<git-sha>` evidence artifact containing the digest-qualified deployment reference.

Always set `API_IMAGE` to the emitted `ghcr.io/scottjoyner/vitrial-api@sha256:...` reference. The SHA tag is useful for discovery, but the digest is deployment authority. If the GHCR package is not publicly readable, authenticate the deployment host to `ghcr.io` before running Compose.

## Production topology

`compose.production.yml` runs only the Vitrial API, a one-shot Alembic migration job, and Caddy. PostgreSQL and S3-compatible object storage are intentionally external persistent services. This prevents a convenient single-host Docker volume from being mistaken for production durability.

Required host preparation:

1. Provision persistent PostgreSQL 17-compatible storage with backups/PITR appropriate to the environment.
2. Provision persistent S3-compatible object storage and a dedicated API credential scoped to the Vitrial evidence bucket.
3. Point the API hostname at the deployment host and allow inbound TCP 80/443 to Caddy only.
4. Copy `env.production.example` to a host-only path such as `/etc/vitrial/vitrial.env`, populate it, and `chmod 600` it.
5. Set `API_IMAGE` to the immutable GHCR digest emitted for the exact backend Git SHA.
6. Store the raw admin bootstrap key separately; only its SHA-256 digest belongs in `ADMIN_API_KEY_HASH`.
7. Validate configuration before any migration:

```bash
python scripts/validate_deployment.py --env-file /etc/vitrial/vitrial.env --mode production
```

Deploy with:

```bash
scripts/deploy.sh /etc/vitrial/vitrial.env
```

The deploy command validates configuration, pulls images, runs Alembic as a one-shot job, replaces the API, starts Caddy, verifies the exact running image identity, probes PostgreSQL/object storage from inside the API container, and then requires all three public HTTPS gates to pass:

- `/health` proves the API process is alive behind the trusted TLS edge;
- `/ready` proves PostgreSQL and evidence storage are both currently usable and that the reported service version matches the requested release;
- `/api/v1/version` proves the public V1 service identity matches the requested release.

A deployment is **not** considered successful when `/health` passes but `/ready` reports degraded dependencies.

## Rollback

Keep the previous known-good env file with its prior immutable `API_IMAGE` digest. Application rollback is:

```bash
scripts/rollback.sh /etc/vitrial/history/vitrial-previous.env
```

Rollback intentionally **does not automatically downgrade the database**. Database restoration/downgrade is a separate destructive operator action and must follow the backup policy of the actual PostgreSQL provider.

## Single-host acceptance topology

`compose.acceptance.yml` exists to unblock physical two-user/two-device acceptance. It adds PostgreSQL and S3-compatible object storage (RustFS) on private Docker
networking with named persistent volumes. Neither data service publishes a host port. Caddy remains the only public edge.

This topology is suitable for an acceptance environment or temporary staging host. It is not equivalent to managed production persistence because database/object data share the fate of one machine.

For local/CI smoke tests `Caddyfile.smoke` serves `https://localhost` using Caddy's internal CA. Real device acceptance should use the normal `Caddyfile` and a publicly resolvable hostname so Caddy obtains a trusted certificate automatically.

## Secret handling

The repository intentionally contains no populated deployment env file. Runtime secret material must remain outside Git and restricted to the deployment operator. The validator rejects group/world-readable env files and known placeholder/default credentials. Structured application logs do not intentionally emit credentials or raw request bodies.

## Evidence object garbage collection

The API *enqueues* superseded evidence blobs for deletion, but something separate has
to *delete* them. Until 2026-09-30 nothing did: `queue_blob_gc` was called from the
request path, while `collect_due_evidence_gc` was only ever called from
`scripts/gc_evidence.py` and from a test. `app/main.py` has no lifespan task or
scheduler, and no GitHub workflow has a `schedule:` trigger, so rows accumulated in
`evidence_object_gc` indefinitely and the S3 objects they named were never removed.
`EVIDENCE_GC_GRACE_SECONDS` was documented and honoured by the collector, but nothing
ever invoked the collector.

Schedule it on the deployment host:

```bash
sudo cp deploy/gc-evidence.service deploy/gc-evidence.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gc-evidence.timer
```

Edit `WorkingDirectory` and `VITRIAL_ENV_FILE` in the unit to match this host, then:

```bash
systemd-analyze verify /etc/systemd/system/gc-evidence.service
systemctl list-timers gc-evidence.timer
sudo systemctl start gc-evidence.service   # one run, now
journalctl -u gc-evidence.service -n 50
```

The timer runs daily at 03:17 rather than 03:00. Every scheduled job in the world
fires on the hour, and a job that talks to production object storage and a production
database has no reason to be in that pile. `Persistent=true` means a host that was down
at 03:17 collects on the next boot instead of silently skipping a day.

The runner is a **dry run by default**. A human running it by hand has to pass
`--execute` to delete anything; the timer passes it explicitly, so scheduled
collection is unaffected.

```bash
scripts/run_evidence_gc.sh /etc/vitrial/vitrial.env              # reports only
scripts/run_evidence_gc.sh /etc/vitrial/vitrial.env --execute    # deletes
```

The runner mirrors `scripts/deploy.sh`: it validates the env file first, then makes a
one-shot `run --rm api` against the same pinned image and environment as the live
service. The collector skips any object still referenced, honours `not_before` as a
grace period, and takes `FOR UPDATE SKIP LOCKED` so two overlapping runs cannot delete
the same object. It exits non-zero if any individual deletion failed, so a run that
silently under-deletes is visible in `systemctl status` rather than looking healthy.

Add the unit's result to the release evidence list below once it is installed.


## Read-only production provider preflight

Before running a production migration, validate the exact provider boundary without
changing provider state:

```bash
scripts/run_production_preflight.sh /etc/vitrial/vitrial.env
```

The runner validates the production env file, starts a one-shot container from the
digest-pinned `API_IMAGE` with `--no-deps`, and performs only read operations. It
checks:

- the provider PostgreSQL revision equals the repository Alembic head;
- the configured single-process pool budget is below provider `max_connections`;
- no `delivery_execution` generic payload exists without server-owned canonical
  ownership, and no ownership row exists without its payload;
- the configured evidence bucket is reachable with the application's S3 credential.

The output intentionally includes `"durabilityClaimed": false`. A green preflight
does **not** prove PostgreSQL backups/PITR, S3 versioning, retention, replication, or
restore behaviour. Those remain provider/operator evidence and must be retained
separately before promotion.

## Backup and release evidence

Before a real production migration, capture a provider-level PostgreSQL backup/snapshot and verify object-storage durability/versioning policy. Preserve the following release evidence:

- exact backend Git SHA;
- immutable `API_IMAGE` digest and `release-image-<git-sha>` workflow artifact;
- deployment configuration validation output (which contains no secret values);
- Alembic current revision after migration;
- dependency probe output;
- public HTTPS `/health`, `/ready`, and `/api/v1/version` results;
- two-user/two-physical-device acceptance evidence from the pinned iOS client.
