# MemeExpert

MemeExpert is a meme catalog and content-pipeline service. The backend is a FastAPI API with SQLAlchemy/Alembic persistence, Redis-backed security/runtime state, RabbitMQ-backed heavy workers, Qdrant vector search, Meilisearch text search, S3-compatible object storage, imgproxy media delivery, and an optional Telegram bot. The web app is a SvelteKit adapter-node frontend that talks to the API from server-side load functions.

## Architecture

- `memexpert-api`: FastAPI HTTP API. It exposes `/health` on port `8000` and the application routes under `/api/v1`.
- `memexpert-workers`: RabbitMQ-backed content-pipeline workers for transcode, OCR, embedding, classification, and search-index sync.
- `memexpert-scheduler`: APScheduler runtime for periodic jobs and scheduler-only operational logs.
- `memexpert-bot`: Optional Telegram bot process using the same backend services and database.
- `frontend`: SvelteKit Node server. It serves adapter-node output on port `3000` and uses `API_BASE_URL` for private SSR API calls.
- Infrastructure: PostgreSQL, Redis, RabbitMQ, Qdrant, Meilisearch, MinIO/S3, and imgproxy.

The Python containers are split by service target (`api`, `worker`, `scheduler`, `bot`). API/bot/scheduler images install only the project common dependencies plus their service group; the worker image is the only Python image with FFmpeg/FFprobe and the separate Python 3.13 PaddleOCR helper venv.

## Prerequisites

- Python `3.14` or newer for local backend work.
- `uv` for Python dependency management.
- Node.js `22` and pnpm `10.28.0` for local frontend work.
- Docker with BuildKit and Docker Compose v2 for container workflows.

## Environment Files

- `.env.example` is for local development defaults.
- `.env.prod.example` is a production compose template with placeholders only. Copy it to an untracked file and replace every `change-me` value before running a production-like stack.
- `docker-compose.yml` is local infrastructure only. It intentionally does not run the app containers.
- `docker-compose.prod.example.yml` is the production-oriented app plus infrastructure example.

Important runtime variables:

