# Infrastructure

## Caching

### Redis

| Cache | Key Pattern | TTL | Notes |
|-------|-------------|-----|-------|
| Meme detail | `meme:{id}` | 5 min | Avoid PG round-trip for hot memes |
| Search results | `search:{user_id}:{query_hash}:{filters_hash}` | 2 min | **Must include user_id** to prevent private meme leakage |
| User profile | `user:{id}` | 10 min | Session-adjacent data |
| Trending | `trending:{period}` | 5 min | Precomputed lists |
| Meme of the day | `motd` | 1 hour | Single value, high traffic |
| Template memes | `template:{slug}:memes` | 5 min | Template page gallery |

Invalidation: meme updates delete `meme:{id}`. Like count changes invalidated after batch sync. SEO generation invalidates `meme:{id}` + CDN purge. Trending keys overwritten on recomputation.

### CDN

| Content | Cache-Control | Notes |
|---------|--------------|-------|
| Media files (S3) | `public, max-age=31536000, immutable` | Content-addressed by MemeFile ID |
| imgproxy transforms | CDN-cached by URL | On-the-fly variants |
| HTML pages | `public, s-maxage=60, stale-while-revalidate=300` | Short cache with SWR |
| API responses | `private, max-age=0` | Personalized |

## Error Handling & Resilience

### Circuit Breakers

| Service | Open After | Half-Open Interval | Fallback |
|---------|-----------|-------------------|----------|
| Voyage AI | 3 failures in 60s | 30s | Queue embedding tasks, retry later |
| Qdrant | 5 failures in 60s | 15s | Text-only search via Meilisearch |
| Meilisearch | 5 failures in 60s | 15s | Semantic-only search via Qdrant |
| Google Translate | 3 failures in 60s | 60s | Skip translation, search original query |

### Graceful Degradation

