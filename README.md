# MemeExpert

MemeExpert is a meme catalog and content-pipeline service. The backend is a FastAPI API with SQLAlchemy/Alembic persistence, Redis-backed security/runtime state, RabbitMQ-backed heavy workers, Qdrant vector search, Meilisearch text search, S3-compatible object storage, imgproxy media delivery, and an optional Telegram bot. The web app is a SvelteKit adapter-node frontend that talks to the API from server-side load functions.

## Architecture

- `memexpert-api`: FastAPI HTTP API. It exposes `/health` on port `8000` and the application routes under `/api/v1`.
- `memexpert-workers`: RabbitMQ-backed content-pipeline workers for transcode, OCR, embedding, classification, and search-index sync.
- `memexpert-bot`: Optional Telegram bot process using the same backend services and database.
- `frontend`: SvelteKit Node server. It serves adapter-node output on port `3000` and uses `API_BASE_URL` for private SSR API calls.
- Infrastructure: PostgreSQL, Redis, RabbitMQ, Qdrant, Meilisearch, MinIO/S3, and imgproxy.

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
- `AUTH_JWT_SECRET`: signing secret for auth cookies and tokens.
- `SECURITY_CORS_ALLOWED_ORIGINS`: comma-separated browser origins allowed to call the API.
- `API_BASE_URL`: private backend URL used by the SvelteKit Node server.
- `HOST`, `PORT`, `ORIGIN`: SvelteKit adapter-node server settings.
- `AUTH_TELEGRAM_BOT_TOKEN`: required only when running the optional bot profile.
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_DIR`: optional Telegram crawler session settings.

## Local Development

Start local infrastructure:

```sh
docker compose up -d
```

Install backend dependencies:

```sh
uv sync --group dev --locked
```

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

Run the optional Telegram bot after configuring `AUTH_TELEGRAM_BOT_TOKEN`:

```sh
uv run memexpert-bot
```

Run backend checks:

```sh
uv run ruff check .
uv run mypy .
uv run pytest -v
```

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

## Container Images

Build the Python app image for API, workers, and bot:

```sh
docker build -t memexpert-app:local -f Dockerfile .
```

Run the API image:

```sh
docker run --rm -p 8000:8000 --env-file .env.example memexpert-app:local
curl http://127.0.0.1:8000/health
```

Run the worker command from the same image:

```sh
docker run --rm --env-file .env.example memexpert-app:local memexpert-workers
```

Run the bot command from the same image:

```sh
docker run --rm --env-file .env.example -e AUTH_TELEGRAM_BOT_TOKEN=replace-me memexpert-app:local memexpert-bot
```

Confirm media tools are present in the Python image:

```sh
docker run --rm memexpert-app:local ffmpeg -version
docker run --rm memexpert-app:local ffprobe -version
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

Edit `.env.prod` and replace every `change-me` placeholder. Then validate the stack:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml config
```

Build and start the production-oriented stack:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.example.yml up -d --build
```

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
docker compose --env-file .env.prod -f docker-compose.prod.example.yml logs -f api workers frontend
```

## Containerized E2E Smoke

Run the deterministic real-stack smoke suite with one command:

```sh
python scripts/run_container_e2e_smoke.py
```

The runner creates a sanitized run id, sets per-run default app/frontend/Playwright image tags, starts `docker-compose.e2e.yml` with `docker compose -p memexpert-e2e-<run-id>`, builds the app/frontend/Playwright images, runs `seed`, runs the in-network Playwright/API checks, collects Compose status/logs, and tears the stack down with volumes unless `E2E_KEEP_STACK=1` is set.

The suite is parallel-safe by default: it uses no fixed host ports, no `container_name`, project-scoped named volumes, an absolute per-run artifact bind mount at `.artifacts/e2e/<run-id>/`, and run-scoped default app/frontend/e2e-runner image tags. Set `E2E_RUN_ID=<id>` to choose a deterministic run id, or set `MEMEXPERT_APP_IMAGE`, `MEMEXPERT_FRONTEND_IMAGE`, or `MEMEXPERT_E2E_RUNNER_IMAGE` to opt into explicit image tags.

Default E2E provider policy is local and secret-free: OCR, Voyage embeddings, and classification run in fake mode, Voyage dimensions are reduced to `4`, auth cookies are non-secure for the Compose network, and security rate limiting is disabled for smoke stability. CI does not call live Voyage, Telegram, Google, or other provider APIs.

The default CI E2E path uses the operator upload pipeline plus fake providers. Full fake Telegram ingest is not wired in this slice.

## CI

`.github/workflows/ci.yml` runs backend lint/type/test checks, frontend checks/tests/builds, frontend mock smoke tests, local infrastructure compose smoke checks, and the deterministic container E2E smoke job. On container E2E failure, CI uploads `.artifacts/e2e/**`.

`.github/workflows/docker-images.yml` validates the production compose example, builds the Python and frontend images with BuildKit/GitHub Actions cache, loads local CI tags, and performs lightweight API/frontend HTTP smoke checks without publishing images or requiring secrets.

## Troubleshooting

- API container fails before `/health`: inspect `docker logs <container>`. The health route does not require database connectivity, so failures usually come from settings parsing or process startup.
- Worker exits on OCR: PaddleOCR is intentionally not installed in the image. The configured worker reports the OCR provider as unavailable unless an installed provider or fallback command is configured.
- Worker transcode failures: verify `ffmpeg` and `ffprobe` are available with the image commands above.
- Frontend shows catalog API errors: confirm `API_BASE_URL` points to the private API URL reachable from the SvelteKit container or Node process.
- Browser auth/CORS issues: align `ORIGIN`, `SECURITY_CORS_ALLOWED_ORIGINS`, API cookie secure/domain settings, and the public reverse-proxy host.
- MinIO upload failures: confirm the bucket named by `S3_BUCKET` exists. The production compose example includes a `minio-init` one-shot service to create it.
