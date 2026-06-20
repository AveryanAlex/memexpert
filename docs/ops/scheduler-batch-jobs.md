# Scheduler Batch Jobs Runbook

This runbook covers the backend scheduler jobs that perform deferred, bounded work outside user request paths:

- `source-engagement-capture` claims due Telegram source posts and enqueues metric refresh work.
- `materialized-view-refresh` refreshes public trend materialized views derived from source engagement snapshots and analytics events.
- `motd` refreshes the deterministic Meme of the Day cache row for the current UTC date.
- `search-index-sync` updates Qdrant and Meilisearch from canonical PostgreSQL state.
- `seo-backlog-batches` generates or refreshes public-safe meme SEO pages.
- `rabbitmq-outbox-publisher` publishes durable RabbitMQ outbox messages.

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

Source engagement and public trend jobs:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_ENABLED` | `true` | Enables the due-source enqueue job. |
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_INTERVAL_SECONDS` | `21600` | APScheduler interval for scanning due source posts. Due times are stored on `meme_sources.next_engagement_check_at`. |
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_BATCH_SIZE` | `100` | Maximum due `meme_sources` rows claimed per run. |
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_LEASE_TIMEOUT_SECONDS` | `1800` | Lease timeout before an old source-engagement claim can be reclaimed. |
| `PIPELINE_BROKER_SOURCE_ENGAGEMENT_CAPTURE_QUEUE` | `pipeline.source_engagement_capture` | RabbitMQ queue used by worker-side metric fetchers. |
| `SCHEDULER_MATERIALIZED_VIEW_REFRESH_ENABLED` | `true` | Enables the public trend MV refresh job. |
| `SCHEDULER_MATERIALIZED_VIEW_REFRESH_INTERVAL_SECONDS` | `300` | Refresh cadence for the derived public trend read models. |

Meme of the Day job and API refresh:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_MOTD_ENABLED` | `true` | Enables the scheduled MOTD cache refresh job. |
| `SCHEDULER_MOTD_INTERVAL_SECONDS` | `86400` | APScheduler interval. The selection key is still the UTC date plus `MOTD_ALGORITHM_VERSION`. |
| `MOTD_ALGORITHM_VERSION` | `motd_v1` | Cache key and attribution algorithm version for the deterministic selector. |
| `MOTD_CANDIDATE_LOOKBACK_DAYS` | `30` | Recent-candidate window. Today's UTC selection ends at current UTC time; past/future requested dates end at the requested UTC date's next midnight. |
| `MOTD_CANDIDATE_LIMIT` | `50` | Maximum candidate rows scored per refresh. |
| `MOTD_MIN_QUALITY_SCORE` | `0.5` | Minimum `meme_files.quality_score` on the meme's primary file. |
| `MOTD_POPULARITY_WEIGHT` | `0.35` | Weight for `public_meme_trends_mv.latest_popularity_score` after log scaling. |
| `MOTD_TRENDING_GROWTH_WEIGHT` | `0.30` | Weight for positive recent-vs-previous trend growth from `public_meme_trends_mv` event-count columns after log scaling. |
| `MOTD_NOVELTY_WEIGHT` | `0.20` | Weight for recency within the configured lookback window. |
| `MOTD_QUALITY_WEIGHT` | `0.15` | Weight for primary-file quality. |

`GET /api/v1/memes/meme-of-the-day` is public and returns today's cached row, lazily refreshing when the row is missing. `POST /api/v1/memes/meme-of-the-day/refresh` requires `AdminUserDep` and recomputes the deterministic row; it is manual refresh only, not manual/admin override. Admin override remains deferred and no override model or endpoint exists.

SEO backlog job:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_SEO_BACKLOG_BATCHES_ENABLED` | `true` | Enables the scheduled SEO backlog batch job. |
| `SCHEDULER_SEO_BACKLOG_BATCHES_INTERVAL_SECONDS` | `900` | APScheduler interval. |
| `SCHEDULER_SEO_BACKLOG_BATCH_SIZE` | `25` | Maximum memes claimed per run. |
| `PIPELINE_SEO_PROVIDER_MODE` | `static` | `static` uses the local no-network provider; `live` uses the OpenAI-compatible provider. |
| `PIPELINE_SEO_PROMPT_VERSION` | `meme-seo-v1` | Stale auto-generated pages are regenerated when their stored prompt version differs. |
| `PIPELINE_SEO_API_BASE_URL`, `PIPELINE_SEO_API_KEY`, `PIPELINE_SEO_MODEL`, `PIPELINE_SEO_TIMEOUT_SECONDS`, `PIPELINE_SEO_MAX_ATTEMPTS` | see config | Live SEO provider settings. |

