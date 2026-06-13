# Infrastructure

## Caching

### Redis

| Cache | Key Pattern | TTL | Notes |
|-------|-------------|-----|-------|
| Meme detail | `meme:{id}` | 5 min | Avoid PG round-trip for hot memes |
| Optional search candidate pool | `search:{user_id}:{scope_hash}:{query_hash}:{filters_hash}:{algo}` | 60–120s | Optional optimization for stable hybrid pagination; must include every access-shaping field |
| User/session-adjacent profile | `user:{id}` | 10 min | Safe user display metadata only |
| Trending | `trending:{period}` | 5 min | Precomputed/materialized rankings if not read directly from PG MV |
| Meme of the day | `motd` | 1 hour | Single value, high traffic |
| Template memes | `template:{slug}:memes` | 5 min | Template page gallery |
| Rate limits | `rate:{tier}:{subject}` | window-based | Sliding-window counters |

Invalidation: meme updates delete `meme:{id}`. Like count and popularity changes are synced after batch jobs. SEO generation invalidates `meme:{id}` + CDN purge where needed. Trending/MOTD keys are overwritten by scheduler jobs.

Search candidate-pool caching is not a correctness dependency for MVP. If enabled, key construction must prevent private result leakage across users/scopes/collections.

### CDN

| Content | Cache-Control | Notes |
|---------|--------------|-------|
| Media files (S3) | `public, max-age=31536000, immutable` | Content-addressed by MemeFile ID |
| imgproxy transforms | CDN-cached by URL | On-the-fly variants |
| HTML pages | `public, s-maxage=60, stale-while-revalidate=300` | Short cache with SWR |
| API responses | `private, max-age=0` | Personalized |

## Error Handling & Resilience

### Circuit Breakers

Circuit breakers are an operational guardrail around external/search services. Thresholds are config, not product constants.

| Service | Example Open Condition | Fallback |
|---------|------------------------|----------|
| Voyage AI | repeated query/embedding timeouts | Queue pipeline embedding tasks; search query path skips semantic component |
| Qdrant | repeated query/write failures | Text + popularity/trending search; similar/recommendation surfaces use tag/trending fallback |
| Meilisearch | repeated query/write failures | Semantic + popularity/trending search |
| Google Translate | repeated failures | Skip translation, search original query |
| Redis | unavailable | Direct DB/search calls; no candidate-pool cache/rate-limit cache |

### Graceful Degradation

| Failure | Behavior |
|---------|----------|
| Qdrant down | Text-only/text+popularity results from Meilisearch; recommendations and similar memes fall back with attribution |
| Meilisearch down | Semantic-only/semantic+popularity results from Qdrant |
| Both search engines down | Return trending/popular materialized-view fallback |
| Voyage AI down | Queue pipeline tasks; query-time semantic embedding skipped; existing indexed embeddings remain usable |
| Redis down | Direct PG/search queries, no caching; rate limiting unavailable. Pipeline unaffected (RabbitMQ). |
| PaddleOCR down | Skip OCR, retry later; existing memes unaffected |

Search/recommendation logs should record `degraded=true`, missing engines, and fallback source so analytics can distinguish poor relevance from infrastructure failures.

### Retry Policy

RabbitMQ consumers: exponential backoff via DLX TTL, max retries configured per queue, 30s base delay. Messages exceeding retries route to dead letter queues for inspection. External API calls (Voyage AI, translation, PydanticAI SEO provider): bounded retries with exponential backoff. Qdrant/Meilisearch writes: bounded retries plus DLQ/replay for durable sync failures.

## Database Migrations

Alembic for PostgreSQL. All migrations reversible where practical. Data migrations separated from schema migrations. Zero-downtime strategy: add columns as nullable first, backfill, then add constraints.

Materialized views can power public trends/tag/template analytics, Meme of the Day candidate sets, and timeline pages. Prefer `REFRESH MATERIALIZED VIEW CONCURRENTLY` where the view has the required unique indexes and the refresh interval is user-visible.

## Embedding Model Upgrades

The embedding cache + Qdrant alias architecture enables zero-downtime model upgrades:

