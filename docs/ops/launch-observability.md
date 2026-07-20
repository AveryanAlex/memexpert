# Launch Observability

This launch stage uses structured application logs and existing operator/read paths. It does not add Prometheus, Grafana, tracing backends, metrics endpoints, dashboards, or a new long-running service.

## Safety Rules

- Search and recommendation logs never include raw query text. Use `query_present`, `query_length`, `surface`, `scope`, `tag_count`, `collection_count`, and `filter_count`.
- Never log signed `attribution_token` or feed cursor values, decoded claims,
  Redis pool bodies, profile vectors, or raw candidate embeddings. Use request,
  feed-session, algorithm/profile version, candidate-source, and coarse outcome
  fields instead.
- Security logs never include rate-limit keys, subjects, cookies, tokens, headers, or IP addresses.
- Analytics event write-failure logs include only `event_type`, `user_id` when present, `payload_key_count`, and `payload_keys`; payload values are not logged.
- Provider failure logs include coarse reason fields and exception type only, not provider credentials or raw upstream payloads.

## Local And Staging Inspection

For local API log inspection, run the API directly and watch stdout:

```sh
uv run memexpert-api
```

For local scheduler job-health and backlog fields, run the scheduler directly and watch stdout:

```sh
uv run memexpert-scheduler
```

For Docker Compose-style staging stacks, follow the app container logs for each process separately:

```sh
docker compose logs -f api
docker compose logs -f scheduler
docker compose logs -f worker-media worker-ocr worker-enrichment worker-sync worker-telegram
```

Useful safe search terms for plain-text log tools or hosted log search are:

- `meme_search_completed`, `meme_recommendation_completed`, and `meme_similar_completed`.
- `meme_search_provider_failure`, `meme_recommendation_provider_failure`, and `meme_similar_provider_failure`.
- `analytics_event_write_failed` and `security_rate_limit_degraded`.
- `scheduler_job_failed` and `scheduler_job_batch_result`.
- `job_id=search-index-sync` and `job_id=rabbitmq-outbox-publisher`.

When correlating a user-visible degraded response, search by the response `request_id` instead of query text. If logs are JSON-formatted, filter on fields like `event`, `request_id`, `job_id`, and `degraded_mode`; if logs are plain text, search for the event names above and then inspect the structured fields printed with the record.

## Scheduler Job Health

Inspect `memexpert-scheduler` logs for:

- `event=scheduler_job_started` with `job_id`, `status=started`, `degraded_mode=false`.
- `event=scheduler_job_succeeded` with `job_id`, `status=succeeded`, `duration_seconds`, `degraded_mode=false`.
- `event=scheduler_job_failed` with `job_id`, `status=failed`, `duration_seconds`, `degraded_mode=true`.
- `event=scheduler_job_batch_result` for batch jobs. These include job-specific counts and `degraded_mode=true` when a batch reports failures or durable backlog remains.

Useful job ids are `search-index-sync`, `rabbitmq-outbox-publisher`,
`source-engagement-capture`, `seo-backlog-batches`,
`materialized-view-refresh`, `recommendation-profile-rebuild`, and
`recommendation-analytics-rollup`. The profile
job reports `claimed_users`, `rebuilt_users`, and `failed_users`; repeated dirty
rows or a growing oldest `dirty_since` indicate rebuild lag even when one batch
finishes normally.
The analytics rollup reports its UTC date window and row count; alert on missed
hourly runs before interpreting an empty daily aggregate as zero traffic.

## Search And Recommendations

For a degraded search or recommendation response, start from the response `request_id` and search API logs for the same field:

- `event=meme_search_completed` for search responses.
- `event=meme_recommendation_completed` for recommendation responses.
- `event=recommendation_candidate_generation_completed` for personalized-v2
  source counts, bounded stage sizes, filtered ratio, stage timings,
  `cold_start`, `qdrant_degraded`, `reason`, and `fallback_category`.
