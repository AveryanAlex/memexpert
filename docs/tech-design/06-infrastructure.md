# Infrastructure

## Caching

### Redis

| Cache | Key Pattern | TTL | Notes |
|-------|-------------|-----|-------|
| Meme detail | `meme:{id}` | 5 min | Avoid PG round-trip for hot memes |
| Optional search candidate pool | `search:{user_id}:{scope_hash}:{query_hash}:{filters_hash}:{algo}` | 60–120s | Optional optimization for stable hybrid pagination; must include every access-shaping field |
| Home feed pool | `recommendation:feed_pool:{pool_id}` | 2 hours | Ordered pool of at most 200 IDs/scores/source contributions; disposable presentation state |
| Viewer feed-pool index | `recommendation:viewer_pools:v2:{user_id}` | 2 hours | Creation-time sorted set; retains four pools per viewer by default and supports atomic oldest-pool eviction plus invalidation |
| Current search intent | `recommendation:intent:{user_id}` | 2 hours | Rolling vector with 30-minute half-life; never stores raw query text |
| Telegram empty-query continuation | `recommendation:telegram_inline:{ti1_handle}` | Up to 2 hours | Compact handle for viewer/NSFW-bound pin position, pending Home items, Home cursor, and next rank; never stores raw query text |
| User/session-adjacent profile | `user:{id}` | 10 min | Safe user display metadata only |
| Trending | `trending:{period}` | 5 min | Precomputed/materialized rankings if not read directly from PG MV |
| Meme of the day | `motd` | 1 hour | Single value, high traffic |
| Template memes | `template:{slug}:memes` | 5 min | Template page gallery |
| Rate limits | `rate:{tier}:{subject}` | window-based | Sliding-window counters |

Invalidation: meme updates delete `meme:{id}`. Like count changes and derived popularity/read-model refreshes are synced after batch jobs. SEO generation invalidates `meme:{id}` + CDN purge where needed. Trending/MOTD keys are overwritten by scheduler jobs.

Search candidate-pool caching is not a correctness dependency for MVP. If enabled, key construction must prevent private result leakage across users/scopes/collections. Home cursor claims are signed and bind their pool to a keyed viewer identity, normalized filters, language/NSFW policy, and algorithm version. PostgreSQL rechecks public visibility, moderation, NSFW, and exact cooldown state on every slice. A cursorless serving-eligible request first performs a Redis read preflight under the configured 0.5-second default timeout; failure switches directly to the bounded `public_meme_trends_mv` PostgreSQL keyset path before profile/Qdrant candidate generation. Freeze atomically stores and indexes the new pool and removes oldest pool bodies above `RECOMMENDATION_FEED_POOL_MAX_PER_VIEWER` (default `4`, bounded `1..32`). Missing, expired, or retention-evicted pools return `410 feed_cursor_expired`. Viewer changes and guest merges invalidate both the versioned sorted index and referenced pools; compatibility invalidation also clears the legacy set/index.

Telegram's compact continuation is additionally bound to Telegram user,
resolved viewer, NSFW allowance, and personal/non-personal mode. If saving the
next handle fails, the bot still returns the current sendable page but marks it
terminal (`has_more=false`, no `next_offset`) and logs
`telegram_inline_cursor_unavailable`. Failure to load/validate an incoming
handle fails closed through the bot's empty inline-answer path rather than
falling back to an unstable numeric offset.

### CDN

| Content | Cache-Control | Notes |
|---------|--------------|-------|
| Media files (S3) | `public, max-age=31536000, immutable` | Content-addressed by MemeFile ID |
| imgproxy transforms | CDN-cached by URL | On-the-fly variants |
| Global deterministic HTML pages | `public, s-maxage=60, stale-while-revalidate=300` | Only pages whose payload has no viewer/profile/token/cursor dependence |
| Home, Search, detail/Similar SSR and personalized/discovery proxies | `private, no-store` | Viewer-, profile-, attribution-token-, or cursor-bound; never shared-cacheable |

## Error Handling & Resilience

### Circuit Breakers

Circuit breakers are an operational guardrail around external/search services. Thresholds are config, not product constants.

| Service | Example Open Condition | Fallback |
|---------|------------------------|----------|
| Voyage AI | repeated query/embedding timeouts | Queue pipeline embedding tasks; search query path skips semantic component |
| Qdrant | repeated query/write failures | Text + popularity/trending search; Similar uses tag/template/popular tiers; Home retains bounded PostgreSQL trending/exploration sources and freezes them when Redis is healthy |
| Meilisearch | repeated query/write failures | Semantic + popularity/trending search |
| Google Translate | repeated failures | Skip translation, search original query |
| Redis | unavailable | Search recomputes without its optional pool; Home uses PostgreSQL MV keyset fallback; rate-limit cache is unavailable |

