# Scheduler Batch Jobs Runbook

This runbook covers the backend scheduler jobs that perform deferred, bounded work outside user request paths:

- `search-index-sync` updates Qdrant and Meilisearch from canonical PostgreSQL state.
- `seo-backlog-batches` generates or refreshes public-safe meme SEO pages.

## Run The Scheduler

Run the scheduler as the only active scheduler process in an environment:

```bash
uv run memexpert-scheduler
```

Production deployments should keep `SCHEDULER_ADVISORY_LOCK_ENABLED=true` so a second scheduler exits before registering duplicate jobs. The scheduler logs `scheduler_runtime_started`, one `scheduler_job_started` per job run, one job-specific `scheduler_job_batch_result` for these batch jobs, then `scheduler_job_succeeded` or `scheduler_job_failed` from the generic wrapper.

## Relevant Env Vars

Search-index job:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_SEARCH_INDEX_SYNC_ENABLED` | `true` | Enables the scheduled search-index batch job. |
| `SCHEDULER_SEARCH_INDEX_SYNC_INTERVAL_SECONDS` | `600` | APScheduler interval. |
| `SCHEDULER_SEARCH_INDEX_SYNC_BATCH_SIZE` | `50` | Maximum work rows claimed per target per run. A run may process up to this many Qdrant rows plus this many Meilisearch rows. |
| `SCHEDULER_SEARCH_INDEX_SYNC_PROCESSING_TIMEOUT_SECONDS` | `900` | Lease timeout before an old `processing` snapshot can be reclaimed. |
| `PIPELINE_VOYAGE_OUTPUT_DIMENSIONS` | `1024` | Vector dimensions used when loading cached embeddings for Qdrant. |
| `QDRANT_URL`, `PIPELINE_QDRANT_COLLECTION_NAME`, `PIPELINE_QDRANT_TIMEOUT_SECONDS` | see config | Qdrant write target. |
| `MEILISEARCH_URL`, `MEILISEARCH_MASTER_KEY`, `PIPELINE_MEILISEARCH_INDEX_NAME`, `PIPELINE_MEILISEARCH_TIMEOUT_SECONDS` | see config | Meilisearch write target. |

SEO backlog job:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_SEO_BACKLOG_BATCHES_ENABLED` | `true` | Enables the scheduled SEO backlog batch job. |
| `SCHEDULER_SEO_BACKLOG_BATCHES_INTERVAL_SECONDS` | `900` | APScheduler interval. |
| `SCHEDULER_SEO_BACKLOG_BATCH_SIZE` | `25` | Maximum memes claimed per run. |
| `PIPELINE_SEO_PROVIDER_MODE` | `static` | `static` uses the local no-network provider; `live` uses the OpenAI-compatible provider. |
| `PIPELINE_SEO_PROMPT_VERSION` | `meme-seo-v1` | Stale auto-generated pages are regenerated when their stored prompt version differs. |
| `PIPELINE_SEO_API_BASE_URL`, `PIPELINE_SEO_API_KEY`, `PIPELINE_SEO_MODEL`, `PIPELINE_SEO_TIMEOUT_SECONDS`, `PIPELINE_SEO_MAX_ATTEMPTS` | see config | Live SEO provider settings. |

## Result Logs

Both jobs emit:

| field | meaning |
|---|---|
| `event` | Always `scheduler_job_batch_result`. |
| `job_id` | `search-index-sync` or `seo-backlog-batches`. |
| `scanned` | Work rows or memes claimed by this run. |
| `updated` | External index updates or SEO page writes that succeeded. |
| `failed` | Claimed items that ended in a recorded failure. |
| `skipped` | Claimed items that could not be finalized because another run changed the row, or SEO generation returned a non-write skip result. |
| `duration_seconds` | Wall-clock seconds spent inside the job action. |

The generic wrapper still emits `scheduler_job_started`, `scheduler_job_succeeded`, and `scheduler_job_failed`. A non-zero `failed` count inside `scheduler_job_batch_result` does not make the scheduler action fail; failures are durable backlog state and are retried by later runs where appropriate.

## Search-Index Work Selection

The durable work/status table is `meme_file_sync_target_snapshots`, keyed by `(meme_file_id, sync_target)`.

Claimable work includes:

- Missing snapshot rows for `meme_files.status = 'ready'`.
- Snapshot rows with `status in ('pending', 'failed')`.
- `processing` rows whose `last_attempt_at` is older than `SCHEDULER_SEARCH_INDEX_SYNC_PROCESSING_TIMEOUT_SECONDS`.
- `synced` rows whose canonical search-index clock is newer than `last_success_at`.

The canonical clock includes `memes.updated_at`, `meme_files.updated_at`, SEO generated/edited timestamps, template updates, collection updates, collection membership rows, and collection-meme membership timestamps. This matches the payload fields that include popularity, tags, template, SEO slug, and collection access hints.

