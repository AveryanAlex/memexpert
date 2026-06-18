# S02 Content Pipeline Heavy-Worker Runbook

This runbook covers the operator proof loop for milestone **M002 / slice S02**:
the real heavy-worker chain (`transcode → ocr → embed → classify → meme_ready`)
running against the live local stack plus `/home/alex/Documents/MemeDataset`.

Stage 3a of the ingest-request refactor moves manual uploads through raw
`pipeline_ingest_requests` plus `pipeline_outbox_events`. New uploads first land
as temp originals, then the worker consumes `media_inspect_requested`, runs the
worker-only media inspector/pHash checks, materializes `Meme`/`MemeFile` state,
and writes the downstream transcode event back to the transactional outbox.

The S01 runbook (`docs/ops/content-pipeline-smoke.md`) still applies for the
upload → replay → duplicate proof. S02 layers the heavy chain on top and adds
a machine-readable run summary that operators read *first* before trusting
any green output.

## Prerequisites

- Docker Compose healthy: `IMGPROXY_PORT=18080 docker compose up -d` (use the
  non-default imgproxy port because host port `8080` is already occupied).
- Alembic head applied: `uv run alembic upgrade head`.
- The native API running on `http://127.0.0.1:8000`: `uv run memexpert-api`.
- The native workers running: `uv run memexpert-workers`.
- Environment variables:
  - `MEMEXPERT_PIPELINE_OPERATOR_TOKEN` — read from `memexpert.core.config.get_settings`
    in the proof harness, so the same token the API accepts is used.
  - `MEMEXPERT_PIPELINE_VOYAGE_API_KEY` — required for the real embed stage.
  - `MEMEXPERT_PIPELINE_CLASSIFICATION_API_URL` / token — required for the
    classify stage (refer to `memexpert/core/classification.py`).
  - Qdrant + MinIO + RabbitMQ credentials are already wired by Docker Compose.

The heavy chain needs real Voyage and classification credentials. If either is
missing the harness will still run, but items will stall at `embed` or
`classify` and the run summary will flag them as blocked — that is the truthful
outcome and is what operators read as "partial trust, fix the provider".

## Running the proof harness

```bash
uv run python scripts/verify_s02_runtime.py \
  --dataset-root /home/alex/Documents/MemeDataset \
  --api-base-url http://127.0.0.1:8000 \
  --artifacts-dir .artifacts/s02-runtime-smoke
```

What the harness does:

1. Walks the dataset root deterministically and picks the first
   `--candidate-limit` supported files (default 8).
2. Uploads each file through the real operator route and waits for the
   media-inspect/materializer worker to create the `MemeFile` state.
3. Polls the enriched `GET /api/v1/pipeline/items/{id}/detail` surface until
   every uploaded item reaches a terminal state (`meme_ready` emitted,
   duplicate, or failed) or the `--stage-timeout` elapses.
4. Aggregates pass rate, OCR fallback rate, stage timing percentiles, merge
   count, blocked items, and emitted `meme_ready` event ids.
5. Writes `report.json` + `report.md` under
   `.artifacts/s02-runtime-smoke/<run-id>/`.

Exit codes:

- `0` — every item reached a terminal success/duplicate state and no errors
  were captured.
- `1` — the harness hit a startup failure (missing dataset, API unreachable,
  bad credentials). The partial summary is still written so operators can
  triage from the artifact.
- `2` — the run completed but had blocked items or per-item errors. Inspect
  the `errors` array and `blocked_item_ids` in `report.json`, then use the
  drill-down URL inside each item report to replay the affected items
  through the S01 replay route.

### Dry-run mode

```bash
uv run python scripts/verify_s02_runtime.py --dry-run --artifacts-dir .artifacts/s02-runtime-smoke
```

`--dry-run` skips every HTTP call and exercises only the summary pipeline with
an empty corpus. It is the fastest way to verify the aggregation + reporting
wiring (and it is what CI uses when the real Voyage/classification stack is
not reachable).

## Reading the run summary

`report.json` is the authoritative structured artifact. `report.md` is a short
human-readable version of the same data.

First-read checklist for operators:

1. `stage_counts.ready_count` — how many items emitted a truthful
   `meme_ready`. Compare against `bounded_item_count`.
2. `stage_counts.blocked_count` and `blocked_item_ids` — items that stalled
   on an external provider (Voyage, Qdrant, classification). These are
   replayable; follow the drill-down URL to the S01 detail route, then replay
   via `POST /api/v1/pipeline/items/{id}/replay`.
