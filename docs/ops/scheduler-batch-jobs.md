# Scheduler Batch Jobs Runbook

This runbook covers the backend scheduler jobs that perform deferred, bounded work outside user request paths:

- `source-engagement-capture` claims due Telegram source posts and enqueues metric refresh work.
- `source-channel-audience-capture` claims due Telegram channels and enqueues
  forward-only subscriber-count observations.
- `materialized-view-refresh` refreshes public trend materialized views and the
  dependent public recommendation-feature view.
- `recommendation-profile-rebuild` rebuilds a bounded batch of dirty long-term
  user taste profiles in PostgreSQL.
- `recommendation-analytics-rollup` idempotently recomputes a bounded UTC-day
  window of dashboard aggregates from authoritative raw interactions.
- `motd` refreshes the deterministic Meme of the Day cache row for the current UTC date.
- `search-index-sync` updates Qdrant and Meilisearch from canonical PostgreSQL state.
- `meilisearch-settings-reconcile` applies the combined published synonym map from PostgreSQL.
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

Meilisearch synonym settings job:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_MEILISEARCH_SETTINGS_RECONCILE_ENABLED` | `true` | Enables durable synonym reconciliation. The job also runs once immediately after the singleton scheduler acquires its advisory lock. |
| `SCHEDULER_MEILISEARCH_SETTINGS_RECONCILE_INTERVAL_SECONDS` | `60` | Periodic desired/observed-state check cadence. |
| `MEILISEARCH_SETTINGS_TASK_TIMEOUT_SECONDS` | `600` | Maximum wait for an asynchronous full settings-replacement task. |
| `MEILISEARCH_URL`, `MEILISEARCH_MASTER_KEY`, `PIPELINE_MEILISEARCH_INDEX_NAME` | see config | Meilisearch target shared with document sync. |

Source engagement and public trend jobs:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_ENABLED` | `true` | Enables the due-source enqueue job. |
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_INTERVAL_SECONDS` | `21600` | APScheduler interval for scanning due source posts. Due times are stored on `meme_sources.next_engagement_check_at`. |
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_BATCH_SIZE` | `100` | Maximum due `meme_sources` rows claimed per run. |
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_PER_SESSION_BATCH_SIZE` | `20` | Maximum due `meme_sources` rows claimed for one Telegram session in a run, capped by the global batch size. |
| `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_LEASE_TIMEOUT_SECONDS` | `1800` | Lease timeout before an old source-engagement claim can be reclaimed. |
| `PIPELINE_BROKER_SOURCE_ENGAGEMENT_CAPTURE_QUEUE` | `pipeline.source_engagement_capture` | RabbitMQ queue-name prefix used by worker-side metric fetchers. The worker declares per-session queues such as `pipeline.source_engagement_capture.<session_key>` with `x-single-active-consumer=true` and exact session routing keys. |
| `SCHEDULER_SOURCE_CHANNEL_AUDIENCE_CAPTURE_ENABLED` | `true` | Enables the due-channel audience enqueue job. |
| `SCHEDULER_SOURCE_CHANNEL_AUDIENCE_CAPTURE_INTERVAL_SECONDS` | `3600` | APScheduler polling interval. A terminal `success` or `not_exposed` outcome advances the channel to a daily UTC slot with deterministic per-channel jitter; a `failed` outcome remains due for a later poll. |
| `SCHEDULER_SOURCE_CHANNEL_AUDIENCE_CAPTURE_BATCH_SIZE` | `100` | Maximum due `source_channels` rows claimed per run. |
| `SCHEDULER_SOURCE_CHANNEL_AUDIENCE_CAPTURE_PER_SESSION_BATCH_SIZE` | `20` | Per-Telegram-session claim cap within the global batch. |
| `SCHEDULER_SOURCE_CHANNEL_AUDIENCE_CAPTURE_LEASE_TIMEOUT_SECONDS` | `1800` | Lease timeout before an abandoned audience claim can be reclaimed. |
| `PIPELINE_BROKER_SOURCE_CHANNEL_AUDIENCE_CAPTURE_QUEUE` | `pipeline.source_channel_audience_capture` | Per-session main/retry queue prefix for the existing Telegram worker role. |
| `SCHEDULER_MATERIALIZED_VIEW_REFRESH_ENABLED` | `true` | Enables the public trend MV refresh job. |
| `SCHEDULER_MATERIALIZED_VIEW_REFRESH_INTERVAL_SECONDS` | `300` | Refresh cadence for the derived public trend read models. |

Recommendation profile job:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_RECOMMENDATION_PROFILE_REBUILD_ENABLED` | `true` | Enables bounded rebuilds of dirty PostgreSQL recommendation profiles. |
| `SCHEDULER_RECOMMENDATION_PROFILE_REBUILD_INTERVAL_SECONDS` | `300` | APScheduler interval. |
| `SCHEDULER_RECOMMENDATION_PROFILE_REBUILD_BATCH_SIZE` | `50` | Maximum dirty users locked with `SKIP LOCKED` and attempted per run. |
| `RECOMMENDATION_LONG_TERM_HALF_LIFE_DAYS` | `90` | Exponential half-life applied to historical high-intent signals; raw history has no cutoff. |
| `RECOMMENDATION_LONG_TERM_SIGNAL_LIMIT` | `500` | Maximum weighted meme signals materialized for one user. |
| `RECOMMENDATION_CLUSTER_ACTIVATION_SIGNALS` | `20` | Distinct strong-positive threshold for deterministic profile clustering. |
| `RECOMMENDATION_CLUSTER_ITERATIONS` / `RECOMMENDATION_CLUSTER_MIN_ITEMS` | `5` / `3` | Spherical clustering iteration bound and minimum retained cluster size. |

