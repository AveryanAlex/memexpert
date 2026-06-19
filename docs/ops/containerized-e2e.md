# Containerized PRD E2E

Run the real-stack PRD E2E suite from the repository root:

```sh
python scripts/run_container_e2e.py
```

The orchestrator creates `.artifacts/e2e/<run-id>/`, exports `E2E_RUN_ID` and `E2E_ARTIFACT_DIR`, sets per-run default main/worker/frontend/e2e-runner image tags, starts `docker-compose.e2e.yml` with `docker compose -p memexpert-e2e-<run-id>`, waits for service health, runs the seed proof, runs Playwright inside the Compose network, captures status/logs/metadata, and removes the stack with volumes unless `E2E_KEEP_STACK=1` is set.

## Stack

The E2E Compose file mirrors the production process split: `postgres`, `redis`, `rabbitmq`, `qdrant`, `meilisearch`, `minio`, `minio-init`, `imgproxy`, `migrate`, `api`, `workers`, `frontend`, `seed`, and `e2e-runner`. `api`/`migrate`/`seed` use the unified `main` image target; `workers` uses the worker image target with extra dependencies and media/OCR tooling.

It deliberately has no fixed host ports, no `container_name`, and no fixed Compose project name. Named volumes are project-scoped by Compose, so concurrent runs are isolated by the `memexpert-e2e-<run-id>` project name.

By default, the runner also sets `MEMEXPERT_MAIN_IMAGE`, `MEMEXPERT_WORKER_IMAGE`, `MEMEXPERT_FRONTEND_IMAGE`, and `MEMEXPERT_E2E_RUNNER_IMAGE` to tags derived from the sanitized run id. This avoids concurrent runs racing on mutable global image tags. If you explicitly provide any of those variables, the runner honors your value and does not remove that image tag during cleanup.

Set `E2E_SKIP_IMAGE_BUILD=1` only when all four configured images already exist in the local Docker daemon. In that mode the runner validates the tags up front, uses `docker compose up --no-build`, and skips the separate E2E runner build. CI uses this mode to reuse the images it just built and loaded:

```sh
MEMEXPERT_MAIN_IMAGE=memexpert-main:ci \
MEMEXPERT_WORKER_IMAGE=memexpert-worker:ci \
MEMEXPERT_FRONTEND_IMAGE=memexpert-frontend:ci \
MEMEXPERT_E2E_RUNNER_IMAGE=memexpert-e2e-runner:ci \
E2E_SKIP_IMAGE_BUILD=1 \
python scripts/run_container_e2e.py
```

## Providers

Default CI and local E2E runs are deterministic and secret-free:

- `PIPELINE_OCR_PROVIDER_MODE=fake`
- `PIPELINE_VOYAGE_PROVIDER_MODE=fake`
- `PIPELINE_CLASSIFICATION_PROVIDER_MODE=fake`
- `PIPELINE_VOYAGE_OUTPUT_DIMENSIONS=4`

The suite does not call live Voyage, Telegram, Google, or other provider APIs. The current default path proves the manual/operator upload pipeline with fake providers. Full fake Telegram ingest is a follow-up.

Live PaddleOCR is available in the worker image through a Python 3.13 helper venv, but it is deliberately disabled for default E2E. Run the gated smoke explicitly when model downloads/runtime cost are acceptable:

```sh
docker build --target worker -t memexpert-worker:ocr-smoke .
docker run --rm \
  -v "$PWD/tests/fixtures/ocr:/fixtures:ro" \
  memexpert-worker:ocr-smoke \
  /opt/paddleocr-venv/bin/python /app/scripts/paddleocr_json.py \
  --input /fixtures/ocr-russian-office-cat-meme.png
```

## Launch-Critical Scenarios

- Public discovery through website search, URL-backed filters, detail pages, and imgproxy media rendering.
- Guest favorite/unfavorite behavior with custom collections and Pin gated to full accounts.
- Fake-provider content pipeline upload, dual search-index proof, and website discovery of the created meme.

## Artifacts

Artifacts are written under `.artifacts/e2e/<run-id>/`:

- `seed.json`: seeded public meme ids/slugs/queries and created upload proof data.
- `compose-ps.txt`: final Compose service status.
- `compose-logs.txt`: timestamped logs for app, infra, seed, and runner services.
- `compose-config.yml`: rendered E2E Compose config.
- `run-metadata.json`: run id, project name, artifact path, timestamps, exit code, and keep-stack flag.
- `playwright-report/` and `playwright-test-results/`: Playwright reports, traces, screenshots, and videos.

## Debugging

Keep the stack for inspection:

```sh
E2E_KEEP_STACK=1 python scripts/run_container_e2e.py
```

Then inspect it with the project name from `run-metadata.json`, for example:

```sh
docker compose -p memexpert-e2e-<run-id> -f docker-compose.e2e.yml ps
docker compose -p memexpert-e2e-<run-id> -f docker-compose.e2e.yml logs -f api workers frontend
```

Clean up manually when finished:

```sh
docker compose -p memexpert-e2e-<run-id> -f docker-compose.e2e.yml down -v --remove-orphans
```