- `DATABASE_URL`: async PostgreSQL URL, for example `postgresql+asyncpg://user:pass@postgres:5432/memexpert`.
- `REDIS_URL`: Redis URL used by rate limiting and runtime services.
- `RABBITMQ_URL`: AMQP URL used by the heavy-worker pipeline.
- `QDRANT_URL` and `MEILISEARCH_URL`: search backends.
- `MEILISEARCH_MASTER_KEY`: Meilisearch key. Use a strong production value.
- `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_REGION`: object storage.
- `IMGPROXY_BASE_URL`, `IMGPROXY_KEY`, `IMGPROXY_SALT`: media URL generation and imgproxy signing.
- `PIPELINE_OPERATOR_TOKEN`: backend operator token for pipeline admin/smoke endpoints.
- `PIPELINE_OCR_PROVIDER_MODE`: `live` runs PaddleOCR; `fake` returns deterministic text for CI/E2E.
- `PIPELINE_OCR_PADDLE_COMMAND`: optional primary PaddleOCR command. The worker image defaults it to `/opt/paddleocr-venv/bin/python /app/scripts/paddleocr_json.py --input {input}`.
- `PIPELINE_OCR_FALLBACK_ENGINE`, `PIPELINE_OCR_FALLBACK_COMMAND`: optional command fallback metadata and command. Blank by default; there is no Qwen/VLM fallback in this code path.
- `PIPELINE_SEO_PROVIDER_MODE`: `static` by default for safe local runs; switch to `live` to enable the PydanticAI/OpenAI-compatible SEO provider.
- `PIPELINE_SEO_MODEL`, `PIPELINE_SEO_API_BASE_URL`, `PIPELINE_SEO_API_KEY`, `PIPELINE_SEO_TIMEOUT_SECONDS`, `PIPELINE_SEO_MAX_ATTEMPTS`, `PIPELINE_SEO_IMAGE_MAX_BYTES`, `PIPELINE_SEO_PROMPT_VERSION`: SEO structured-output provider settings.
- `AUTH_JWT_SECRET`: signing secret for auth cookies and tokens.
- `SECURITY_CORS_ALLOWED_ORIGINS`: comma-separated browser origins allowed to call the API.
- `API_BASE_URL`: private backend URL used by the SvelteKit Node server.
- `HOST`, `PORT`, `ORIGIN`: SvelteKit adapter-node server settings.
- `FRONTEND_ORIGIN`: canonical public origin for frontend-generated SEO XML. Production should use `https://memexpert.net`; if unset, frontend XML falls back to `ORIGIN`, then `https://memexpert.net`.
- `AUTH_TELEGRAM_BOT_TOKEN`: required only when running the optional bot profile.
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_ENCRYPTION_SECRET`: optional Telegram crawler settings. Production must set a high-entropy encryption secret before importing DB-backed Telethon StringSessions.
- `SCHEDULER_*`: enable flags, interval seconds, and PostgreSQL advisory-lock settings for the scheduler process.

## Local Development

Start local infrastructure:

```sh
docker compose up -d
```

Install backend dependencies:

```sh
uv sync --locked
```

Main API/bot/scheduler dependencies live in normal project dependencies. The `dev` and `worker` dependency groups are default groups, so `uv sync --locked` installs the full local/check environment including worker-only Python extras. Docker targets opt out of default groups with `--no-default-groups`: the `main` image installs normal dependencies only, and the `worker` image adds the worker group plus its heavier system/runtime tools. PaddleOCR/PaddlePaddle are not part of the Python 3.14 uv lock; worker live OCR uses `docker/paddleocr-requirements.txt` inside a Python 3.13 helper venv.

Apply migrations:

```sh
uv run alembic upgrade head
```

Run the API:

```sh
uv run memexpert-api
```

Run the workers:

```sh
uv run memexpert-workers
```

Run the scheduler:

```sh
uv run memexpert-scheduler
```

Run the optional Telegram bot after configuring `AUTH_TELEGRAM_BOT_TOKEN`:

```sh
uv run memexpert-bot
```

Run backend checks:

```sh
uv run ruff check .
uv run ty check
uv run pytest -v
```

## SEO Structured Output

The backend SEO POC keeps local development secret-free by default:

- `PIPELINE_SEO_PROVIDER_MODE=static` uses the no-network fallback provider.
- `PIPELINE_SEO_PROVIDER_MODE=live` enables the OpenAI-compatible PydanticAI provider and requires `PIPELINE_SEO_API_KEY`.
- `PIPELINE_SEO_API_BASE_URL` is optional; leave it blank to use the provider default, or set it for an OpenAI-compatible gateway.
- `PIPELINE_SEO_MAX_ATTEMPTS` bounds transient provider retries at the service layer.
- `PIPELINE_SEO_IMAGE_MAX_BYTES` caps optional live-provider image bytes at 5 MiB by default; oversized, missing, unsupported, or unreadable primary images are skipped and generation continues text-only.

Prompt provenance notes:

- The baseline prompt in `memexpert/services/meme_seo.py` is derived from the v0 Rust branch prompt at `v0:prompts/meta.md` and its structured schema in `v0:src/ai.rs`.
- Live generation attaches eligible primary image bytes via PydanticAI `BinaryContent` when object storage can resolve them safely. It does not send S3 object keys, storage endpoints, signed URLs, or storage credentials to the model provider.
- When image bytes are absent or skipped, live generation only sees OCR text, existing tags, language, safe media metadata, and current template metadata, so output quality remains bounded by those inputs.
- Current DB provenance remains limited to `model_id`, `prompt_version`, `generated_at`, and `edited_at`; this POC does not add a richer provenance migration.

Run the frontend locally:

```sh
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

Run frontend checks:

```sh
cd frontend
pnpm check
pnpm test
pnpm build
```

## Scheduler

