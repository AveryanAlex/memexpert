# Containerized PRD E2E

Run the real-stack PRD E2E suite from the repository root:

```sh
python scripts/run_container_e2e.py
```

The orchestrator exclusively creates `.artifacts/e2e/<run-id>/`, exports `E2E_RUN_ID` and `E2E_ARTIFACT_DIR`, sets per-run default main/worker/frontend/e2e-runner image tags, starts `docker-compose.e2e.yml` with `docker compose -p memexpert-e2e-<run-id>`, waits for service health, and then verifies that RabbitMQ has consumers on all seven pipeline queues before seeding. It runs the seed proof and Playwright inside the Compose network, captures status/logs/metadata, and removes the stack with volumes unless `E2E_KEEP_STACK=1` is set.

Before starting Compose, the runner rejects an existing artifact directory and atomically claims the Compose project name with a dedicated labeled Docker network. A second worktree using the same sanitized run ID fails at network creation even when its artifact root differs. The claim remains while `E2E_KEEP_STACK=1` is active or any normal cleanup step fails, and is removed only after the stack, volumes, and per-run images clean up successfully. The runner also rejects pre-existing Compose-labeled containers, volumes, or networks and never joins or tears down a stack it did not establish as available. Explicit run IDs are normalized to Docker-safe values; normalized IDs longer than 48 characters retain a hash suffix so different long IDs cannot collide merely because of truncation.

## Stack

The E2E Compose file mirrors the production process split: `postgres`, `redis`, `rabbitmq`, `qdrant`, `meilisearch`, `minio`, `minio-init`, `imgproxy`, `migrate`, `api`, `workers`, `frontend`, `seed`, and `e2e-runner`. `api`/`migrate`/`seed` use the unified `main` image target; `workers` uses the worker image target with extra dependencies and media/OCR tooling.

It deliberately has no fixed host ports, no `container_name`, and no fixed Compose project name. Named volumes are project-scoped by Compose, so concurrent runs are isolated by the `memexpert-e2e-<run-id>` project name.

The checked-in local, E2E, and production examples use verified explicit infrastructure releases rather than mutable broad tags: PostgreSQL 16.14, Redis 7.4.9, RabbitMQ 4.3.1-management, Qdrant 1.18.2, Meilisearch 1.46.1, MinIO `RELEASE.2025-09-07T16-13-09Z`, MinIO Client `RELEASE.2025-08-13T08-35-41Z`, and imgproxy 4.0.4. The optional local pgAdmin profile uses pgAdmin 9.16.

Both E2E and production `minio-init` clear the `mc` image entrypoint and execute an explicit `/bin/sh -c` command, so bucket initialization cannot be reinterpreted as arguments to the `mc` entrypoint.

RabbitMQ runs as the image's `rabbitmq` user in every Compose variant. This keeps its Erlang cookie readable by both the broker and in-container `rabbitmqctl` under rootless Docker while preserving named-volume ownership.

By default, the runner also sets `MEMEXPERT_MAIN_IMAGE`, `MEMEXPERT_WORKER_IMAGE`, `MEMEXPERT_FRONTEND_IMAGE`, and `MEMEXPERT_E2E_RUNNER_IMAGE` to tags derived from the sanitized run id. This avoids concurrent runs racing on mutable global image tags. If you explicitly provide any of those variables, the runner honors your value and does not remove that image tag during cleanup.

After `workers` starts, the runner polls `rabbitmqctl list_consumers --formatter=json` through the RabbitMQ Compose service under a fixed monotonic deadline. Seed begins only after consumers exist for `pipeline.media_inspect`, `pipeline.transcode`, `pipeline.ocr`, `pipeline.embed`, `pipeline.classify`, `pipeline.sync_qdrant`, and `pipeline.sync_meili`. The Compose `service_started` dependency remains intentionally truthful: it does not claim process readiness, while the orchestrator owns the stronger consumer-registration gate.

Seed HTTP retries and eventual-consistency pollers use caller-owned monotonic deadlines. Each fixed logical phase—health, upload, materialization, dual sync, public visibility, Meilisearch visibility, and final proofs—receives a fresh `--timeout-seconds` budget. A slow phase therefore cannot starve a later independent phase, while every individual request timeout and retry chain remains capped by its current phase deadline. Only transport errors, HTTP 408/425/429, and 5xx responses are retried; non-idempotent writes require a durable replay identity.

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
- `run-metadata.json`: run id, project name, artifact path, timestamps, process exit code, keep-stack flag, project-claim release/retention state, primary failure, and structured artifact-capture/cleanup failures.
- `playwright-report/` and `playwright-test-results/`: Playwright reports, traces, screenshots, and videos.

Every capture file starts with the command that produced it, flushed before child output. A nonzero or unlaunchable capture command leaves a visible `ARTIFACT CAPTURE FAILED` marker when the file is writable and is also recorded in `run-metadata.json`. Artifact-capture and cleanup failures are best-effort diagnostics: they are visible but do not change a successful core E2E result or replace an existing core failure code. Metadata finalization failure remains nonzero because the run result cannot be recorded truthfully.

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
docker network rm memexpert-e2e-<run-id>-claim
```

Remove the claim network only after the Compose teardown succeeds; retaining it prevents another worktree from reusing partially cleaned state.