Recommendation analytics job:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_RECOMMENDATION_ANALYTICS_ROLLUP_ENABLED` | `true` | Enables the bounded, idempotent daily dashboard rollup. |
| `SCHEDULER_RECOMMENDATION_ANALYTICS_ROLLUP_INTERVAL_SECONDS` | `3600` | Recompute cadence. |
| `SCHEDULER_RECOMMENDATION_ANALYTICS_ROLLUP_LOOKBACK_DAYS` | `2` | UTC dates replaced atomically per run so late browser retries update today and yesterday. |

Meme of the Day job and API refresh:

| variable | default | meaning |
|---|---:|---|
| `SCHEDULER_MOTD_ENABLED` | `true` | Enables the scheduled MOTD cache refresh job. |
| `SCHEDULER_MOTD_INTERVAL_SECONDS` | `86400` | APScheduler interval. The selection key is still the UTC date plus `MOTD_ALGORITHM_VERSION`. |
| `MOTD_ALGORITHM_VERSION` | `motd_v2` | Cache key and attribution algorithm version for the deterministic selector. |
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

Search-index, source-engagement, source-channel-audience, recommendation-profile,
and SEO jobs all emit
`event=scheduler_job_batch_result`, their `job_id`, and `duration_seconds`.
Their work-count fields are job-specific rather than one shared schema.

Search-index and SEO batch jobs use:

| field | meaning |
|---|---|
| `scanned` | Work rows or memes claimed by this run. |
| `updated` | External index updates or SEO page writes that succeeded. |
| `failed` | Claimed items that ended in a recorded failure. |
| `skipped` | Claimed items that could not be finalized because another run changed the row, or SEO generation returned a non-write skip result. |

The source-engagement and source-channel-audience enqueue jobs instead use
`claimed` for due rows and `enqueued` for RabbitMQ outbox messages written. The
audience event also carries `status=completed`, `degraded_mode=false`,
`source_channel_ids`, and `outbox_message_ids`; it does not emit `scanned`,
`updated`, `failed`, or `skipped`. Those internal UUIDs support operator
correlation, while subscriber counts, session credentials, usernames, and error
payloads stay out of the scheduler result. The MOTD job emits the same
`event=scheduler_job_batch_result` with `job_id=motd`, `candidate_count`,
`selected_meme_id`, `reason`, `algorithm_version`, and `refreshed_at`. The
outbox publisher emits the same event with `job_id=rabbitmq-outbox-publisher`,
`recovered`, `claimed`, `published`, `failed`, and `duration_seconds` fields.

The recommendation profile job emits
`job_id=recommendation-profile-rebuild`, `claimed_users`, `rebuilt_users`, and
`failed_users`. It sets `degraded_mode=true` when any user fails. A failed user
remains dirty for a later bounded retry; one failure rolls back only that user's
nested rebuild and does not discard successful users from the same batch.

The recommendation analytics job emits
`job_id=recommendation-analytics-rollup`, the inclusive `start_date` and
`end_date`, and `aggregate_rows`. It never logs raw events, viewer IDs, queries,
tokens, or vectors.

The Meilisearch settings job emits `status`, `changed`, `desired_hash`,
`actual_hash`, `provider_task_uid`, and `revision_count`. Hashes are safe
content fingerprints; logs and admin reads never contain the synonym map or
Meilisearch credentials.

The generic wrapper still emits `scheduler_job_started`, `scheduler_job_succeeded`, and `scheduler_job_failed`. A non-zero `failed` count inside `scheduler_job_batch_result` does not make the scheduler action fail; failures are durable backlog state and are retried by later runs where appropriate.

## Recommendation Profile Work Selection

`user_recommendation_profile_status` is the durable work ledger. Interaction
and durable-preference changes set `dirty_since`; account merge also dirties the
target profile after transferring signals and invalidating both viewers' feed
caches. Persisted profile rows whose embedding model or profile base version no
longer matches current configuration are eligible even when `dirty_since` is
clean; a bounded reconciliation also creates a missing ledger row if needed.
Each scheduler run orders eligible users by `dirty_since`, locks at most
`SCHEDULER_RECOMMENDATION_PROFILE_REBUILD_BATCH_SIZE` ledger rows with `FOR
UPDATE SKIP LOCKED`, and rebuilds each user independently. Serving ignores a
stale vector while this backlog drains.

The rebuild reads current Favorite/Save/Pin state plus indefinitely retained
high-intent events, applies the 90-day half-life, and stores at most 500 weighted
meme signals. Slot zero is the global centroid. With at least 20 distinct strong
positives, deterministic cosine farthest-first initialization and at most five
spherical iterations may also store up to four clusters, dropping clusters
under three items. Profile rows record model/profile versions, counts, weight,
event watermark, vector bytes, and generated time in PostgreSQL; user vectors
are never written to Qdrant.

On success the user's prior signals/profile slots are replaced atomically,
`dirty_since` is cleared, and `last_rebuilt_at`/`event_watermark` advance. On
failure the nested transaction rolls back and `dirty_since` remains set. Watch
the oldest dirty timestamp and repeated `failed_users`, not just whether the
generic scheduler wrapper returned. Missing or invalid item embeddings may
reduce a user's profile slots without deleting their raw events.

Revision `0043` creates dirty status rows for recommendation state available at
migration time after projecting existing events and current collection/pin
rows. Assess that migration separately for the live event volume and normal
deployment ordering; the subsequent scheduler profile rebuild is the bounded
part and drains only 50 users at a time by default. Do not claim the backfill
complete from schema/code presence, and do not run an unbounded live rebuild
without explicit authorization.

## Materialized View Refresh Order

`materialized-view-refresh` runs on the existing five-minute cadence. It
refreshes `public_meme_trends_mv`, tag/template summaries, tag/template point
views, and finally `public_meme_recommendation_features_mv`, because the item
feature view consumes trend ranks. Each view prefers `CONCURRENTLY`; when
PostgreSQL rejects that mode the job logs
`public_trend_mv_concurrent_refresh_fallback` with the view name and retries
that view non-concurrently. A later dependency failure fails the job and leaves
the last successfully materialized state in place for serving.

Monitor feature-view row count against currently public memes with ready primary
files, `refreshed_at` age, provenance/source-quality/technical/platform-response
coverage flags, and exploration-index availability. Missing derived values are
expected to be neutral `0.5` with false coverage, not zero. Home can continue
through the older view during a failed refresh, but rising feature age and broad
coverage loss should block a `personalized_v2` canary expansion.

`recommendation-analytics-rollup` deletes and replaces only its configured
inclusive UTC-date window in one transaction. Rows are grouped by surface,
algorithm/profile version, and candidate source, with impression, strong-action,
attributed-send, exploration, and impression-level fallback counts. Repeated
contributions for the same typed candidate source on one keyed impression count
once. The JSON metrics include strong/send rates,
`repeat_within_cooldown_count` and rate using the active configured cooldowns,
fallback rate, catalog/long-tail coverage, source/template concentration,
exploration share/conversion, and unique-meme count. Long-tail coverage counts
distinct exposed memes with feature-row popularity below `0.8` against the full
feature-view catalog; monitor feature coverage alongside it.

The schema's `cache_expiry_count` is reserved and this rollup writes it as zero:
cursor expiry is request-path structured telemetry, not an `analytics_events`
fact. Cold-start/provider/Redis breakdowns, candidate counts, filtered ratio,
and latency samples likewise remain structured-log concerns. An empty table can
still mean no eligible attributed events or a disabled/failing job, so
dashboards must also monitor the job timestamp. Raw events remain the audit and
offline-evaluation source; aggregates never replace them.

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

Initial ingest writes an `ingest_initial` snapshot as the baseline. Later
captures are scheduled from the Telegram post date, not ingest time: `+1h`,
`+3h`, `+12h`, `+1d`, `+3d`, `+7d`, `+1month`, then monthly. A missed old
interval is not backfilled with invented deltas; the first observed snapshot
for a source contributes zero historical delta because there is no previous
snapshot to compare. Public activity reads count only increases above each
counter's prior running high, so a decrease and recovery cannot be counted
twice.

Each scheduler run finds due work through the `MemeSource -> SourceChannel -> TelegramSession` FK assignment, skips orphaned or disabled channels and non-runnable Telegram sessions, claims due alive Telegram `meme_sources` rows with `FOR UPDATE SKIP LOCKED`, sets the source engagement lease fields, and writes a `source_engagement_capture_requested` message through the generic RabbitMQ outbox in the same DB transaction. Claiming is capped by both `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_BATCH_SIZE` globally and `SCHEDULER_SOURCE_ENGAGEMENT_CAPTURE_PER_SESSION_BATCH_SIZE` per Telegram session so one session cannot consume the full default run while other sessions have due work. Scheduler-published messages include `telegram_session_id`, `session_name`, and `session_key`, and route as `pipeline.source_engagement_capture.<session_key>`. A session in `flood_wait` is skipped while `flood_wait_until` is in the future; once the cooldown expires, the scheduler clears the session flood-wait state as it claims work so the later runtime can load an active session. Worker startup discovers engagement-enabled Telegram sessions and declares a single-active RabbitMQ main queue plus retry queue for each session key; this isolates in-flight captures per session while allowing different sessions to run independently. Runtime source-engagement captures reuse the `TelegramSessionManager` cached client for the event session. If Telegram returns FloodWait, the worker parks only that `telegram_sessions` row, clears the source lease, ACKs the message, and does not write a failed source snapshot. Source-level failures such as missing or inaccessible posts still write scheduled snapshots.

Snapshot NULLs mean "Telegram did not expose this counter" and are preserved
in canonical storage. Public trend ranking may coalesce unknown to `0`; the
public meme source/analytics DTOs preserve null plus coverage. `forward_count`
is Telegram's public forward/repost count and maps to public
`latest_source_reposts`; it is unrelated to `forwarded_from_*` attribution on
forwarded messages.

## Source Channel Audience Work Selection

`source_channel_audience_snapshots` is durable forward-only observation history
keyed by channel/slot/reason; the system never reconstructs subscriber counts
before collection began. A failed slot remains retryable, but the first
terminal `success` or `not_exposed` observation for a slot is immutable.
Public-source resolution records `initial_resolution`, the crawler's metadata
refresh records `crawler_refresh`, and this scheduler dispatches `scheduled`
daily slots. All three paths call Telegram `channels.getFullChannel`; a missing
participant count is stored as `not_exposed`, while known zero is a successful
`0` observation.

The scheduler polls hourly by default. It claims active, unpaused Telegram
channels with engagement enabled, an assigned enabled/engagement-enabled
runnable session, a due `next_audience_capture_at` (or no schedule yet), and no
live lease newer than the timeout. Claims use `FOR UPDATE SKIP LOCKED`, global
and per-session caps, and the transactional RabbitMQ outbox. Each message is
routed to `pipeline.source_channel_audience_capture.<session_key>`; the existing
Telegram worker role declares single-active main and retry queues per session
and reuses its session manager/rate limiter. No separate container is required.
Before calling Telegram, the worker locks and revalidates that the queued event
still owns the channel's current session and due time and that the channel is
still active, unpaused, and engagement-enabled. It repeats the same fence after
the RPC, so a concurrent pause, reassignment, disable, or reschedule discards
the result without clearing or overwriting the newer lease/schedule.

Session-affined queue keys are discovered only when the Telegram worker starts.
After adding or enabling a Telegram session, or changing its name, restart the
`memexpert-worker-telegram.service` worker through the normal systemd/Reploy
workflow before allowing scheduled dispatch. Until the new binding exists,
mandatory RabbitMQ publication is unroutable and the durable outbox keeps
retrying it. Adding or reassigning a channel to a session whose queue was
already discovered does not require this restart.

Both `success` and `not_exposed` are terminal for a scheduled slot. The first
terminal row persisted for
`(source_channel_id, capture_slot, capture_reason)` is immutable, and a later
same-slot attempt returns that row unchanged. A terminal result clears the
matching lease and advances to the next daily UTC time with stable
channel-specific jitter. A success also refreshes the
`SourceChannel.subscriber_count` latest-success cache; `not_exposed` preserves
that cache. Only a `failed` row remains retryable: a later same-slot attempt may
replace it with another `failed`, `success`, or `not_exposed` result. Failure
clears the lease but leaves the original due time in place, so a later hourly
scheduler poll retries that slot rather than fabricating another sample. Flood
wait is different again: it parks only the affected Telegram session, clears
the channel lease, and writes no failed audience snapshot; healthy sessions
continue. Revoked authorization and permanent bans likewise write no retryable
audience snapshot: they mark the session `auth_required` or `quarantined`,
invalidate its cached worker client, and leave the due slot dormant until an
operator repairs or replaces the non-runnable session. None of these
non-success paths clears a previously valid subscriber cache.

Inspect stale/missing audience state without printing credentials or raw
container environment:

```sql
SELECT
  id,
  title,
  subscriber_count,
  subscriber_count_updated_at,
  last_audience_capture_at,
  next_audience_capture_at,
  audience_capture_attempt_count,
  last_audience_error_code