### Graceful Degradation

| Failure | Behavior |
|---------|----------|
| Qdrant down | Text-only/text+popularity results from Meilisearch; Similar uses its PostgreSQL fallback tiers; Home omits vector sources but can still fuse/rank/freeze PostgreSQL trending and exploration candidates with typed remaining-source attribution and degraded logging |
| Meilisearch down | Semantic-only/semantic+popularity results from Qdrant |
| Both search engines down | Return trending/popular materialized-view fallback |
| Voyage AI down | Queue pipeline tasks; query-time semantic embedding skipped; existing indexed embeddings remain usable |
| Redis down | Search uses direct PG/search calls; Home uses bounded MV keyset paging rather than personalized offsets; rate limiting is unavailable. Pipeline is unaffected (RabbitMQ). |
| PaddleOCR down | Skip OCR, retry later; existing memes unaffected |

Search/recommendation logs should record `degraded=true`, missing engines, and fallback source so analytics can distinguish poor relevance from infrastructure failures.

### Retry Policy

RabbitMQ consumers: exponential backoff via DLX TTL, max retries configured per queue, 30s base delay. Messages exceeding retries route to dead letter queues for inspection. External API calls (Voyage AI, translation, PydanticAI SEO provider): bounded retries with exponential backoff. Qdrant/Meilisearch writes: bounded retries plus DLQ/replay for durable sync failures.

## Database Migrations

Alembic for PostgreSQL. All migrations reversible where practical. Data migrations separated from schema migrations. Zero-downtime strategy: add columns as nullable first, backfill, then add constraints.

Materialized views can power public trends/tag/template analytics, Meme of the Day candidate sets, timeline pages, and item recommendation features. These are derived-cached read models over source snapshots, keyed exposures, `analytics_events`, and meme/template metadata; they are not canonical truth. Prefer `REFRESH MATERIALIZED VIEW CONCURRENTLY` where the view has the required unique indexes and the refresh interval is user-visible. The scheduler refreshes in dependency order (`public_meme_trends_mv`, tag/template summaries, point aggregates, then `public_meme_recommendation_features_mv`) and logs `public_trend_mv_concurrent_refresh_fallback` with the view name before retrying without `CONCURRENTLY` when PostgreSQL rejects concurrent refresh.

## Embedding Model Upgrades

The current repository uses one unnamed 1024-dimensional Voyage
`voyage-multimodal-3.5` vector per file in the configured Qdrant collection.
Meilisearch remains the lexical OCR engine. A stable Qdrant read alias,
named-vector collection, dual-write path, and embedding-artifact normalization
are Phase-3 future work, not deployed architecture or the routine metadata
resync path.

Before adding `ocr_dense`, chronological offline replay must show at least 5%
overall or 10% text-heavy Recall@50 improvement, no more than 50 ms additional
p95 retrieval time, and projected Qdrant peak memory below 70% of the live 4 GB
limit. If memory fails, evaluate 512-dimensional Matryoshka vectors or scalar
quantization first. Qdrant sparse OCR and a separate meme-level recommendation
collection remain out of scope without later evidence.

If the evidence gate passes, first normalize PostgreSQL storage into immutable
embedding artifacts with file associations. Then introduce a stable read alias,
create a versioned collection and payload indexes, dual-write new sync events,
backfill from PostgreSQL in bounded batches, verify READY-primary coverage,
counts, dimensions, filters, memory and sampled ranking parity, atomically
switch the alias, and retain the old collection for rollback. No document or
landed schema should be read as evidence that this production migration has
occurred.

## Observability

OpenTelemetry is the planned observability direction. A Prometheus-compatible `/metrics` endpoint is deferred.

MVP observability requirements:

- Structured logs with request id / user id where safe / algorithm version / degraded mode
- Search latency per component (semantic, text, DB/rerank)
- Recommendation p50/p95 latency by retrieval, fusion, PostgreSQL
  filtering/features, rerank/diversity, Redis, and hydration stage
- Per-source candidate counts, union/rerank/pool sizes, filtered ratio,
  cold-start/Qdrant/Redis fallback, and cursor-expiry rate
- Strong-action and attributed-send rate per keyed impression,
  repeat-within-cooldown rate, source/template concentration, catalog/long-tail
  coverage, and exploration share/conversion grouped by surface and
  algorithm/profile version
