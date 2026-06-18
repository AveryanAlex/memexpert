# S01 Content Pipeline Smoke Run

This is the historical repeatable operator proof for milestone M002 / slice S01.

Stage 2 of the ingest-request refactor changes `POST /api/v1/pipeline/uploads` to return a raw `IngestRequestRead` and enqueue `media_inspect_requested` in `pipeline_outbox_events`. The old upload-to-`MemeFile` smoke flow is not valid until the next worker materialization stage consumes those outbox events. During this stage, operators should verify raw acceptance with `GET /api/v1/pipeline/ingest-requests` and keep `/api/v1/pipeline/items` for already-materialized `MemeFile` rows only.

It uses the real local dataset at `/home/alex/Documents/MemeDataset`, the live Docker Compose infrastructure, and native API/worker processes. It does **not** copy dataset files into the repo.

## One-command proof

Start the local infrastructure first:

```bash
IMGPROXY_PORT=18080 docker compose up -d
```

Then run the bounded smoke script:

```bash
uv run python scripts/verify_s01_runtime.py \
  --dataset-root /home/alex/Documents/MemeDataset \
  --api-base-url http://127.0.0.1:8000 \
  --fail-stage transcode
```

What the script proves:

1. the dataset exists and contains supported image files
2. required compose services are up and healthy
3. the S3 bucket exists or can be created against the live MinIO endpoint
4. Alembic migrations are applied before the API starts
5. a real upload reaches durable raw ingest-request state
6. an outbox row exists for future media inspection
7. raw ingest is visible through `/api/v1/pipeline/ingest-requests`
8. `/api/v1/pipeline/items` remains reserved for already-materialized `MemeFile` rows

The transcode failure/replay assertions from the original S01 proof are temporarily deferred for new uploads until worker materialization lands.

The script starts and stops native `memexpert-api` and `memexpert-workers` processes itself. It also uses a **unique RabbitMQ topology per run** so stale retries from earlier smoke attempts cannot taint the current proof.

## Artifacts and diagnostics

Each run writes logs and a machine-readable report under:

```text
.artifacts/s01-runtime-smoke/<run-id>/
```

Expected files:

- `api.log`
- `worker-bootstrap.log`
- `worker-failure.log`
- `worker-replay.log`
- `alembic-upgrade.log`
- `report.json`

If the script fails, start with `report.json`, then inspect the relevant process log.

## Manual debug commands

The smoke script is the normal path. Use these only when you need to reproduce one side of the flow manually.

Start the API:

```bash
uv run memexpert-api
```

Start the worker normally:

```bash
uv run memexpert-workers
```

Start the worker with forced transcode failure for one known item:

```bash
PIPELINE_WORKER_FAIL_TRANSCODE_FOR_MEME_FILE_ID=<meme_file_id> uv run memexpert-workers
```

## Notes

- Use `IMGPROXY_PORT=18080` on this workstation because host port `8080` is already occupied.
- The smoke script refuses to run if stale native `memexpert-api` or `memexpert-workers` processes are already present.
- If the dataset has already been partially accepted in previous runs, source replay should return the existing ingest request rather than creating duplicate temp objects or outbox work.
