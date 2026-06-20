# Backup, Restore, And Rollback Runbook

This MVP launch runbook covers manual backups, restore drills, and service/image
rollback for the production-style Docker Compose stack. It uses placeholders
only; do not paste real secret values into tickets, logs, or committed files.

## Scope And Source Of Truth

- PostgreSQL is the authoritative store for users, memes, pipeline state,
  crawler state, scheduler state, and search-sync snapshots.
- S3/MinIO-compatible object storage holds originals, temp originals, and
  derivatives under the configured `S3_BUCKET`.
- Qdrant and Meilisearch are rebuildable indexes. For launch, treat their
  backups as optional convenience; PostgreSQL plus object storage are the
  required recovery sources.
- Service rollback uses previously published immutable image tags, preferably
  `sha-<short-sha>` tags, not mutable branch tags such as `main`.
- Runtime environment variable names come from `memexpert.core.config.Settings`
  and are unprefixed, for example `PIPELINE_OPERATOR_TOKEN`, `QDRANT_URL`,
  `MEILISEARCH_URL`, and `MEILISEARCH_MASTER_KEY`. The compose image-selection
  variables remain `MEMEXPERT_MAIN_IMAGE`, `MEMEXPERT_WORKER_IMAGE`, and
  `MEMEXPERT_FRONTEND_IMAGE`.

## Safety Rules

- Freeze app writes before a destructive restore: stop `frontend`, `api`,
  `workers`, `scheduler`, and `telegram-crawler` first. Stop the optional `bot`
  too if that profile is enabled.
- Take a fresh PostgreSQL and object-storage backup before any production
  rollback or restore, even if the incident is urgent.
- Do not roll back the database just because service images are rolling back.
  Prefer a forward fix unless the failed release corrupted data or the rollback
  target is known to match the database schema.
- If a release applied migrations, check the release notes and Alembic history
  before choosing DB restore. A DB restore loses writes after the backup point
  and must be coordinated with object storage from the same window.
- Never delete canonical object keys manually during rollback. Extra objects are
  usually harmless; missing objects break restored database rows.

## PostgreSQL Backup

Run from the repository root on the host running the production compose stack:

```bash
export ENV_FILE=.env.prod
export COMPOSE_FILE=docker-compose.prod.example.yml
export BACKUP_TS=<timestamp>
mkdir -p ".artifacts/ops-backups/$BACKUP_TS"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' \
  > ".artifacts/ops-backups/$BACKUP_TS/postgres.dump"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres sh -lc \
  'pg_restore --list' \
  < ".artifacts/ops-backups/$BACKUP_TS/postgres.dump" \
  > ".artifacts/ops-backups/$BACKUP_TS/postgres.dump.list"
```

Copy `.artifacts/ops-backups/<timestamp>/` to the approved external backup
location before treating the backup as durable. The local `.artifacts/` tree is
only a staging path and is gitignored.

## Object Storage Backup

Mirror the configured MinIO/S3 bucket into the same backup directory:

```bash
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm \
  -v "$PWD/.artifacts/ops-backups/$BACKUP_TS:/backup" \
  --entrypoint /bin/sh minio-init -lc \
  'mc alias set prod http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && mc mirror --overwrite "prod/$S3_BUCKET" "/backup/s3/$S3_BUCKET"'
```

Object-storage retention guidance:

- Keep point-in-time backups long enough to cover the launch rollback window.
- Configure provider lifecycle rules for temporary originals only after launch
  owners agree on the evidence-retention window.
- Keep canonical originals and derivatives recoverable for at least as long as
  the PostgreSQL backup window. A restored database can reference older object
  keys.
- If your deployment uses managed S3 instead of the bundled MinIO service,
  adapt only the `mc alias set` endpoint/credentials source; keep bucket names
  and prefixes from `S3_BUCKET` and the pipeline `Settings` values.

## Restore PostgreSQL And Objects

This is destructive. Use it for a real restore only after freezing writes and
confirming the selected backup timestamp.

```bash
export ENV_FILE=.env.prod
export COMPOSE_FILE=docker-compose.prod.example.yml
export BACKUP_TS=<timestamp>

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" stop frontend api workers scheduler telegram-crawler

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres sh -lc \
  'dropdb --if-exists -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres sh -lc \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl' \
  < ".artifacts/ops-backups/$BACKUP_TS/postgres.dump"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm \
  -v "$PWD/.artifacts/ops-backups/$BACKUP_TS:/backup:ro" \
  --entrypoint /bin/sh minio-init -lc \
  'mc alias set prod http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && mc mirror --overwrite "/backup/s3/$S3_BUCKET" "prod/$S3_BUCKET"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d migrate api workers scheduler telegram-crawler frontend
```

Add the optional `bot` service to the stop/up commands only when the bot profile
is enabled in that environment.

## Service/Image Rollback

Use this when the database does not need to move backward. The rollback target
must be a previously published immutable tag for all three app images.

```bash
export ENV_FILE=.env.prod
export COMPOSE_FILE=docker-compose.prod.example.yml
export PREVIOUS_SHA_TAG=<previous-sha-tag>
export MEMEXPERT_MAIN_IMAGE="ghcr.io/averyanalex/memexpert/main:$PREVIOUS_SHA_TAG"
export MEMEXPERT_WORKER_IMAGE="ghcr.io/averyanalex/memexpert/worker:$PREVIOUS_SHA_TAG"
export MEMEXPERT_FRONTEND_IMAGE="ghcr.io/averyanalex/memexpert/frontend:$PREVIOUS_SHA_TAG"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --images
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull migrate api workers scheduler telegram-crawler frontend
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --no-build migrate api workers scheduler telegram-crawler frontend
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
```