- Interaction-event write failures
- RabbitMQ queue depth/lag per queue
- Sync lag (time from PG write to search-index update)
- API error rates by endpoint
- Embedding and SEO provider latency/error rate
- Crawler health (last crawl time, error rate per channel)
- Scheduler lifecycle, advisory-lock conflict, job duration/failure logs, source-engagement capture counts, and materialized-view concurrent-refresh fallback logs

The bounded daily recommendation rollup owns trusted keyed-impression
conversion, cooldown-repeat, concentration, coverage, exploration, and coarse
impression-level fallback metrics. Structured logs own cold-start/provider/Redis
fallback categories, cursor expiry, candidate/filter counts, and raw stage
latency samples from which p50/p95 are calculated. The aggregate
`cache_expiry_count` field is reserved at zero; do not combine it with the
structured cursor-expiry series.

Performance acceptance at the current catalog size is cold personalized first
page p95 at or below 800 ms, cached continuation p95 at or below 250 ms,
candidate union at or below 600, ordered pool at or below 200, and fallback
hydration limited to the returned rows. `personalized_v2` first runs in shadow
mode, then a stable-user-hash canary. Shadow and canary telemetry must carry the
algorithm/profile versions and candidate sources while never logging raw query
text, signed cursor/attribution tokens, or their claims.

## Local Development

```
docker compose up -d   # PG, Qdrant, Meilisearch, Redis, RabbitMQ, imgproxy
uv run memexpert-api   # run API locally
uv run memexpert-scheduler   # run periodic jobs locally
uv run memexpert-bot   # run bot locally
```

Python app runs natively via `uv` (package manager + virtualenv). All infrastructure services run in Docker containers via `docker-compose.yml`. Code changes don't require container rebuilds — fastest iteration loop.

Infrastructure image tags are explicit where applicable: local and
container-E2E Compose use Qdrant 1.18.3, while the production example and live
beta use the validated 1.18.3 immutable digest below. The stack also uses
PostgreSQL 16.14, Redis 7.4.9, RabbitMQ 4.3.1-management,
Meilisearch 1.46.1, MinIO `RELEASE.2025-09-07T16-13-09Z`, MinIO Client
`RELEASE.2025-08-13T08-35-41Z`, and imgproxy 4.0.4. The live beta's separate
Nix/Quadlet source of truth omits automatic image updates and pins:

```text
docker.io/qdrant/qdrant@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286
```

That pin does not imply the application migration, payload backfill, or payload
indexes have been applied on beta. The optional local pgAdmin profile is pinned
to 9.16. Updates are deliberate manifest/config changes rather than implicit
`latest` or broad-major pulls.

The E2E and production MinIO initializer services clear the pinned `mc` image entrypoint and invoke `/bin/sh -c` explicitly before configuring the alias and idempotently creating the bucket.

Compose runs RabbitMQ as its image-provided `rabbitmq` user so the broker and `rabbitmqctl` share readable Erlang-cookie ownership on rootless as well as rootful Docker daemons.

## Testing

Run the Python suite through the project `uv` environment:

```bash
uv run pytest -q
```

`pyproject.toml` configures pytest to use `pytest-xdist` by default (`-n 4 --dist loadfile`). This keeps file-local test ordering intact while running different test files across four workers, which is the fastest safe default found so far. Use `uv run pytest -q --override-ini 'addopts='` when you need a single-process diagnostic run.

CI overrides that local ceiling explicitly with `-n 2 --dist loadfile` to reduce concurrent RootlessKit/testcontainer pressure. It writes `backend-junit.xml` and uploads it when the backend job fails. Frontend smoke tests keep Playwright retries at zero and write each invocation beneath `frontend/test-results/smoke/<run-id>-<hash>/`; CI uploads `frontend/test-results/**` on frontend failure.

### Unit Tests (pytest, no I/O)

Fast, mocked, run on every push. Target pure business logic in the service layer:

- Popularity formula, trending calculation, search score merging
- Recommendation candidate weighting and interaction-signal selection
- pHash comparison, dedup scoring thresholds
- Pydantic schema validation (API models, FastStream message schemas)
- Bot handlers with mocked Telegram update objects — verify handler calls correct service functions
- Utility functions

### Integration Tests (pytest + testcontainers)

Service layer with real infrastructure — each test run spins up fresh containers, no pre-provisioned databases:

