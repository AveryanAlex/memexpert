# Containerized E2E Smoke

Run the real-stack smoke suite from the repository root:

```sh
python scripts/run_container_e2e_smoke.py
```

The orchestrator creates `.artifacts/e2e/<run-id>/`, exports `E2E_RUN_ID` and `E2E_ARTIFACT_DIR`, sets per-run default app/frontend/e2e-runner image tags, starts `docker-compose.e2e.yml` with `docker compose -p memexpert-e2e-<run-id>`, waits for service health, runs the seed proof, runs Playwright inside the Compose network, captures status/logs/metadata, and removes the stack with volumes unless `E2E_KEEP_STACK=1` is set.

## Stack

The E2E Compose file mirrors the production process split: `postgres`, `redis`, `rabbitmq`, `qdrant`, `meilisearch`, `minio`, `minio-init`, `imgproxy`, `migrate`, `api`, `workers`, `frontend`, `seed`, and `e2e-runner`.

It deliberately has no fixed host ports, no `container_name`, and no fixed Compose project name. Named volumes are project-scoped by Compose, so concurrent runs are isolated by the `memexpert-e2e-<run-id>` project name.

By default, the runner also sets `MEMEXPERT_APP_IMAGE`, `MEMEXPERT_FRONTEND_IMAGE`, and `MEMEXPERT_E2E_RUNNER_IMAGE` to tags derived from the sanitized run id. This avoids concurrent runs racing on mutable global app image tags. If you explicitly provide any of those variables, the runner honors your value and does not remove that image tag during cleanup.

## Providers

Default CI and local E2E runs are deterministic and secret-free:

- `PIPELINE_OCR_PROVIDER_MODE=fake`
- `PIPELINE_VOYAGE_PROVIDER_MODE=fake`
- `PIPELINE_CLASSIFICATION_PROVIDER_MODE=fake`
- `PIPELINE_VOYAGE_OUTPUT_DIMENSIONS=4`

The suite does not call live Voyage, Telegram, Google, or other provider APIs. The current default path proves the manual/operator upload pipeline with fake providers. Full fake Telegram ingest is a follow-up.

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
E2E_KEEP_STACK=1 python scripts/run_container_e2e_smoke.py
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