1. Create new Qdrant collection (`meme_files_{new_version}`)
2. Recompute all embeddings with new model (replay events or run a scheduler/batch job)
3. Populate new collection with new embeddings + existing payloads
4. Atomic alias switch (`meme_files` alias → new collection)
5. Invalidate text query cache in PG (`DELETE FROM embedding_cache WHERE input_type = 'text'`)
6. Clean up old collection after verification

Old and new embeddings coexist in the cache table (keyed by `model_version`). The alias switch is atomic — zero search downtime.

**Full resync** (maintenance, recovery) uses the same alias pattern: build new collection alongside the live one, switch atomically.

## Observability

OpenTelemetry is the planned observability direction. A Prometheus-compatible `/metrics` endpoint is deferred.

MVP observability requirements:

- Structured logs with request id / user id where safe / algorithm version / degraded mode
- Search latency per component (semantic, text, DB/rerank)
- Recommendation latency and candidate counts
- Interaction-event write failures
- RabbitMQ queue depth/lag per queue
- Sync lag (time from PG write to search-index update)
- API error rates by endpoint
- Embedding and SEO provider latency/error rate
- Crawler health (last crawl time, error rate per channel)
- Scheduler lifecycle, advisory-lock conflict, and job duration/failure logs

## Local Development

```
docker compose up -d   # PG, Qdrant, Meilisearch, Redis, RabbitMQ, imgproxy
uv run memexpert-api   # run API locally
uv run memexpert-scheduler   # run periodic jobs locally
uv run memexpert-bot   # run bot locally
```

Python app runs natively via `uv` (package manager + virtualenv). All infrastructure services run in Docker containers via `docker-compose.yml`. Code changes don't require container rebuilds — fastest iteration loop.

## Testing

Run the Python suite through the project `uv` environment:

```bash
uv run pytest -q
```

`pyproject.toml` configures pytest to use `pytest-xdist` by default (`-n 4 --dist loadfile`). This keeps file-local test ordering intact while running different test files across four workers, which is the fastest safe default found so far. Use `uv run pytest -q --override-ini 'addopts='` when you need a single-process diagnostic run.

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
push/PR → [parallel]
            ├─ Python: lint (ruff) → type check (mypy) → unit tests → integration tests (testcontainers)
            └─ SvelteKit: pnpm install → svelte-check → Vitest → Playwright smoke → build
merge    → build images → deploy staging → Playwright E2E → deploy production
```

Integration tests run in CI with testcontainers — no shared test databases, no flaky state between runs.

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Telethon ban / rate limit | High | Multiple sessions, conservative rates, backoff; consider Bot API fallback |
| Voyage AI downtime | Medium | Circuit breaker, queue tasks, search works from cached/indexed embeddings |
| Voyage AI pricing change or discontinuation | High | Switching provider requires recomputing embeddings and rebuilding Qdrant; alias pattern makes the switch zero-downtime, but recomputation takes days at scale. Mitigation: Matryoshka dims, possible self-hosted model, PG embedding cache. |
| Qdrant downtime | Medium | Degrades to Meilisearch/text + popularity; similar/recs fall back with attribution. |
| Meilisearch downtime | Medium | Degrades to Qdrant/semantic + popularity. |
| Sync lag | Low | RabbitMQ retries via DLX. Full resync for recovery. Monitor sync delay. |
| Qdrant memory at scale | Low | Scalar quantization halves memory. At 10M+ vectors consider cluster mode. |
| Transcoding backlog | Medium | Monitor queue depth, scale workers independently. |
| PaddleOCR accuracy on Cyrillic | Medium | Qwen2.5-VL fallback. Manual correction for important memes. |
| Guest account growth | Low/Medium | Guest histories are retained for personalization and conversion; monitor storage growth and keep event tables partitionable/archivable if needed. |
| Account merge data loss | Medium | Audit log. Careful merge logic within transactions. |
| JWT secret compromise | High | HttpOnly cookies, configurable TTL, token nonce revocation, secret rotation plan. |
| Dedup threshold tuning | Medium | Conservative start, admin merge for misses. |