- **PostgreSQL:** CRUD, account merge atomicity, collection access control, dedup logic, like count consistency, interaction-event persistence
- **Qdrant:** semantic search, embedding-based dedup, recommendations, payload filtering
- **Meilisearch:** text search, typo tolerance, Russian morphology, faceted filtering
- **Redis:** cache hit/miss behavior, rate limiting counters, optional candidate pool caching
- **RabbitMQ:** FastStream consumer tests — event routing, fan-out, DLX retry, message schema validation
- **FastAPI routes:** httpx against FastAPI test client with all real deps — verifies serialization, auth middleware, error responses, rate limiting

### SvelteKit Tests

- The frontend lives under `frontend/` and uses pnpm with SvelteKit.
- Current CI runs `pnpm install --frozen-lockfile`, `pnpm check`, Vitest, a Playwright smoke test with a local mocked backend, and `pnpm build`.
- The smoke path covers search results → meme detail → rendered media → visible meme actions.
- The temporary/staging production target is `@sveltejs/adapter-node`; `pnpm build` writes `frontend/build`, and `pnpm start` runs the built Node server.

### What We Don't Test Automatically

- **Telegram bot E2E** — no test mode in Telegram Bot API. Covered by: service-layer integration tests + mocked update objects in unit tests + manual QA.
- **Crawlers** — depend on live Telegram channels. Test the ingestion service with fake `RawMeme` input, not the Telethon listener.
- **Channel Bot** — test recommendation/selection logic as a service function, not the posting.

## CI/CD

```
push/PR   → [parallel]
             ├─ Python lint and type checks
             ├─ Python unit and integration tests (testcontainers)
             ├─ SvelteKit checks, Vitest, Playwright smoke, and build
             └─ container builds, image smoke tests, and real-stack PRD E2E
main push → publish main/worker/frontend `:main` images → Reploy production
```

Integration tests run in CI with testcontainers — no shared test databases, no flaky state between runs.

The production deployment job runs only for a successful push to `main` and
waits for every CI job. It uses the GitHub Environment `production`; the Reploy
endpoint is stored in its `REPLOY_URL` environment variable and the bearer token
in its `REPLOY_TOKEN` environment secret. The deployment requests all three
published app images so Reploy can pull them and restart the affected units.

CI concurrency is keyed by source repository and branch/PR head so a newer run cancels an obsolete run for that head. Before BuildKit cache options are assembled, CI replaces unsafe branch characters and adds a hash of the source repository and full branch name. The actual default-branch run uses the literal `main` suffix so it keeps the fallback cache populated. Other heads write only their sanitized, hashed scope and read `main` as a fallback, preventing concurrent branches or forks from sharing a writer scope or injecting cache-option delimiters.

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Telethon ban / rate limit | High | Multiple sessions, conservative rates, backoff; consider Bot API fallback |
| Voyage AI downtime | Medium | Circuit breaker, queue tasks, search works from cached/indexed embeddings |
| Voyage AI pricing change or discontinuation | High | Switching provider requires recomputing embeddings and rebuilding Qdrant. A future validated alias/dual-write migration can make cutover atomic, but that path is not deployed; recomputation may take days. Mitigation: evidence gates, Matryoshka dimensions, possible self-hosted model, PG embedding cache. |
| Qdrant downtime | Medium | Search degrades to Meilisearch/text + popularity, and Similar uses its PostgreSQL fallback tiers. With Redis healthy, Home omits vector sources but still fuses, ranks, and freezes bounded PostgreSQL trend/exploration candidates with attribution; Redis-unavailable Home uses its signed PostgreSQL keyset fallback. |
| Meilisearch downtime | Medium | Degrades to Qdrant/semantic + popularity. |
| Sync lag | Low | RabbitMQ retries via DLX. Full resync for recovery. Monitor sync delay. |
| Qdrant memory at scale | Low | Scalar quantization halves memory. At 10M+ vectors consider cluster mode. |
| Transcoding backlog | Medium | Monitor queue depth, scale workers independently. |
| PaddleOCR accuracy on Cyrillic | Medium | Tune preprocessing/model version, track low-confidence OCR, and use manual correction for important memes. No Qwen/VLM fallback is active in this slice. |
| Guest account growth | Low/Medium | Guest histories are retained indefinitely for personalization and conversion in this roadmap; monitor storage growth. Partitioning, archival, deletion, reset, and opt-out are explicit non-goals for this phase. |
| Account merge data loss | Medium | Audit log. Careful merge logic within transactions. |
| JWT secret compromise | High | HttpOnly cookies, configurable TTL, token nonce revocation, secret rotation plan. |
| Dedup threshold tuning | Medium | Conservative start, admin merge for misses. |