- `event=recommendation_home_page_completed` for pool/fallback cache status,
  served/configured algorithm versions, normalized profile version, returned
  count, continuation state, fallback category, Redis preflight/pool timings,
  and end-to-end latency.
- `event=recommendation_page_hydration_completed` for `frozen_pool` versus
  `postgres_trending` page mode. Frozen-pool pages expose authorization,
  hydration, and total timing; PostgreSQL fallback pages expose fallback-ID
  query, hydration, and total timing.
- `reason=redis_preflight_unavailable` when the first-page read preflight falls
  back before profile/Qdrant generation; distinguish this from
  `pool_freeze_redis_unavailable` and
  `pool_continuation_redis_unavailable`.
- `event=recommendation_feed_cursor_expired` for the machine-readable 410 path;
  it carries the configured algorithm, normalized `profile_version=none`, cache
  status, reason, and `fallback_category=cache_expiry`.
- `event=recommendation_feed_cursor_invalid` for the separate 422 path with
  `fallback_category=invalid_cursor`.
- `event=recommendation_shadow_completed` or
  `event=recommendation_shadow_failed` for bounded shadow work; the latter
  includes the safe exception type and configured timeout, not candidate data.
- `event=qdrant_best_score_recommendation_degraded` when the multi-positive
  batch fails, including from a stale/missing positive seed; nearest-vector
  source results remain usable. Its fields are exception type and source count
  only.
- `event=recommendation_qdrant_degraded` when the recommendation adapter fails
  as a whole; it carries surface, algorithm/profile version, safe reason,
  `fallback_category=qdrant_provider`, and exception type.
- `event=telegram_inline_cursor_unavailable` when the current empty-query page
  was returned but Redis could not persist its next-offset handle; that page is
  terminal and the log contains no handle or viewer identity.
- `event=meme_similar_completed` for similar-meme responses.
- `event=meme_search_provider_failure`, `event=meme_recommendation_provider_failure`, or `event=meme_similar_provider_failure` for provider fallback context.

Where both fields appear, `algorithm_version` is the version actually served
while `configured_algorithm_version` identifies the active rollout target.
Missing profiles are normalized to the literal `none`. Candidate-generation source
counts distinguish short-term, current-intent, long-term global/clusters,
multi-positive, trending, and exploration; bounded union/post-filter/rerank/pool
sizes and filtered ratio are emitted with Qdrant, PostgreSQL candidate, fusion,
filter/feature, ranking/diversity, and total timings. Shadow outcome is a
separate event as described above. Do not assume a per-viewer canary bucket is
present in logs; the serving bucket is deterministic from the configured
percentage and must not be reconstructed by exporting viewer IDs.

Do not search logs for raw query text. Use `query_present=true` and `query_length` to confirm whether a query existed without exposing private search text.

### Recommendation quality and safety

Daily aggregate rows group trusted keyed impressions by UTC date, surface,
algorithm version, profile version, and typed candidate source. Repeated
contributions with the same typed source on one impression are deduplicated.
The rows provide impression/strong-action/attributed-send/exploration/fallback
counts and these JSON metrics:

- strong-action and attributed-send rate per keyed impression;
- `repeat_within_cooldown_count` and rate, using the active configured
  impression and strong-action windows;
- source/template concentration;
- catalog coverage, plus long-tail coverage for exposed items whose feature-row
  popularity quantile is below `0.8`;
- exploration share/conversion, unique-meme count, and impression-level
  fallback rate.

Cold-start and Qdrant/Redis fallback categories, cursor expiry, candidate
counts, filtered ratio, and stage latencies are structured-log metrics, not
daily rollup columns. Compute p50/p95 from those log samples. The reserved
`cache_expiry_count` aggregate column remains `0`; use
`recommendation_feed_cursor_expired` logs for the actual expiry rate. Likewise,
the daily fallback count does not distinguish provider, Redis, rollout-gate, or
cache-expiry causes.