3. `stage_counts.ocr_fallback_used` and `stage_counts.ocr_low_confidence` —
   two separate buckets. Both are *not failures*; they mark items that need
   operator skepticism when reviewing extracted text.
4. `stage_counts.merge_count` — how many items auto-merged into an older
   canonical meme. Each merge is backed by a `MemeMergeLog` row and is
   visible via the enriched detail route's `merge` projection.
5. `stage_timings[*]` — p50/p95/max latency per stage. Compare run to run on
   target hardware; sustained regressions here are a heavy-chain problem,
   not a script problem.
6. `errors` — actionable diagnostics captured during the run. Treat a
   non-empty array as "run did not fully succeed" even if every item
   eventually reached a terminal state.

## Replaying blocked items

For each entry in `blocked_item_ids`:

```bash
curl -sS -X POST \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"stage": "embed"}' \
  http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/replay
```

Replace `embed` with the blocked stage reported in `item_reports[*].terminal_stage`.
The enriched detail route will reflect the new attempt within a poll interval.

## Raw ingest, temp storage, and outbox recovery

The first worker hop is `pipeline.media_inspect`. It consumes
`media_inspect_requested` events and is separate from materialized stage events
such as `meme_created` or `meme_transcoded`.

Operational rules:

1. `pipeline_ingest_requests.status=media_inspect_pending` means the API has
   accepted bytes and an outbox row should publish a media-inspect event.
2. `failed_invalid_media` keeps the temp object under
   `pipeline/temp-originals/<ingest_request_id>/...` for retention/debugging.
   Do not manually delete these before the agreed lifecycle window unless ops
   has captured enough evidence.
3. `materialized` and `failed_blocked_phash` should have a canonical original
   under `pipeline/originals/<meme_file_id>/...`; their temp object is safe to
   delete and the worker does this idempotently after commit.
4. `pipeline_outbox_events.status=failed` is retryable when `next_retry_at` is
   due. The outbox publisher can claim pending/failed rows generically and does
   not care whether the event is `media_inspect_requested` or `meme_created`.
5. `pipeline_outbox_events.status=publishing` older than the publisher lease
   window indicates a crashed publisher. Run the recovery path to mark those
   rows failed with `next_retry_at=now`, then resume the publisher.

## Wiping the artifact directory

Each run creates a new `<run-id>` subdirectory, so old runs do not overwrite
new ones. When the directory grows too large:

```bash
rm -rf .artifacts/s02-runtime-smoke
```

`.artifacts/` is gitignored; artifacts must never be committed.

## Common failure modes

- **`GET /health` returns 503**: the native API process is not running, or
  Postgres/Redis are unhealthy. Check `docker compose ps` and the API log.
- **Items stall at `embed`**: Voyage API key is missing, expired, or
  rate-limited. Check `item_reports[*].failure_reason` — expected values are
  `embed_provider_blocked`, `embed_timeout`, or `embed_similarity_timeout`.
  All of these are replayable once the upstream recovers.
- **Items stall at `classify`**: classification provider is unreachable. The
  reason code will be `classify_provider_blocked`.
- **Items dead-letter instead of blocking**: the provider returned a
  structurally malformed response. The run summary marks these as
  `outcome: failed` (not `blocked`) because the heavy chain refuses to
  replay a non-retryable failure.
- **Uploads stay `media_inspect_pending`**: the transactional outbox publisher
  is not running or cannot publish to RabbitMQ. Inspect `pipeline_outbox_events`
  for due `pending`/`failed` rows and restart the publisher/recovery loop.
- **Uploads stay `media_inspecting`**: a media-inspect worker likely died while
  holding work. Confirm no worker is still processing the temp object, then
  reset/requeue according to the incident runbook.
- **Invalid media temp objects accumulate**: this is expected until the storage
  lifecycle policy expires retained invalid uploads. Verify the failed request
  has `failure_code=invalid_media` before deleting early.
- **`stage_counts.merge_count` is unexpectedly high**: similarity threshold
  drift or a corrupted embedding cache. Inspect `MemeMergeLog` and the
  enriched detail route's `merge` projection before bumping the threshold.

## Related references

- `scripts/verify_s02_runtime.py` — the harness implementation.
- `memexpert/services/content_pipeline_reporting.py` — the aggregation and
  rendering helpers the harness (and tests) share.
- `memexpert/schemas/content_pipeline.py` — authoritative schemas for the
  enriched detail projections and run summary payload.
- `docs/ops/content-pipeline-smoke.md` — the S01 upload/replay/duplicate
  proof the S02 run builds on top of.