`memexpert-scheduler` is the dedicated APScheduler process for periodic jobs. The current registry includes these jobs:

| Job | Enable variable | Interval variable | Default interval |
|---|---|---|---|
| Public trend materialized-view refresh | `SCHEDULER_MATERIALIZED_VIEW_REFRESH_ENABLED` | `SCHEDULER_MATERIALIZED_VIEW_REFRESH_INTERVAL_SECONDS` | `300` seconds |
| Source engagement capture dispatch | `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_ENABLED` | `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_INTERVAL_SECONDS` | `21600` seconds |
| Meme of the Day cache refresh | `SCHEDULER_MOTD_ENABLED` | `SCHEDULER_MOTD_INTERVAL_SECONDS` | `86400` seconds |
| Search-index sync batches | `SCHEDULER_SEARCH_INDEX_SYNC_ENABLED` | `SCHEDULER_SEARCH_INDEX_SYNC_INTERVAL_SECONDS` | `600` seconds |
| SEO backlog batches | `SCHEDULER_SEO_BACKLOG_BATCHES_ENABLED` | `SCHEDULER_SEO_BACKLOG_BATCHES_INTERVAL_SECONDS` | `900` seconds |
| RabbitMQ outbox publisher | `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_ENABLED` | `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_INTERVAL_SECONDS` | `5` seconds |

The public trend materialized-view refresh, source engagement capture dispatch, Meme of the Day refresh, search-index sync batches, SEO backlog batches, and RabbitMQ outbox publisher perform real business work.

Source engagement capture is split between PostgreSQL scheduling and RabbitMQ execution. `meme_sources.next_engagement_check_at` stores the durable due time; the scheduler claims due Telegram sources through their `SourceChannel -> TelegramSession` FK assignment, writes `source_engagement_capture_requested` outbox rows routed as `pipeline.source_engagement_capture.<session_key>`, and the worker consumes one single-active queue per session to fetch Telegram counters and append or update `meme_source_engagement_snapshots`. Follow-up cadence is anchored to the Telegram post date: `+1h`, `+3h`, `+12h`, `+1d`, `+3d`, `+7d`, `+1month`, then monthly. The scheduler processes up to `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_BATCH_SIZE=100` due sources per run, caps each Telegram session at `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_PER_SESSION_BATCH_SIZE=20`, and reclaims stale claims after `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_LEASE_TIMEOUT_SECONDS=1800`.

Search-index sync batches process up to `SCHEDULER_SEARCH_INDEX_SYNC_BATCH_SIZE=50` rows per target per run. The job claims `meme_file_sync_target_snapshots` rows for both Qdrant and Meilisearch, commits the `processing` claim before external writes, retries failed rows, reclaims stale `processing` rows after `SCHEDULER_SEARCH_INDEX_SYNC_PROCESSING_TIMEOUT_SECONDS=900`, and reprocesses synced rows when canonical meme/search metadata is newer than `last_success_at`.

SEO backlog batches process up to `SCHEDULER_SEO_BACKLOG_BATCH_SIZE=25` memes per run. The job prioritizes public, non-NSFW memes missing SEO pages, then stale auto-generated pages whose `prompt_version` differs from `PIPELINE_SEO_PROMPT_VERSION`; manually edited pages are skipped.

Meme of the Day refresh writes one row per UTC date and `MOTD_ALGORITHM_VERSION` in `meme_of_the_day_selections`. `GET /api/v1/memes/meme-of-the-day` is public and lazily refreshes only when today's cache row is missing. `POST /api/v1/memes/meme-of-the-day/refresh` requires an admin user and recomputes the same deterministic selection; it is not a manual override. Candidate tuning is settings-backed: `MOTD_ALGORITHM_VERSION=motd_v1`, `MOTD_CANDIDATE_LOOKBACK_DAYS=30`, `MOTD_CANDIDATE_LIMIT=50`, `MOTD_MIN_QUALITY_SCORE=0.5`, and the score weights `MOTD_POPULARITY_WEIGHT=0.35`, `MOTD_TRENDING_GROWTH_WEIGHT=0.30`, `MOTD_NOVELTY_WEIGHT=0.20`, `MOTD_QUALITY_WEIGHT=0.15`. If no public non-NSFW recent high-quality candidate exists, the cache stores a safe fallback row with `meme_id=NULL`, `candidate_count=0`, and `reason=no_candidates`. Manual/admin override remains deferred and is not implemented.