A nonzero cooldown-repeat rate is a correctness alert, not merely a relevance
signal. Investigate exact-state writes/merge semantics and per-page PostgreSQL
rechecks before tuning ranking. Missing derived item features should appear as
neutral `0.5` with false coverage; monitor coverage separately so a broad
backfill gap cannot masquerade as low quality. An item without a recommendation
feature row cannot enter the long-tail numerator, so interpret long-tail
coverage alongside feature-view coverage.

### Qdrant recommendation readiness

Before enabling recommendation traffic against a collection, verify its server
version/digest through the deployment source of truth, all expected payload
indexes, and primary-file payload coverage. The expected index fields are
keyword `search_index_algorithm_version`, `uploader_user_ids`, `media_type`,
`language`, `tags`, `collection_ids`, `collection_owner_user_ids`, and
`collection_member_user_ids`, plus boolean `is_public`, `is_primary_file`, and
`is_nsfw`. Compare READY primary files in PostgreSQL with Qdrant points carrying
`is_primary_file=true`, and sample filter behavior. A code change or migration
file is not proof that beta provisioning/backfill has happened.

### Shadow, canary, and acceptance gates

The runtime gates default to `RECOMMENDATION_ENABLED=false`,
`RECOMMENDATION_SHADOW_MODE=true`, and `RECOMMENDATION_CANARY_PERCENT=0`.
Those defaults serve the PostgreSQL trending fallback and cannot return
`personalized_v2`. For shadow evaluation, explicitly enable the subsystem while
leaving shadow mode on and the canary at zero. Serving requires a separate
change that turns shadow mode off and raises the stable-viewer canary above
zero; expand that percentage only after the readiness checks below pass.
`RECOMMENDATION_SHADOW_TIMEOUT_SECONDS` defaults to `0.25`, bounding shadow
candidate generation well below the provider timeout so fallback latency does
not inherit a slow Qdrant request. Search and Similar retain their versioned
global quality/popularity terms, but their profile-derived taste term obeys the
same serving eligibility gate.

The gate is evaluated for new cursorless Home sessions. Existing signed cursors
are handled first and remain usable until expiry when their algorithm binding
still matches, so lowering the canary or enabling shadow is not an immediate
revocation of already-frozen personalized pools. Treat cache invalidation or an
algorithm-version rollout as a separate state-changing operation requiring the
usual authorization and recovery planning.

Redis operations default to a `0.5`-second timeout. A viewer retains at most four
frozen pools by default; creation of another atomically evicts the oldest pool
body. Treat a resulting typed cursor expiry as bounded retention, not Redis
corruption, unless expiry rates rise beyond expected back-navigation behavior.

Run `personalized_v2` in shadow mode before it affects returned order, then
canary by stable user hash. Compare candidate availability, fallback rate,
safety filters, stage latency, source/template concentration, and offline replay
quality against the baseline. Current beta traffic is too sparse for reliable
online A/B significance; chronological offline replay chooses the algorithm and
the canary limits operational risk.

At the current catalog size, acceptance is:

- cold personalized first page p95 no more than 800 ms;
- cached continuation p95 no more than 250 ms;
- candidate union no more than 600 and reranked/frozen pool no more than 200;
- PostgreSQL MV fallback hydrates only the rows returned for that page;
- no public/NSFW/moderation leak and no repeat within an exact cooldown.

These are rollout gates, not a statement that `personalized_v2` or its
backfills are active on the live beta.

### Chronological offline evaluator

Before changing the active item embedding or recommendation representation,
run the bounded PostgreSQL-only replay:

```sh
uv run memexpert-recommendation-evaluator \
  --max-users 25 \
  --max-catalog 1000 \
  --max-cases 50 \
  --k 50 \
  --pretty
```

The command forces its transaction to `READ ONLY`, rolls it back, does not call
Qdrant or another provider, and prints aggregate JSON without user IDs, raw
queries, vectors, tokens, or cursors. The catalog is sampled newest-first from
currently public memes whose primary file is READY and has a current-model
image embedding. To return the requested catalog bound, SQL scans at most
`min(50_000, 5 × requested catalog limit)` newest public/READY memes before
embedding eligibility is applied. This bounded newest-first sample biases
catalog and coverage interpretation toward recent inventory; its metrics do
not describe the full public catalog.