Each claimed row is set to `processing`, `last_attempt_at` is refreshed, and `attempt_count` is incremented before the scheduler calls Qdrant or Meilisearch. The claim transaction commits before the external write so overlapping scheduler runs skip the same target row. Successful writes set `status='synced'`, clear error fields, refresh `last_success_at`, and store a bounded typed preview compatible with the pipeline inspect decoder. Failed writes set `status='failed'`, `normalized_reason`, `last_error_text`, and `last_attempt_at` while preserving the last known good `last_success_at` and preview.

## SEO Work Selection

The SEO job locks one `memes` row at a time with `SELECT ... FOR UPDATE SKIP LOCKED`; it does not preselect a broad unlocked batch.

Priority order:

1. Public, non-NSFW memes with no `meme_seo_pages` row.
2. Public, non-NSFW memes with an auto-generated SEO page whose `prompt_version` differs from `PIPELINE_SEO_PROMPT_VERSION` and `edited_at IS NULL`.
3. Within each class, higher `popularity_score` first, then stable creation/id tie-breakers.

Manual pages (`edited_at IS NOT NULL`) are intentionally skipped. For stale auto-generated pages, the job calls `MemeSeoGenerationService` with `force=True`; missing pages use the default non-force path.

## Inspect Failures

Search-index status:

```sql
SELECT
  sync_target,
  status,
  attempt_count,
  normalized_reason,
  left(last_error_text, 500) AS last_error_sample,
  last_attempt_at,
  last_success_at
FROM meme_file_sync_target_snapshots
WHERE meme_file_id = '<meme_file_id>'
ORDER BY sync_target;
```

Find current failed search-index backlog:

```sql
SELECT meme_file_id, sync_target, attempt_count, normalized_reason, last_attempt_at
FROM meme_file_sync_target_snapshots
WHERE status = 'failed'
ORDER BY last_attempt_at DESC
LIMIT 100;
```

Find old processing leases that are eligible soon or already eligible:

```sql
SELECT meme_file_id, sync_target, attempt_count, last_attempt_at
FROM meme_file_sync_target_snapshots
WHERE status = 'processing'
ORDER BY last_attempt_at NULLS FIRST
LIMIT 100;
```

SEO backlog size:

```sql
SELECT count(*) AS missing_public_safe_seo_pages
FROM memes m
LEFT JOIN meme_seo_pages seo ON seo.meme_id = m.id
WHERE m.is_public IS TRUE
  AND m.is_nsfw IS FALSE
  AND seo.meme_id IS NULL;

SELECT count(*) AS stale_auto_generated_seo_pages
FROM memes m
JOIN meme_seo_pages seo ON seo.meme_id = m.id
WHERE m.is_public IS TRUE
  AND m.is_nsfw IS FALSE
  AND seo.prompt_version <> '<PIPELINE_SEO_PROMPT_VERSION>'
  AND seo.edited_at IS NULL;
```

For API-level inspect, use the existing pipeline item detail endpoint:

```bash
curl "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/detail" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN"
```

## Replay And Full Resync Paths

Automatic replay:

- Leave failed search-index snapshots in `failed`; the scheduler will retry them in later bounded runs.
- Leave stale `synced` snapshots alone; the scheduler detects canonical drift and reprocesses them.
- Leave crashed `processing` rows alone unless an operator has confirmed the lease timeout is too high; the scheduler reclaims them after `SCHEDULER_SEARCH_INDEX_SYNC_PROCESSING_TIMEOUT_SECONDS`.

Manual per-file/per-target replay remains the existing operator API path documented in `docs/ops/content-pipeline-search-sync.md`:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/qdrant/replay" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN"

curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/meili/replay" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN"
```

Those endpoints still queue the pipeline worker per-target replay path; the scheduler job does not remove or replace them.

Full/manual resync:

- For a broad Meilisearch catch-up, let the scheduler advance in bounded chunks or use the existing per-target batch replay endpoint in operator-sized chunks.
- For a full Qdrant alias rebuild, keep using the existing manual/full-resync procedure for rebuilding the Qdrant collection/alias. The scheduler job is an incremental catch-up mechanism; it does not perform alias swaps or whole-index rebuild orchestration.

## Common Failure Modes

- `sync_qdrant_timeout` / `sync_meili_timeout`: provider timeout. Check engine health and timeout settings; leave failed rows for retry after the provider recovers.
- `sync_qdrant_provider_blocked` / `sync_meili_provider_blocked`: provider unavailable or rejected the write. Check URLs, credentials, and index/collection existence.
- `sync_qdrant_malformed_payload` / `sync_meili_malformed_payload`: payload or provider response shape is invalid. Inspect `last_error_text`; this usually needs a code/config fix rather than repeated replay.
- SEO `failed` result counts with no page written: inspect scheduler logs around the provider warning. In live mode, confirm `PIPELINE_SEO_API_KEY`, model, base URL, and object-storage access for image inputs.