FROM source_channels
WHERE platform = 'telegram'
ORDER BY next_audience_capture_at NULLS FIRST
LIMIT 100;

SELECT
  source_channel_id,
  captured_at,
  capture_reason,
  fetch_status,
  subscriber_count,
  error_code
FROM source_channel_audience_snapshots
ORDER BY captured_at DESC
LIMIT 100;
```

The public meme API uses only successful observations. It assigns
`audience_at_publish` from the latest success at or before publication when no
more than 48 hours old, exposes current/latest counts with coverage, and never
calls a sum of channel subscribers reach or unique audience.

## Meilisearch Synonym Settings Reconciliation

PostgreSQL is authoritative. An admin publish archives the locale's previous
publication, creates a new immutable published revision and fresh draft, then
marks the singleton `search_synonym_sync_states` row pending with the complete
desired locale revision set. It does not call Meilisearch in the request path.

The scheduler loads every published locale snapshot and fails safely if locale
maps contain the same source key with different targets. It recompiles each
authored source and verifies the stored compiler version, compiled map, and
revision hash before trusting it. With no publications it remains idle only if
nothing was previously applied; disappearance after an application is a
failure requiring operator review. It refuses every empty locale snapshot and
an empty combined map, so it can never clear live synonyms because of missing
or invalid database state. For a non-empty map, it reads the current provider
settings and compares canonical hashes. An
equal hash is recorded as synchronized without a write. A mismatch submits one
full asynchronous synonym replacement, stores the provider task UID, waits for
completion, re-reads the settings, and only records success if the observed hash
matches the desired hash.

Failures preserve the last applied hash/revision set and store only bounded,
credential-free diagnostics. A later periodic run retries automatically;
operators can also request an audited, idempotent retry from
`/admin/search/synonyms`. Scheduler state writes compare the exact published
revision generation before changing desired state. If an admin publishes while
a provider task is in flight, the stale completion cannot overwrite the newer
pending generation and the same job immediately runs another convergence pass.
Each job run closes its Meilisearch SDK client and HTTP connection pool.

## Search-Index Work Selection

The durable work/status table is `meme_file_sync_target_snapshots`, keyed by `(meme_file_id, sync_target)`.

Claimable work includes:

- Missing snapshot rows for `meme_files.status = 'ready'`.
- Snapshot rows with `status in ('pending', 'failed')`.
- `processing` rows whose `last_attempt_at` is older than `SCHEDULER_SEARCH_INDEX_SYNC_PROCESSING_TIMEOUT_SECONDS`.
- `synced` rows whose canonical search-index clock is newer than `last_success_at`.
- Qdrant `synced` rows whose typed payload preview has a missing or stale
  `is_primary_file` value relative to the current `Meme.primary_file_id`.

The canonical clock includes `memes.updated_at`, `meme_files.updated_at`, SEO generated/edited timestamps, template updates, collection updates, collection membership rows, and collection-meme membership timestamps. Search payload `popularity_score` is derived at rebuild time from source engagement snapshots plus `analytics_events`; it is not a stored canonical meme column. Backlog counts and oldest-lag age also include the stale Qdrant primary-file payload contract above, even if its last successful sync is newer than the canonical clock, so the bounded scheduler can backfill old points without an unrelated row update.

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

Inspect bounded recommendation-profile backlog and feature freshness:

```sql
SELECT
  count(*) AS dirty_users,
  min(dirty_since) AS oldest_dirty_since,
  max(last_rebuilt_at) AS latest_rebuild