Hard maxima are 500 users, 50,000 catalog items, 10,000 holdout cases, and
`K=200`. The command has a 600-second timeout, and each user's loaded history is
capped at the 500 most recent observations before chronological cases are
constructed. It holds out each next distinct strong positive chronologically,
excludes future/not-yet-created catalog items and memes retained in the earlier
training history, and compares `current_centroid`, `two_profile`, `clustered`,
and `multi_positive`. Reports include Recall@K, NDCG@K, bounded catalog
coverage, source/template concentration, and intra-list diversity.
Earlier weak observations of the held-out target are removed from that case's
training history to prevent target-vector leakage, so the evaluator can still
measure a later weak-to-strong conversion.
Training context uses detail views (weight 1), engaged views (2), high-intent
actions (4), and current favorite/save/pin rows (5); impressions contribute no
preference weight. Durable rows are a current-state snapshot, so removed
preferences are intentionally absent and historical replay retains that
survivorship limitation.

Start with the defaults above: runtime grows with cases × catalog size × vector
dimensions (and the number of direct positive examples). Increase one bound at
a time, retain the exact bounds with every report, and compare variants only
when they use the same database snapshot and bounds. A zero-case report means
the sampled catalog/users did not contain enough chronological embedded strong
positives; it is not evidence that variants are equivalent.

## Analytics Event Write Failures

Search API/bot logs for `event=analytics_event_write_failed`.

Use `event_type`, `user_id`, `payload_key_count`, and `payload_keys` to identify the failing call path. Payload values are intentionally omitted, so inspect the caller code or reproduce safely if the key summary is not enough.

## RabbitMQ Outbox Lag

Search scheduler logs for `event=scheduler_job_batch_result job_id=rabbitmq-outbox-publisher`.

Important fields:

- `claimed`, `published`, `failed`, and `recovered` show the current sweep result.
- `outbox_due_count` shows due messages still waiting after the sweep.
- `outbox_pending_count`, `outbox_failed_count`, and `outbox_publishing_count` show durable outbox state.
- `outbox_oldest_due_age_seconds` shows how old the oldest currently due message is.

If `outbox_due_count` or `outbox_oldest_due_age_seconds` grows across scheduler ticks, verify RabbitMQ connectivity, worker health, broker credentials, and publisher failure reasons in nearby logs.

## Search Index Sync Lag

Search scheduler logs for `event=scheduler_job_batch_result job_id=search-index-sync`.

Important fields:

- `scanned`, `updated`, `failed`, and `skipped` show the current bounded sweep result.
- `index_sync_unsynced_count` counts pending, failed, and processing snapshot rows after the sweep.
- `index_sync_failed_count` counts rows needing retry after a provider or payload failure.
- `index_sync_processing_count` counts rows currently leased or stuck until reclaim timeout.
- `index_sync_oldest_lag_seconds` shows the oldest lagging snapshot age.

If lag grows, check Qdrant and Meilisearch availability and nearby provider failure logs from the scheduler or content pipeline. See `docs/ops/content-pipeline-search-sync.md` for the underlying sync workflow.

## Crawler Freshness

Crawler freshness is still read through existing operator surfaces that use `build_crawler_freshness_snapshot`; this stage does not add a scheduler freshness evaluator. Use the current operator route/output to review stale channels, last success timestamps, and last failure reasons, then correlate with Telegram crawler logs and `source-engagement-capture` scheduler logs.

## Provider Failures

Provider failures are surfaced as structured degraded logs with safe reasons such as `text_search_failed`, `semantic_search_failed`, `query_embedding_failed`, `qdrant_lookup_failed`, or `stored_image_embedding_decode_failed`. The logs include `exception_type` but do not include raw provider payloads, credentials, tokens, or query text.

## Future Direction

OpenTelemetry can be wired later around these same fields and request ids. It is not wired in this stage, and there is intentionally no Prometheus endpoint or dashboard to operate.
