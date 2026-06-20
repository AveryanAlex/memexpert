# Launch Observability

This launch stage uses structured application logs and existing operator/read paths. It does not add Prometheus, Grafana, tracing backends, metrics endpoints, dashboards, or a new long-running service.

## Safety Rules

- Search and recommendation logs never include raw query text. Use `query_present`, `query_length`, `surface`, `scope`, `tag_count`, `collection_count`, and `filter_count`.
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
docker compose logs -f workers
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

Useful job ids are `search-index-sync`, `rabbitmq-outbox-publisher`, `source-engagement-capture`, `seo-backlog-batches`, and `materialized-view-refresh`.

## Search And Recommendations

For a degraded search or recommendation response, start from the response `request_id` and search API logs for the same field:

- `event=meme_search_completed` for search responses.
- `event=meme_recommendation_completed` for recommendation responses.
- `event=meme_similar_completed` for similar-meme responses.
- `event=meme_search_provider_failure`, `event=meme_recommendation_provider_failure`, or `event=meme_similar_provider_failure` for provider fallback context.

Key fields are `request_id`, `surface`, `user_id`, `source_algorithm`, `algorithm_version`, `degraded_mode`, `reason`, `fallback_reason`, `candidate_count`, `visible_count`, `result_count`, `embedding_latency_seconds`, `text_latency_seconds`, `semantic_latency_seconds`, `db_latency_seconds`, and `total_latency_seconds`.

Do not search logs for raw query text. Use `query_present=true` and `query_length` to confirm whether a query existed without exposing private search text.

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
