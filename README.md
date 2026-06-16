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
- `PIPELINE_SEO_MODEL`, `PIPELINE_SEO_API_BASE_URL`, `PIPELINE_SEO_API_KEY`, `PIPELINE_SEO_TIMEOUT_SECONDS`, `PIPELINE_SEO_MAX_ATTEMPTS`, `PIPELINE_SEO_PROMPT_VERSION`: SEO structured-output provider settings.
- `AUTH_JWT_SECRET`: signing secret for auth cookies and tokens.
- `SECURITY_CORS_ALLOWED_ORIGINS`: comma-separated browser origins allowed to call the API.
- `API_BASE_URL`: private backend URL used by the SvelteKit Node server.
- `HOST`, `PORT`, `ORIGIN`: SvelteKit adapter-node server settings.
- `AUTH_TELEGRAM_BOT_TOKEN`: required only when running the optional bot profile.
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_DIR`: optional Telegram crawler session settings.
- `SCHEDULER_*`: enable flags, interval seconds, and PostgreSQL advisory-lock settings for the scheduler process.

## Local Development

Start local infrastructure:

```sh
docker compose up -d
```

Install backend dependencies:

```sh
uv sync --group dev --locked
```

Normal `uv sync --locked` also installs all default development and service dependency groups. Docker targets opt out with `--no-default-groups` and add only the service group they need. PaddleOCR/PaddlePaddle are not part of the Python 3.14 uv lock; worker live OCR uses `docker/paddleocr-requirements.txt` inside a Python 3.13 helper venv.

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

Prompt provenance notes:

- The baseline prompt in `memexpert/services/meme_seo.py` is derived from the v0 Rust branch prompt at `v0:prompts/meta.md` and its structured schema in `v0:src/ai.rs`.
- The current Python backend does not pass image bytes into SEO generation yet. Live generation only sees OCR text, existing tags, language, and current template metadata, so output quality is intentionally bounded until image-aware inputs are added in a later phase.
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

`memexpert-scheduler` is the dedicated APScheduler process for periodic jobs. The current registry is intentionally limited to five jobs:

| Job | Enable variable | Interval variable | Default interval |
|---|---|---|---|
| Public trend materialized-view refresh | `SCHEDULER_MATERIALIZED_VIEW_REFRESH_ENABLED` | `SCHEDULER_MATERIALIZED_VIEW_REFRESH_INTERVAL_SECONDS` | `300` seconds |
| Popularity snapshots | `SCHEDULER_POPULARITY_SNAPSHOTS_ENABLED` | `SCHEDULER_POPULARITY_SNAPSHOTS_INTERVAL_SECONDS` | `21600` seconds |
| Meme of the Day placeholder | `SCHEDULER_MOTD_ENABLED` | `SCHEDULER_MOTD_INTERVAL_SECONDS` | `86400` seconds |
| Search-index sync placeholder | `SCHEDULER_SEARCH_INDEX_SYNC_ENABLED` | `SCHEDULER_SEARCH_INDEX_SYNC_INTERVAL_SECONDS` | `600` seconds |
| SEO backlog batches placeholder | `SCHEDULER_SEO_BACKLOG_BATCHES_ENABLED` | `SCHEDULER_SEO_BACKLOG_BATCHES_INTERVAL_SECONDS` | `900` seconds |

The public trend materialized-view refresh and popularity snapshot capture perform real business work. MOTD, search-index sync, and SEO backlog batches remain deliberate no-op placeholders so the scheduler infrastructure, observability, and deployment wiring can ship before those business behaviors land.

Popularity snapshots use `log1p`-scaled cumulative metrics from persisted tables only. Current metrics are source views, summed source reactions, forwarded/reposted source rows, platform views (`meme_view`/`view`), platform sends (`meme_send`/`share`), platform saves (`meme_save`/`save`), and platform likes (`meme_like`/`favorite`). Snapshot columns for impressions/downloads are deferred, so they are not part of the static popularity score in this stage.

Popularity weights are configurable with flat scheduler settings: `SCHEDULER_POPULARITY_SOURCE_VIEW_WEIGHT=1.0`, `SCHEDULER_POPULARITY_SOURCE_REACTION_WEIGHT=2.0`, `SCHEDULER_POPULARITY_SOURCE_REPOST_WEIGHT=3.0`, `SCHEDULER_POPULARITY_PLATFORM_VIEW_WEIGHT=1.0`, `SCHEDULER_POPULARITY_PLATFORM_SEND_WEIGHT=3.0`, `SCHEDULER_POPULARITY_PLATFORM_SAVE_WEIGHT=4.0`, and `SCHEDULER_POPULARITY_PLATFORM_LIKE_WEIGHT=5.0`.

For local no-op/startup testing, disable some or all jobs with the `*_ENABLED=false` flags and still run the scheduler process. Disabling all five jobs is a supported way to validate startup, advisory-lock acquisition, and graceful shutdown without executing business work.

The scheduler emits structured stdout logs by default. Operators should watch for these event names:

- `scheduler_runtime_started` and `scheduler_runtime_stopped` for process lifecycle.
- `scheduler_stop_requested` when the process receives `SIGINT` or `SIGTERM`.
- `scheduler_job_started`, `scheduler_job_succeeded`, and `scheduler_job_failed` with `job_id` and `duration_seconds` for each run.
- `popularity_snapshot_capture_started` and `popularity_snapshot_capture_succeeded` with `captured_at` and row counts for snapshot runs.
- `public_trend_mv_concurrent_refresh_fallback` with `view_name` when a concurrent materialized-view refresh cannot run and the scheduler retries without `CONCURRENTLY`.
- `scheduler_job_placeholder_completed` for the remaining no-op jobs.
- `scheduler_instance_lock_unavailable` if another scheduler instance already holds the advisory lock.
- `scheduler_advisory_lock_disabled` only when `SCHEDULER_ADVISORY_LOCK_ENABLED=false`.

Graceful shutdown is built into `memexpert-scheduler`: on `SIGINT` or `SIGTERM`, APScheduler stops accepting new work, waits for in-flight jobs to finish, releases the PostgreSQL advisory lock, and then exits.

Duplicate production execution is guarded by the PostgreSQL advisory lock. Keep `SCHEDULER_ADVISORY_LOCK_ENABLED=true` and set `SCHEDULER_ADVISORY_LOCK_KEY` to the same two-integer key for every legitimate scheduler deployment. If a second instance is started accidentally, it fails fast before registering jobs.

## Container Images

The Docker Images workflow publishes production-ready images to GHCR after local image smoke checks on push, tag, and manual runs:

- `ghcr.io/averyanalex/memexpert/api`
- `ghcr.io/averyanalex/memexpert/worker`
- `ghcr.io/averyanalex/memexpert/scheduler`
- `ghcr.io/averyanalex/memexpert/bot`
- `ghcr.io/averyanalex/memexpert/frontend`

Published tags include branch names such as `main`, Git tags, semver tags such as `1.2.3` and `1.2` when applicable, and immutable `sha-<short-sha>` tags. Prefer a release tag or immutable SHA tag for production pinning.

Build the split Python images:

```sh
docker build --target api -t memexpert-api:local -f Dockerfile .
docker build --target worker -t memexpert-worker:local -f Dockerfile .
docker build --target scheduler -t memexpert-scheduler:local -f Dockerfile .
docker build --target bot -t memexpert-bot:local -f Dockerfile .
```

Run the API image:

```sh
docker run --rm -p 8000:8000 --env-file .env.example memexpert-api:local
curl http://127.0.0.1:8000/health
```

Run the worker image:

```sh
docker run --rm --env-file .env.example memexpert-worker:local
```

Run the scheduler image:

```sh
docker run --rm --env-file .env.example memexpert-scheduler:local
```

Run the bot image:

```sh
docker run --rm --env-file .env.example -e AUTH_TELEGRAM_BOT_TOKEN=replace-me memexpert-bot:local
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