| Failure | Behavior |
|---------|----------|
| Qdrant down | Text-only results from Meilisearch (rebalanced weights — see [Search: Degraded Mode](03-search.md#degraded-mode)) |
| Meilisearch down | Semantic-only results from Qdrant (rebalanced weights — see [Search: Degraded Mode](03-search.md#degraded-mode)) |
| Both search engines down | Return trending memes as fallback |
| Voyage AI down | Queue tasks; existing search works from cached embeddings |
| Redis down | Direct PG queries, no caching; rate limiting unavailable. Pipeline unaffected (RabbitMQ). |
| PaddleOCR down | Skip OCR, retry later; existing memes unaffected |

### Retry Policy

RabbitMQ consumers: exponential backoff via DLX TTL, max 5 retries, 30s base delay. Messages exceeding retries route to dead letter queues for inspection. External API calls (Voyage AI, Google Translate): 3 retries with 1s/2s/4s backoff. Qdrant/Meilisearch writes: 3 retries with 1s backoff.

## Database Migrations

Alembic for PostgreSQL. All migrations reversible. Data migrations separated from schema migrations. Zero-downtime strategy: add columns as nullable first, backfill, then add constraints.

## Embedding Model Upgrades

The embedding cache + Qdrant alias architecture enables zero-downtime model upgrades:

1. Create new Qdrant collection (`meme_files_{new_version}`)
2. Recompute all embeddings with new model (replay `meme_ready` events or batch job via APScheduler)
3. Populate new collection with new embeddings + existing payloads
4. Atomic alias switch (`meme_files` alias → new collection)
5. Invalidate text query cache in PG (`DELETE FROM embedding_cache WHERE input_type = 'text'`)
6. Clean up old collection after verification

Old and new embeddings coexist in the cache table (keyed by `model_version`). The alias switch is atomic — zero search downtime.

**Full resync** (maintenance, recovery) uses the same alias pattern: build new collection alongside the live one, switch atomically.

## Monitoring

The application exposes metrics via a Prometheus-compatible endpoint (`/metrics`). Metrics collection, storage, dashboards, and alerting (Prometheus, Grafana, etc.) are deployment concerns handled outside this project.

### Metrics to expose

- Search latency (p50, p95, p99) — per engine and combined
- RabbitMQ queue depth — per queue (transcode, ocr, embed, sync, seo)
- Sync lag (time from PG write to index update)
- API error rates by endpoint
- Embedding API latency and error rate
- Crawler health (last crawl time, error rate per channel)
- Circuit breaker state changes

## Local Development

```
docker compose up -d   # PG, Qdrant, Meilisearch, Redis, RabbitMQ, imgproxy
uv run memexpert-api   # run API locally
uv run memexpert-bot   # run bot locally
```

Python app runs natively via `uv` (package manager + virtualenv). All infrastructure services run in Docker containers via `docker-compose.yml`. Code changes don't require container rebuilds — fastest iteration loop.

## Testing

### Unit Tests (pytest, no I/O)

Fast, mocked, run on every push. Target pure business logic in the service layer:

- Popularity formula, trending calculation, search score merging
- pHash comparison, dedup scoring thresholds
- Pydantic schema validation (API models, FastStream message schemas)
- Bot handlers with mocked Telegram update objects — verify handler calls correct service functions
- Utility functions

### Integration Tests (pytest + testcontainers)

Service layer with real infrastructure — each test run spins up fresh containers, no pre-provisioned databases:

- **PostgreSQL:** CRUD, account merge atomicity, collection access control, dedup logic, like count consistency, deletion cascade
- **Qdrant:** semantic search, embedding-based dedup, recommendations, payload filtering
- **Meilisearch:** text search, typo tolerance, Russian morphology, faceted filtering
- **Redis:** cache hit/miss behavior, rate limiting counters, candidate pool caching
- **RabbitMQ:** FastStream consumer tests — event routing, fan-out, DLX retry, message schema validation
- **FastAPI routes:** httpx against FastAPI test client with all real deps — verifies serialization, auth middleware, error responses, rate limiting

### SvelteKit Tests

- **Component tests (Vitest):** individual components in isolation — meme cards, search bar, collection grid, filter sidebar, admin panels
- **E2E tests (Playwright):** full browser flows against running API:
  - Search → view meme → like → appears in favorites
  - Collection create → invite link → join → see shared memes
  - Guest browsing → link Telegram → account merge
  - Admin: meme merge, SEO AI-assisted edit, template curation
  - SSR: pages render correctly with SEO content, meta tags, OpenGraph

### What We Don't Test Automatically

- **Telegram bot E2E** — no test mode in Telegram Bot API. Covered by: service-layer integration tests + mocked update objects in unit tests + manual QA.
- **Crawlers** — depend on live Telegram channels. Test the ingestion service with fake `RawMeme` input, not the Telethon listener.
- **Channel Bot** — test recommendation/selection logic as a service function, not the posting.

## CI/CD

```
push/PR → [parallel]
            ├─ Python: lint (ruff) → type check (mypy) → unit tests → integration tests (testcontainers)
            └─ SvelteKit: lint (biome) → type check (svelte-check) → Vitest component tests
merge    → build images → deploy staging → Playwright E2E → deploy production
```

Integration tests run in CI with testcontainers — no shared test databases, no flaky state between runs.

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Telethon ban / rate limit | High | Multiple sessions, conservative rates, backoff; consider Bot API fallback |
| Voyage AI downtime | Medium | Circuit breaker, queue tasks, search works from cached embeddings |
| Voyage AI pricing change or discontinuation | High | Unlike LLMs, embeddings cannot be hot-swapped — switching provider requires recomputing all embeddings and rebuilding the Qdrant index (the alias pattern makes the switch zero-downtime, but recomputation takes days at scale). Mitigation: Matryoshka dims (reduce to 512) for cost, or self-hosted model (CLIP). Embedding cache in PG enables recomputation without re-downloading media. Accept this as a vendor lock-in risk. |
| Qdrant downtime | Medium | Degrades to Meilisearch-only. PG has embeddings for recovery. |
| Meilisearch downtime | Medium | Degrades to Qdrant-only. |
| Sync lag | Low | RabbitMQ retries via DLX. Full resync for recovery. Monitor sync delay. |
| Qdrant memory at scale | Low | Scalar quantization halves memory. At 10M+ vectors consider cluster mode. |
| Transcoding backlog | Medium | Monitor queue depth, scale workers independently. |
| PaddleOCR accuracy on Cyrillic | Medium | Qwen2.5-VL fallback. Manual correction for important memes. |
| Guest account accumulation | Low | 90-day TTL cleanup job (guests with no interactions). |
| Account merge data loss | Medium | Audit log. Careful merge logic within transactions. |
| JWT secret compromise | High | Short-lived access tokens (15 min), refresh rotation, revocation. |
| Dedup threshold tuning | Medium | Conservative start, admin merge for misses. |
| Account deletion edge cases | Medium | 30-day grace period, deletion log, automated hard delete job. |