RabbitMQ outbox publisher runs every `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_INTERVAL_SECONDS=5` seconds by default. Each run starts or reuses the RabbitMQ pipeline broker, recovers `rabbitmq_outbox_messages.status='publishing'` rows whose `locked_at` is older than `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_STALE_TIMEOUT_SECONDS=300`, then publishes up to `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_BATCH_SIZE=100` due `pending`/`failed` rows by their stored `exchange`, `routing_key`, JSON payload, headers, and stable `message_id`. This is the production path for accepted raw-upload `media_inspect_requested` events, post-materialization transcode dispatches, stage fan-out, replay, and sync-success notifications.

Public trend, search, and `popularity_score` read-model fields are derived from source engagement snapshots plus `analytics_events`; there is no canonical popularity snapshot table or stored meme/source metric column. The first source snapshot is a baseline and contributes no invented historical delta. Later public chart points use only real source deltas and platform events. Snapshot `NULL` means Telegram did not expose a counter; public read models may coalesce unknown to `0` for ranking/output, but canonical storage preserves NULL-vs-zero. Telegram `forward_count` is the public forward/repost counter that feeds `latest_source_reposts`; forwarded-message attribution (`forwarded_from_*`) records where a repost originated and is not an engagement count.

For local no-op/startup testing, disable some or all jobs with the `*_ENABLED=false` flags and still run the scheduler process. Disabling every job is a supported way to validate startup, advisory-lock acquisition, and graceful shutdown without executing business work.

The scheduler emits structured stdout logs by default. Operators should watch for these event names:

- `scheduler_runtime_started` and `scheduler_runtime_stopped` for process lifecycle.
- `scheduler_stop_requested` when the process receives `SIGINT` or `SIGTERM`.
- `scheduler_job_started`, `scheduler_job_succeeded`, and `scheduler_job_failed` with `job_id` and `duration_seconds` for each run.
- `scheduler_job_batch_result` with `job_id`, `scanned`, `updated`, `failed`, `skipped`, and `duration_seconds` for search-index and SEO batch runs; the outbox publisher uses the same event with `recovered`, `claimed`, `published`, `failed`, and `duration_seconds`.
- `scheduler_job_batch_result` with `job_id=source-engagement-capture`, `claimed`, and `enqueued` for source engagement dispatch runs.
- `scheduler_job_batch_result` with `job_id=motd`, `candidate_count`, `selected_meme_id`, `reason`, `algorithm_version`, and `refreshed_at` for Meme of the Day refresh runs.
- `public_trend_mv_concurrent_refresh_fallback` with `view_name` when a concurrent materialized-view refresh cannot run and the scheduler retries without `CONCURRENTLY`.
- `scheduler_instance_lock_unavailable` if another scheduler instance already holds the advisory lock.
- `scheduler_advisory_lock_disabled` only when `SCHEDULER_ADVISORY_LOCK_ENABLED=false`.

Detailed run/replay/inspection guidance for scheduler batch and outbox jobs lives in `docs/ops/scheduler-batch-jobs.md`.

Graceful shutdown is built into `memexpert-scheduler`: on `SIGINT` or `SIGTERM`, APScheduler stops accepting new work, waits for in-flight jobs to finish, releases the PostgreSQL advisory lock, and then exits.

Duplicate production execution is guarded by the PostgreSQL advisory lock. Keep `SCHEDULER_ADVISORY_LOCK_ENABLED=true` and set `SCHEDULER_ADVISORY_LOCK_KEY` to the same two-integer key for every legitimate scheduler deployment. If a second instance is started accidentally, it fails fast before registering jobs.