The example env defaults the split `MEMEXPERT_*_IMAGE` values to GHCR `:main` images. For production, pin them to release tags or immutable `sha-<short-sha>` tags from the Docker Images workflow.

Validate the stack:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml config
```

Pull the configured GHCR images and start the production-oriented stack:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml pull
docker compose --env-file .env.prod -f docker-compose.prod.example.yml up -d
```

The production example uses split Python images: `MEMEXPERT_API_IMAGE`, `MEMEXPERT_WORKER_IMAGE`, `MEMEXPERT_SCHEDULER_IMAGE`, and `MEMEXPERT_BOT_IMAGE`. It starts exactly one `scheduler` service; if a second scheduler container is started accidentally, the PostgreSQL advisory lock remains the duplicate-run guard.

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

The runner creates a sanitized run id, sets per-run default API/worker/frontend/Playwright image tags, starts `docker-compose.e2e.yml` with `docker compose -p memexpert-e2e-<run-id>`, builds the split Python/frontend/Playwright images, runs `seed`, runs the in-network Playwright/API checks, collects Compose status/logs, and tears the stack down with volumes unless `E2E_KEEP_STACK=1` is set.

The suite is parallel-safe by default: it uses no fixed host ports, no `container_name`, project-scoped named volumes, an absolute per-run artifact bind mount at `.artifacts/e2e/<run-id>/`, and run-scoped default API/worker/frontend/e2e-runner image tags. Set `E2E_RUN_ID=<id>` to choose a deterministic run id, or set `MEMEXPERT_API_IMAGE`, `MEMEXPERT_WORKER_IMAGE`, `MEMEXPERT_FRONTEND_IMAGE`, or `MEMEXPERT_E2E_RUNNER_IMAGE` to opt into explicit image tags.

Default E2E provider policy is local and secret-free: OCR, Voyage embeddings, and classification run in fake mode, Voyage dimensions are reduced to `4`, auth cookies are non-secure for the Compose network, and security rate limiting is disabled for deterministic PRD coverage. CI does not call live Voyage, Telegram, Google, or other provider APIs.

The default CI E2E path uses the operator upload pipeline plus fake providers. It covers public discovery, guest favorite/library boundaries, and the pipeline/indexing loop. Full fake Telegram ingest is not wired in this slice.

## CI

`.github/workflows/ci.yml` runs backend lint/type/test checks, frontend checks/tests/builds, frontend mock smoke tests, and deterministic PRD E2E. On E2E failure, CI uploads `.artifacts/e2e/**`.

`.github/workflows/docker-images.yml` validates the production compose example, builds the split API/bot/worker/scheduler Python images and frontend image with BuildKit/GitHub Actions cache, loads local CI tags, and performs lightweight API/frontend HTTP smoke checks. It also verifies API/bot/scheduler do not import Paddle packages and the worker has FFmpeg/FFprobe plus a launchable Python 3.13 PaddleOCR helper. After smoke checks pass, non-PR runs publish `ghcr.io/averyanalex/memexpert/{api,bot,worker,scheduler,frontend}` with metadata labels plus branch, tag, semver, and `sha-<short-sha>` tags.

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
- MinIO upload failures: confirm the bucket named by `S3_BUCKET` exists. The production compose example includes a `minio-init` one-shot service to create it.