FROM user_recommendation_profile_status
WHERE dirty_since IS NOT NULL;

SELECT
  status.user_id,
  status.dirty_since,
  status.last_rebuilt_at,
  status.event_watermark,
  count(profile.id) AS profile_slots
FROM user_recommendation_profile_status status
LEFT JOIN user_recommendation_profiles profile ON profile.user_id = status.user_id
WHERE status.dirty_since IS NOT NULL
GROUP BY status.user_id, status.dirty_since, status.last_rebuilt_at, status.event_watermark
ORDER BY status.dirty_since
LIMIT 100;

SELECT
  count(*) AS feature_rows,
  min(refreshed_at) AS oldest_refresh,
  max(refreshed_at) AS newest_refresh,
  count(*) FILTER (WHERE (coverage_flags ->> 'source_quality')::boolean) AS source_quality_covered,
  count(*) FILTER (WHERE (coverage_flags ->> 'platform_response')::boolean) AS response_covered
FROM public_meme_recommendation_features_mv;
```

These reads expose UUIDs and aggregate coverage only. Do not select vectors,
raw analytics payloads, queries, attribution tokens, or credentials into
operator logs/chat.

Inspect synonym publications and their durable sync state:

```sql
SELECT
  id,
  status,
  desired_hash,
  applied_hash,
  actual_hash,
  desired_revision_ids,
  applied_revision_ids,
  provider_task_uid,
  left(last_error, 500) AS last_error_sample,
  requested_at,
  last_attempt_at,
  last_success_at,
  last_failure_at
