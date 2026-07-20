# Backup, Restore, And Rollback Runbook

This runbook covers manual backups, restore drills, and service/image rollback.
The commands below remain the local/production-example Docker Compose form. The
live beta at `beta.memexpert.net` is the NixOS/Podman Quadlet deployment on
`whale`; its source of truth is the sibling dotfiles repo, not generated unit
files on the host. It uses placeholders only; never paste secrets into tickets,
logs, or committed files.

## Scope And Source Of Truth

- PostgreSQL is the authoritative store for users, memes, pipeline state,
  crawler state, scheduler state, and search-sync snapshots.
- S3/MinIO-compatible object storage holds originals, temp originals, and
  immutable derivative generations under the configured `S3_BUCKET`.
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

For live beta runtime changes, edit `../dotfiles/apps/memexpert/default.nix`,
validate the dotfiles flake, and use `./deploy.sh whale build` then `switch` (or
`test`) from that repo. `deploy.sh` builds on the target. Never edit generated
Quadlet/systemd files directly. Image-only rollback/redeploy still follows the
normal CI/Reploy dependency ordering and all three application images must exist
before rollout.

## Safety Rules

- Freeze app writes before a destructive restore: stop `frontend`, `api`, all
  five `worker-*` roles, `scheduler`, and `telegram-crawler` first. Stop the
  optional `bot` too if that profile is enabled.
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
- PostgreSQL active media pointers and immutable generation objects must come
  from the same backup window. Pause generation GC during restore/reconciliation;
  never delete current, young, unknown, or referenced objects to make a restore
  look tidy.

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
- Live storage retains superseded recognized generations for seven days, but a
  point-in-time object backup must retain the generation set paired with each
  PostgreSQL backup for the full database restore window. The live GC window is
  not permission to prune historical backup media.
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

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" stop \
  frontend api worker-media worker-ocr worker-enrichment worker-sync worker-telegram scheduler telegram-crawler

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres sh -lc \
  'dropdb --if-exists -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres sh -lc \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl' \
  < ".artifacts/ops-backups/$BACKUP_TS/postgres.dump"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm \
  -v "$PWD/.artifacts/ops-backups/$BACKUP_TS:/backup:ro" \
  --entrypoint /bin/sh minio-init -lc \
  'mc alias set prod http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && mc mirror --overwrite "/backup/s3/$S3_BUCKET" "prod/$S3_BUCKET"'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d \
  migrate api worker-media worker-ocr worker-enrichment worker-sync worker-telegram scheduler telegram-crawler frontend
```

Keep media-generation GC disabled until the restored database has been checked
against restored objects. For every non-null active web-video pointer, derive
and verify its sibling `preview.png`; verify its durable generation record and
profile/audio observations where applicable. Extra superseded/unknown objects
are safer than missing active objects and must not be removed manually.

Add the optional `bot` service to the stop/up commands only when the bot profile
is enabled in that environment.

## Service/Image Rollback

Use this when the database does not need to move backward. The rollback target
must be a previously published immutable tag for all three app images.

### Immutable-media compatibility boundary

Migration `0042` and the first activation of an immutable media generation form
a one-way runtime compatibility boundary. Once any active web-video pointer uses
`pipeline/derived/{file_id}/generations/{generation_id}/web.mp4`, do **not** roll
the API, workers, or frontend back to an image from before `0042`. The old
rendering code derives the flat legacy poster path
`pipeline/derived/{file_id}/preview.png`; it cannot pair that poster with the
active generation video and can therefore break playback even if the forward
database schema still accepts the old image.

Treat all three application images as one release unit across this boundary. In
an incident, deploy a forward hotfix that understands both legacy and generation
layouts. The only safe way to run a pre-`0042` image again is a coordinated
PostgreSQL **and** object-storage restore from a window before immutable
generation activation, with the accepted loss of later writes explicitly
approved. An Alembic downgrade or image-only rollback does not rewrite active
object pointers and is not sufficient.

```bash
export ENV_FILE=.env.prod
export COMPOSE_FILE=docker-compose.prod.example.yml
export PREVIOUS_SHA_TAG=<previous-sha-tag>
export MEMEXPERT_MAIN_IMAGE="ghcr.io/averyanalex/memexpert/main:$PREVIOUS_SHA_TAG"
export MEMEXPERT_WORKER_IMAGE="ghcr.io/averyanalex/memexpert/worker:$PREVIOUS_SHA_TAG"
export MEMEXPERT_FRONTEND_IMAGE="ghcr.io/averyanalex/memexpert/frontend:$PREVIOUS_SHA_TAG"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --images
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull \
  migrate api worker-media worker-ocr worker-enrichment worker-sync worker-telegram scheduler telegram-crawler frontend
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --no-build \
  migrate api worker-media worker-ocr worker-enrichment worker-sync worker-telegram scheduler telegram-crawler frontend
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
   `0042` becomes runtime-incompatible with older images as soon as an immutable
   generation is activated; follow the compatibility boundary above even when
   the database columns themselves are additive.
4. Restore PostgreSQL only when the release corrupted data, the accepted write
   loss is understood, and object storage from the same backup window is
   available.
5. After DB restore, run the normal migration service from the chosen image tag
   and then verify API health, scheduler logs, and search-index replay/status
   checks.

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
  engines as stale until per-target replay/status checks show they were rebuilt.
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
- Scratch restore can read `media_generations` and every active derivative key
  belongs either to a durable generation or a recognized legacy layout.
- Scratch bucket mirror completes and `mc ls --summarize` reports objects or an
  intentional empty bucket.
- Cleanup removes the scratch database and scratch bucket.

## Post-Restore Checks

After any real restore or rollback:

```bash
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --since 10m \
  api worker-media worker-ocr worker-enrichment worker-sync worker-telegram scheduler telegram-crawler
curl -fsS http://127.0.0.1:<api-port>/health
```

Then run the relevant API health, pipeline item-detail, and per-target
search-sync checks from `docs/ops/content-pipeline-search-sync.md`. For search
recovery, expect Qdrant and Meilisearch to be stale until replay/scheduler work
finishes and target status rows show fresh `synced` results.

On Whale, use the production-safe checks from the repository notes (`ssh whale`
plus systemd/Podman/API health/journald), not the Compose commands above. Open
Replay & Repair and verify:

- every active web video resolves and has its derived sibling poster;
- active `web-h264-aac-1080p30-v2` generations retain verified dimensions, FPS,
  bitrate, codecs, and source/output audio state;
- no generation referenced by PostgreSQL is missing from restored storage;
- generation GC remains stopped until these checks pass; and
- any search/index drift is repaired through versioned stage-only or cascading
  jobs rather than by editing snapshot rows.