RabbitMQ outbox publisher job:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_ENABLED` | `true` | Enables the scheduled transactional RabbitMQ outbox publisher. |
| `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_INTERVAL_SECONDS` | `5` | APScheduler interval. |
| `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_BATCH_SIZE` | `100` | Maximum due outbox rows claimed per run. |
| `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_STALE_TIMEOUT_SECONDS` | `300` | Lease timeout before an old `publishing` outbox row is recovered. |

## Result Logs

Search-index, source-engagement, and SEO jobs emit:

| field | meaning |
|---|---|
| `event` | Always `scheduler_job_batch_result`. |
| `job_id` | `search-index-sync`, `source-engagement-capture`, or `seo-backlog-batches`. |
| `scanned` | Work rows or memes claimed by this run. |
| `updated` | External index updates or SEO page writes that succeeded. |
| `failed` | Claimed items that ended in a recorded failure. |
| `skipped` | Claimed items that could not be finalized because another run changed the row, or SEO generation returned a non-write skip result. |
| `duration_seconds` | Wall-clock seconds spent inside the job action. |

The source-engagement job uses `claimed` for due source rows and `enqueued` for RabbitMQ outbox messages written. The MOTD job emits the same `event=scheduler_job_batch_result` with `job_id=motd`, `candidate_count`, `selected_meme_id`, `reason`, `algorithm_version`, and `refreshed_at`. The outbox publisher emits the same event with `job_id=rabbitmq-outbox-publisher`, `recovered`, `claimed`, `published`, `failed`, and `duration_seconds` fields.

The generic wrapper still emits `scheduler_job_started`, `scheduler_job_succeeded`, and `scheduler_job_failed`. A non-zero `failed` count inside `scheduler_job_batch_result` does not make the scheduler action fail; failures are durable backlog state and are retried by later runs where appropriate.

## Meme Of The Day Work Selection

The durable cache table is `meme_of_the_day_selections`, unique on `(selected_for, algorithm_version)`. Refreshes upsert the current UTC date for scheduled runs, or the requested date for service/API callers.

Candidate filters are intentionally public-safe: `memes.is_public IS TRUE`, `memes.is_nsfw IS FALSE`, primary file quality at or above `MOTD_MIN_QUALITY_SCORE`, and `memes.created_at` inside the configured lookback window. For the current UTC date, the upper bound is current UTC time so future-created rows cannot win today's MOTD; past and future requested dates use the deterministic full UTC date window. The selector left-joins `public_meme_trends_mv`; candidates without an MV row remain eligible with popularity/trend inputs treated as `0`.

Scoring uses weighted popularity, positive trending growth, recency/novelty, and quality. Selection is deterministic: score descending, then newest `created_at`, then meme id. No random selection is used. If no candidate qualifies, the job stores a fallback row with `meme_id=NULL`, `candidate_count=0`, and `reason='no_candidates'`; the public API returns `meme: null` and no attribution for that row.

When a meme is selected, the API response includes `MemeResultAttributionRead` with `source_algorithm='motd'`, `surface='web_home'`, `rank=1`, `algorithm_version`, `score`, `score_components`, and `reason`. Frontend impression/click/action telemetry should forward this attribution unchanged.

## RabbitMQ Outbox Work Selection

The durable work table is `rabbitmq_outbox_messages`. API raw-upload acceptance writes `media_inspect_requested` rows in the same transaction as `pipeline_ingest_requests`; worker materialization and stage services write dispatch/replay/sync-success messages in the same transaction as `meme_files` and stage-journal state.

Each publisher run opens a scheduler DB session, starts or reuses the RabbitMQ pipeline broker, recovers stale rows where `status='publishing'` and `locked_at` is older than `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_STALE_TIMEOUT_SECONDS`, then claims due rows where `status in ('pending', 'failed')` and `next_retry_at IS NULL OR next_retry_at <= now()`. Claims are locked with `FOR UPDATE SKIP LOCKED`, set to `publishing` with `locked_at`/`lock_owner`, and committed before the broker publish.

The publisher sends the stored JSON payload to the row's stored `exchange` and `routing_key` with stored headers, content type, and stable `message_id`; it does not branch on event type. Successful publishes set `status='published'`, `published_at`, and clear retry/lease metadata. Broker failures set `status='failed'`, incremented attempt metadata is preserved, `last_error_text` is recorded, and `next_retry_at` is scheduled from `PIPELINE_BROKER_RETRY_BACKOFF_SECONDS`.

## Source Engagement Work Selection

The canonical volatile source counters live in append-only `meme_source_engagement_snapshots`. `meme_sources` stores provenance and scheduling state only: platform identity, `published_at`, `next_engagement_check_at`, lease fields, and source liveness. It does not store Telegram view/reaction/forward totals.

Initial ingest writes an `ingest_initial` snapshot as the baseline. Later captures are scheduled from the Telegram post date, not ingest time: `+1h`, `+3h`, `+12h`, `+1d`, `+3d`, `+7d`, `+1month`, then monthly. A missed old interval is not backfilled with invented deltas; the first observed snapshot for a source contributes zero historical delta because there is no previous snapshot to compare.

Each scheduler run claims due, alive Telegram `meme_sources` rows with `FOR UPDATE SKIP LOCKED`, sets the source engagement lease fields, and writes a `source_engagement_capture_requested` message through the generic RabbitMQ outbox in the same DB transaction. Worker execution happens later from RabbitMQ; that worker fetches Telegram metadata and appends or updates the scheduled snapshot row.

Snapshot NULLs mean "Telegram did not expose this counter" and are preserved in canonical storage. Public trend/read-model queries may coalesce unknown to 0 for ranking and summaries only. `forward_count` is Telegram's public forward/repost count and maps to public `latest_source_reposts`; it is unrelated to `forwarded_from_*` attribution on forwarded messages.

## Search-Index Work Selection

The durable work/status table is `meme_file_sync_target_snapshots`, keyed by `(meme_file_id, sync_target)`.

Claimable work includes:

- Missing snapshot rows for `meme_files.status = 'ready'`.
- Snapshot rows with `status in ('pending', 'failed')`.
- `processing` rows whose `last_attempt_at` is older than `SCHEDULER_SEARCH_INDEX_SYNC_PROCESSING_TIMEOUT_SECONDS`.
- `synced` rows whose canonical search-index clock is newer than `last_success_at`.

The canonical clock includes `memes.updated_at`, `meme_files.updated_at`, SEO generated/edited timestamps, template updates, collection updates, collection membership rows, and collection-meme membership timestamps. Search payload `popularity_score` is derived at rebuild time from source engagement snapshots plus `analytics_events`; it is not a stored canonical meme column.

Each claimed row is set to `processing`, `last_attempt_at` is refreshed, and `attempt_count` is incremented before the scheduler calls Qdrant or Meilisearch. The claim transaction commits before the external write so overlapping scheduler runs skip the same target row. Successful writes set `status='synced'`, clear error fields, refresh `last_success_at`, and store a bounded typed preview compatible with the pipeline inspect decoder. Failed writes set `status='failed'`, `normalized_reason`, `last_error_text`, and `last_attempt_at` while preserving the last known good `last_success_at` and preview.

## SEO Work Selection

The SEO job locks one `memes` row at a time with `SELECT ... FOR UPDATE SKIP LOCKED`; it does not preselect a broad unlocked batch.

Priority order:

1. Public, non-NSFW memes with no `meme_seo_pages` row.
2. Public, non-NSFW memes with an auto-generated SEO page whose `prompt_version` differs from `PIPELINE_SEO_PROMPT_VERSION` and `edited_at IS NULL`.
3. Within each class, stable creation/id tie-breakers keep claims deterministic.

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

Outbox backlog and failures:

```sql
SELECT status, count(*)
FROM rabbitmq_outbox_messages
GROUP BY status
ORDER BY status;

SELECT id, event_type, exchange, routing_key, status, attempt_count, next_retry_at, left(last_error_text, 500) AS last_error_sample
FROM rabbitmq_outbox_messages
WHERE status IN ('pending', 'failed', 'publishing')
ORDER BY created_at
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
- Leave failed outbox rows in `failed`; the `rabbitmq-outbox-publisher` job retries them when `next_retry_at` is due.
- Leave stale outbox `publishing` rows alone unless an operator has confirmed the lease timeout is too high; the scheduler recovers them after `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_STALE_TIMEOUT_SECONDS`.

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