FROM search_synonym_sync_states;

SELECT c.locale, r.revision_number, r.status, r.compiler_version,
       r.compiled_hash, r.published_at, r.archived_at
FROM search_synonym_revisions r
JOIN search_synonym_catalogs c ON c.id = r.catalog_id
ORDER BY c.locale, r.revision_number DESC;
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
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN"
```

## Replay And Full Resync Paths

Automatic replay:

- Leave failed search-index snapshots in `failed`; the scheduler will retry them in later bounded runs.
- Leave stale `synced` snapshots alone; the scheduler detects canonical drift and reprocesses them.
- Leave crashed `processing` rows alone unless an operator has confirmed the lease timeout is too high; the scheduler reclaims them after `SCHEDULER_SEARCH_INDEX_SYNC_PROCESSING_TIMEOUT_SECONDS`.
- Leave failed or pending synonym settings state durable; the next settings run retries it. After correcting a catalog or provider issue, an admin may use the audited Retry sync action without republishing.
- Leave failed outbox rows in `failed`; the `rabbitmq-outbox-publisher` job retries them when `next_retry_at` is due.
- Leave stale outbox `publishing` rows alone unless an operator has confirmed the lease timeout is too high; the scheduler recovers them after `SCHEDULER_RABBITMQ_OUTBOX_PUBLISHER_STALE_TIMEOUT_SECONDS`.
- Leave failed recommendation users dirty; the next five-minute bounded run
  retries them. Investigate repeated failures before manually changing status
  or profile rows.
- Leave a failed recommendation-feature MV refresh to the next scheduled run
  after correcting the PostgreSQL/lock issue. Do not substitute direct writes
  into a materialized view.

Manual per-file/per-target replay remains the existing operator API path documented in `docs/ops/content-pipeline-search-sync.md`:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/qdrant/replay" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN"

curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/meili/replay" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN"
```