## Container Images

The CI workflow publishes production-ready images to GHCR after local image smoke checks and containerized E2E pass on push, tag, and manual runs:

- `ghcr.io/averyanalex/memexpert/main`
- `ghcr.io/averyanalex/memexpert/worker`
- `ghcr.io/averyanalex/memexpert/frontend`

Published tags include branch names such as `main`, Git tags, semver tags such as `1.2.3` and `1.2` when applicable, and immutable `sha-<short-sha>` tags. Prefer a release tag or immutable SHA tag for production pinning.

Build the Python images:

```sh
docker build --target main -t memexpert-main:local -f Dockerfile .
docker build --target worker -t memexpert-worker:local -f Dockerfile .
```

Run the API from the main image:

```sh
docker run --rm -p 8000:8000 --env-file .env.example memexpert-main:local
curl http://127.0.0.1:8000/health
```

Run the worker image:

```sh
docker run --rm --env-file .env.example memexpert-worker:local
```

Run the scheduler from the main image:

```sh
docker run --rm --env-file .env.example memexpert-main:local memexpert-scheduler
```

Run the bot from the main image:

```sh
docker run --rm --env-file .env.example -e AUTH_TELEGRAM_BOT_TOKEN=replace-me memexpert-main:local memexpert-bot
```

Confirm worker-only media/OCR tools are present:

```sh
docker run --rm memexpert-worker:local ffmpeg -version
docker run --rm memexpert-worker:local ffprobe -version
docker run --rm memexpert-worker:local /opt/paddleocr-venv/bin/python -c "import paddle, paddleocr; print('paddleocr-helper-ok')"
```

Build the SvelteKit frontend image:

```sh
docker build -t memexpert-frontend:local -f frontend/Dockerfile .
```

Run the frontend image:

```sh
docker run --rm -p 3000:3000 -e API_BASE_URL=http://host.docker.internal:8000 memexpert-frontend:local
```

On Linux, add `--add-host host.docker.internal:host-gateway` if the frontend container needs to reach an API process running on the host.

## Production Compose Example

Create a real env file from the placeholder template:

```sh
cp .env.prod.example .env.prod
```

Edit `.env.prod` and replace every `change-me` placeholder.

The example env defaults `MEMEXPERT_MAIN_IMAGE`, `MEMEXPERT_WORKER_IMAGE`, and `MEMEXPERT_FRONTEND_IMAGE` to GHCR `:main` images. For production, pin them to release tags or immutable `sha-<short-sha>` tags from the CI workflow image-publish path.

Validate the stack:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml config
```

Pull the configured GHCR images and start the production-oriented stack:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml pull
docker compose --env-file .env.prod -f docker-compose.prod.example.yml up -d
```

The production example uses `MEMEXPERT_MAIN_IMAGE` for `migrate`, `api`, `scheduler`, and `bot`, and `MEMEXPERT_WORKER_IMAGE` for worker nodes with extra worker dependencies and media/OCR tooling. It starts exactly one `scheduler` service; if a second scheduler container is started accidentally, the PostgreSQL advisory lock remains the duplicate-run guard.

Run the optional bot profile only when `AUTH_TELEGRAM_BOT_TOKEN` is configured:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml --profile bot up -d bot
```

Apply only migrations if needed:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml run --rm migrate
```