After the rollback is confirmed, persist the same immutable tags in the
untracked production env file so the next restart does not drift back to a
mutable or bad image tag.

## Migration And DB Rollback Decision

Use this decision flow during an incident:

1. If the bad release did not apply migrations and data is not corrupted, roll
   back service images only.
2. If migrations were applied but are backward-compatible with the previous
   image, roll back service images only and keep the DB forward.
3. If migrations are not backward-compatible, prefer a forward hotfix or a new
   image that can read the current schema.
4. Restore PostgreSQL only when the release corrupted data, the accepted write
   loss is understood, and object storage from the same backup window is
   available.
5. After DB restore, run the normal migration service from the chosen image tag
   and then verify API health, scheduler logs, and search-index smoke checks.

Do not run ad-hoc downgrade SQL in production unless it is reviewed as part of
the incident plan. Alembic downgrade support is not a substitute for a tested
backup restore.

## Qdrant And Meilisearch Stance

Qdrant and Meilisearch contain derived search data rebuilt from PostgreSQL and
object storage. For launch readiness:

- Prefer rebuilding stale or empty indexes through the existing scheduler and
  per-target replay paths in `docs/ops/content-pipeline-search-sync.md`.
- Use engine-native snapshots or volume backups only as a speed optimization.
- If a restored database points at older meme/search state, treat both search
  engines as stale until the S03 smoke proof passes.
- Do not treat a `synced` snapshot row as sufficient after object or DB restore;
  prove the engine can find the item.

## Launch Verification Drill

Run this drill before launch and after changing backup storage. It proves that a
backup can be read, PostgreSQL can restore into a scratch database, and object
storage can restore into a scratch bucket without overwriting production data.

```bash
export ENV_FILE=.env.prod
export COMPOSE_FILE=docker-compose.prod.example.yml
export BACKUP_TS=<timestamp>
export DRILL_DB=memexpert_restore_drill_<timestamp>
export DRILL_BUCKET=<bucket>-restore-drill-<timestamp>

mkdir -p ".artifacts/ops-backups/$BACKUP_TS"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' \
  > ".artifacts/ops-backups/$BACKUP_TS/postgres.dump"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres sh -lc \
  'pg_restore --list' \
  < ".artifacts/ops-backups/$BACKUP_TS/postgres.dump" \
  > ".artifacts/ops-backups/$BACKUP_TS/postgres.dump.list"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm \
  -v "$PWD/.artifacts/ops-backups/$BACKUP_TS:/backup" \
  --entrypoint /bin/sh minio-init -lc \
  'mc alias set prod http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && mc mirror --overwrite "prod/$S3_BUCKET" "/backup/s3/$S3_BUCKET"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -e DRILL_DB="$DRILL_DB" postgres sh -lc \
  'dropdb --if-exists -U "$POSTGRES_USER" "$DRILL_DB" && createdb -U "$POSTGRES_USER" "$DRILL_DB"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -e DRILL_DB="$DRILL_DB" postgres sh -lc \
  'pg_restore -U "$POSTGRES_USER" -d "$DRILL_DB" --no-owner --no-acl' \
  < ".artifacts/ops-backups/$BACKUP_TS/postgres.dump"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -e DRILL_DB="$DRILL_DB" postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$DRILL_DB" -v ON_ERROR_STOP=1 -c "SELECT version_num FROM alembic_version;" -c "SELECT count(*) AS meme_files FROM meme_files;"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm \
  -e DRILL_BUCKET="$DRILL_BUCKET" \
  -v "$PWD/.artifacts/ops-backups/$BACKUP_TS:/backup:ro" \
  --entrypoint /bin/sh minio-init -lc \
  'mc alias set prod http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && mc mb --ignore-existing "prod/$DRILL_BUCKET" && mc mirror --overwrite "/backup/s3/$S3_BUCKET" "prod/$DRILL_BUCKET" && mc ls --recursive --summarize "prod/$DRILL_BUCKET"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -e DRILL_DB="$DRILL_DB" postgres sh -lc \
  'dropdb --if-exists -U "$POSTGRES_USER" "$DRILL_DB"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm \
  -e DRILL_BUCKET="$DRILL_BUCKET" \
  --entrypoint /bin/sh minio-init -lc \
  'mc alias set prod http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && mc rb --force "prod/$DRILL_BUCKET"'
```

Pass criteria:

- `postgres.dump` and `postgres.dump.list` exist under the backup timestamp.
- Scratch restore prints one Alembic version row and a `meme_files` count.
- Scratch bucket mirror completes and `mc ls --summarize` reports objects or an
  intentional empty bucket.
- Cleanup removes the scratch database and scratch bucket.

## Post-Restore Checks

After any real restore or rollback:

```bash
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --since 10m api workers scheduler telegram-crawler
curl -fsS http://127.0.0.1:<api-port>/health
```

Then run the relevant pipeline smoke proof from `docs/ops/content-pipeline-smoke.md`
or `docs/ops/content-pipeline-search-sync.md`. For search recovery, expect
Qdrant and Meilisearch to be stale until replay/scheduler work finishes and the
S03 smoke proof passes.
