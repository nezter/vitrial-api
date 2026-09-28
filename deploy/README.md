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

`compose.acceptance.yml` exists to validate application topology and the S3 protocol contract without pretending CI storage is production durability. It runs PostgreSQL on a named persistent volume plus an **ephemeral S3 test service** on private Docker networking. Neither data service publishes a host port. Caddy remains the only public edge.

The S3 test service is deliberately recreated from configuration and initialized through boto3. Its purpose is to prove the API operations Vitrial uses (bucket access, multipart upload, copy/head/get/delete and recovery behavior), not object durability. The acceptance restart check proves PostgreSQL persistence and that the API recovers after the S3 dependency restarts and the test bucket is recreated.

Real staging/production promotion must separately prove the actual S3 provider's persistence/versioning/retention/restore behavior. Do not cite the CI emulator as durability evidence.

For local/CI smoke tests `Caddyfile.smoke` serves `https://localhost` using Caddy's internal CA. Real device acceptance should use the normal `Caddyfile` and a publicly resolvable hostname so Caddy obtains a trusted certificate automatically.

## Real object-storage durability promotion gate

The CI S3 service is intentionally ephemeral. Before promoting a release that depends on evidence storage, run a two-phase probe against the actual staging or production-equivalent S3 provider. The probe uses Vitrial's `S3ObjectStore` directly and writes a disposable 6 MiB object by default, which exercises multipart upload, verified publication, HEAD/GET, and digest validation without using application evidence records.

Run the write phase inside the deployed API container so it uses the exact release image and storage configuration:

```bash
COMPOSE="docker compose --env-file /etc/vitrial/vitrial.env -f deploy/compose.production.yml"
$COMPOSE exec -T api python scripts/probe_s3_durability.py write
```

Record the returned `key`, `sha256`, and `sizeBytes` as release evidence. Then cross a real durability boundary without recreating the bucket: redeploy or restart the API; for self-hosted object storage also restart the storage service or node according to its normal operating procedure. Managed providers do not require an artificial provider restart, but the verification must occur after the application redeploy and outside the original process lifetime.

Verify the exact same object afterward:

```bash
$COMPOSE exec -T api python scripts/probe_s3_durability.py verify \
  --key 'release-probes/REPLACE_ME' \
  --sha256 'REPLACE_WITH_RECORDED_SHA256'
```

A passing verify phase proves that the exact bytes published by the Vitrial storage implementation remained retrievable across the selected staging/production boundary. It does **not** by itself prove provider backup, retention, object-lock, or version-recovery policy. Capture those provider-level settings separately and perform at least one disposable restore/version-recovery drill before calling production durability proven.

After preserving the evidence, remove only the disposable release probe:

```bash
$COMPOSE exec -T api python scripts/probe_s3_durability.py cleanup \
  --key 'release-probes/REPLACE_ME'
```

The cleanup command refuses to delete keys outside the dedicated `release-probes/` prefix.

## Secret handling

The repository intentionally contains no populated deployment env file. Runtime secret material must remain outside Git and restricted to the deployment operator. The validator rejects group/world-readable env files and known placeholder/default credentials. Structured application logs do not intentionally emit credentials or raw request bodies.

## Backup and release evidence

Before a real production migration, capture a provider-level PostgreSQL backup/snapshot and verify object-storage durability/versioning policy. Preserve the following release evidence:

- exact backend Git SHA;
- immutable `API_IMAGE` digest and `release-image-<git-sha>` workflow artifact;
- deployment configuration validation output (which contains no secret values);
- Alembic current revision after migration;
- dependency probe output;
- public HTTPS `/health`, `/ready`, and `/api/v1/version` results;
- two-user/two-physical-device acceptance evidence from the pinned iOS client.