Check status and logs:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml ps
docker compose --env-file .env.prod -f docker-compose.prod.example.yml logs -f api workers scheduler frontend
```

## Containerized PRD E2E

Run the deterministic real-stack PRD E2E suite with one command:

```sh
python scripts/run_container_e2e.py
```

The runner creates a sanitized run id, sets per-run default main/worker/frontend/Playwright image tags, starts `docker-compose.e2e.yml` with `docker compose -p memexpert-e2e-<run-id>`, builds the unified main image, worker image, frontend image, and Playwright image, runs `seed`, runs the in-network Playwright/API checks, collects Compose status/logs, and tears the stack down with volumes unless `E2E_KEEP_STACK=1` is set. CI prebuilds those four images once, passes their explicit tags to the runner, and sets `E2E_SKIP_IMAGE_BUILD=1` so Compose reuses the loaded images instead of rebuilding them during E2E.

The suite is parallel-safe by default: it uses no fixed host ports, no `container_name`, project-scoped named volumes, an absolute per-run artifact bind mount at `.artifacts/e2e/<run-id>/`, and run-scoped default main/worker/frontend/e2e-runner image tags. Set `E2E_RUN_ID=<id>` to choose a deterministic run id, or set `MEMEXPERT_MAIN_IMAGE`, `MEMEXPERT_WORKER_IMAGE`, `MEMEXPERT_FRONTEND_IMAGE`, or `MEMEXPERT_E2E_RUNNER_IMAGE` to opt into explicit image tags.

Default E2E provider policy is local and secret-free: OCR, Voyage embeddings, and classification run in fake mode, Voyage dimensions are reduced to `4`, auth cookies are non-secure for the Compose network, and security rate limiting is disabled for deterministic PRD coverage. CI does not call live Voyage, Telegram, Google, or other provider APIs.

The default CI E2E path uses the operator upload pipeline plus fake providers. It covers public discovery, guest favorite/library boundaries, and the pipeline/indexing loop. Full fake Telegram ingest is not wired in this slice.

## CI

`.github/workflows/ci.yml` runs backend lint/type/test checks, frontend checks/tests/builds, frontend mock smoke tests, production compose validation, Docker image builds, image smoke checks, and deterministic PRD E2E. The E2E job builds and loads the unified main Python image, worker Python image, frontend image, and E2E runner image with BuildKit/GitHub Actions cache, then runs E2E with `E2E_SKIP_IMAGE_BUILD=1` so it reuses those loaded tags. On E2E failure, CI uploads `.artifacts/e2e/**`. After smoke checks and E2E pass, non-PR runs publish `ghcr.io/averyanalex/memexpert/{main,worker,frontend}` with metadata labels plus branch, tag, semver, and `sha-<short-sha>` tags.

Run the local real OCR smoke only when the worker image has been built and model downloads are acceptable:

```sh
docker build --target worker -t memexpert-worker:ocr-smoke .
docker run --rm \
  -v "$PWD/tests/fixtures/ocr:/fixtures:ro" \
  memexpert-worker:ocr-smoke \
  /opt/paddleocr-venv/bin/python /app/scripts/paddleocr_json.py \
  --input /fixtures/ocr-russian-office-cat-meme.png
```

## Troubleshooting

- API container fails before `/health`: inspect `docker logs <container>`. The health route does not require database connectivity, so failures usually come from settings parsing or process startup.
- Worker exits on OCR: confirm `PIPELINE_OCR_PROVIDER_MODE=live` and `PIPELINE_OCR_PADDLE_COMMAND` points at the worker helper (`/opt/paddleocr-venv/bin/python /app/scripts/paddleocr_json.py --input {input}`). API/bot/scheduler images intentionally do not contain PaddleOCR/PaddlePaddle.
- Worker transcode failures: verify `ffmpeg` and `ffprobe` are available in the worker image with the CI smoke commands above.
- Frontend shows catalog API errors: confirm `API_BASE_URL` points to the private API URL reachable from the SvelteKit container or Node process.
- Browser auth/CORS issues: align `ORIGIN`, `SECURITY_CORS_ALLOWED_ORIGINS`, API cookie secure/domain settings, and the public reverse-proxy host.
- SEO XML issues: confirm `FRONTEND_ORIGIN=https://memexpert.net` in production and that the frontend can reach `/api/v1/seo/summary`; Pinterest consumers should use `/feeds/pinterest.xml`.
- MinIO upload failures: confirm the bucket named by `S3_BUCKET` exists. The production compose example includes a `minio-init` one-shot service to create it.
