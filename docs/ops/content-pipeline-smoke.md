# S01 Content Pipeline Smoke Run

This is the repeatable operator proof for milestone M002 / slice S01.

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
5. a real upload reaches durable ingest state
6. the worker can be forced to fail `transcode`
7. the failed item is visible through the supported list/detail HTTP surfaces
8. re-uploading the same file returns the supported duplicate terminal outcome
9. replaying the failed item after removing the failure injection drives it to success without manual DB edits

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
- If the dataset has already been partially ingested in previous runs, the script walks deterministic files until it finds one that still produces a fresh non-duplicate upload.