Those endpoints still queue the pipeline worker per-target replay path; the scheduler job does not remove or replace them.

They also remain operator-token, failure-only, and bounded. Cookie-admin Replay
& Repair is separate: successful or forced stage replay is CSRF-protected and
audited, and broad all-matching work first becomes a resumable `preparing` job.
Its materializer scans in keyset pages, records exact roots/steps/exclusions,
and releases the reviewed result under the same capacity gates.

Full/manual resync:

- For routine drift, let the scheduler advance in bounded chunks. Use the
  operator batch route only for a bounded failure cohort; use cookie-admin
  Replay & Repair for an audited, exact all-matching maintenance job.
- The current scheduler is an incremental catch-up mechanism for the configured
  Qdrant collection. It does not perform alias swaps or whole-index rebuild
  orchestration. A stable read alias, named-vector collection, dual-write,
  bounded backfill, verified atomic switch, and rollback retention are
  evidence-gated Phase-3 work, not an existing routine full-resync path.

## Common Failure Modes

- `sync_qdrant_timeout` / `sync_meili_timeout`: provider timeout. Check engine health and timeout settings; leave failed rows for retry after the provider recovers.
- `sync_qdrant_provider_blocked` / `sync_meili_provider_blocked`: provider unavailable or rejected the write. Check URLs, credentials, and index/collection existence.
- `sync_qdrant_malformed_payload` / `sync_meili_malformed_payload`: payload or provider response shape is invalid. Inspect `last_error_text`; this usually needs a code/config fix rather than repeated replay.
- `meilisearch-settings-reconcile` with `status='failed'`: inspect the bounded `last_error` and scheduler log. Fix cross-locale key conflicts in the draft and republish, or restore provider health and use Retry sync. Never clear provider synonyms manually as a recovery shortcut.
- `recommendation-profile-rebuild` with repeated `failed_users`: inspect safe
  exception context and affected dirty/status timestamps; verify migration head
  and primary image embedding shape. Do not log profile vectors or clear dirty
  flags merely to reduce backlog.
- `materialized-view-refresh` failing at
  `public_meme_recommendation_features_mv`: verify the trend view dependency,
  unique indexes, database capacity/locks, and migration head. Keep serving the
  prior materialization and retry in normal dependency order.
- SEO `failed` result counts with no page written: inspect scheduler logs around the provider warning. In live mode, confirm `PIPELINE_SEO_API_KEY`, model, base URL, and object-storage access for image inputs.
