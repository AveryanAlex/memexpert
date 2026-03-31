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
| Qdrant down | Text-only results from Meilisearch |
| Meilisearch down | Semantic-only results from Qdrant |
| Both search engines down | Return trending memes as fallback |
| Voyage AI down | Queue tasks; existing search works from cached embeddings |
| Redis down | Direct PG queries; sync tasks lost (resync later) |
| PaddleOCR down | Skip OCR, retry later; existing memes unaffected |

### Retry Policy

TaskIQ tasks: exponential backoff, max 5 retries, 30s base delay. External API calls (Voyage AI, Google Translate): 3 retries with 1s/2s/4s backoff. Qdrant/Meilisearch writes: 3 retries with 1s backoff.

## Database Migrations

Alembic for PostgreSQL. All migrations reversible. Data migrations separated from schema migrations. Zero-downtime strategy: add columns as nullable first, backfill, then add constraints.

## Embedding Model Upgrades

The embedding cache + Qdrant alias architecture enables zero-downtime model upgrades:

1. Create new Qdrant collection (`meme_files_{new_version}`)
2. Recompute all embeddings with new model (batched via TaskIQ)
3. Populate new collection with new embeddings + existing payloads
4. Atomic alias switch (`meme_files` alias → new collection)
5. Invalidate text query cache in PG (`DELETE FROM embedding_cache WHERE input_type = 'text'`)
6. Clean up old collection after verification

Old and new embeddings coexist in the cache table (keyed by `model_version`). The alias switch is atomic — zero search downtime.

**Full resync** (maintenance, recovery) uses the same alias pattern: build new collection alongside the live one, switch atomically.

## Monitoring

Prometheus + Grafana. Key metrics:

- Search latency (p50, p95, p99) — per engine and combined
- TaskIQ queue depth — per queue (transcode, ocr, embed, sync, etc.)
- Sync lag (time from PG write to index update)
- API error rates by endpoint
- Embedding API latency and error rate
- Crawler health (last crawl time, error rate per channel)

## CI/CD

```
push/PR → lint (ruff) → type check (mypy) → unit tests → integration tests → build
merge to main → deploy staging → E2E tests → deploy production
```

- Unit tests: pytest, mocked external services
- Integration tests: testcontainers for PostgreSQL, Qdrant, Meilisearch, Redis
- E2E tests: Playwright for SvelteKit frontend
- API tests: httpx + pytest against FastAPI test client

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Telethon ban / rate limit | High | Multiple sessions, conservative rates, backoff; consider Bot API fallback |
| Voyage AI downtime | Medium | Circuit breaker, queue tasks, search works from cache |
| Voyage AI pricing change | Medium | Matryoshka dims (reduce to 512), or self-hosted model. Embedding cache enables atomic switch. |
| Qdrant downtime | Medium | Degrades to Meilisearch-only. PG has embeddings for recovery. |
| Meilisearch downtime | Medium | Degrades to Qdrant-only. |
| Sync lag | Low | TaskIQ retries. Full resync for recovery. Monitor sync delay. |
| Qdrant memory at scale | Low | Scalar quantization halves memory. At 10M+ vectors consider cluster mode. |
| Transcoding backlog | Medium | Monitor queue depth, scale workers independently. |
| PaddleOCR accuracy on Cyrillic | Medium | Qwen2.5-VL fallback. Manual correction for important memes. |
| Guest account accumulation | Low | 30-day TTL cleanup job. |
| Account merge data loss | Medium | Audit log. Careful merge logic within transactions. |
| JWT secret compromise | High | Short-lived access tokens (15 min), refresh rotation, revocation. |
| Dedup threshold tuning | Medium | Conservative start, admin merge for misses. |
| 152-FZ non-compliance | High | Grace period, destruction log, automated hard delete. |
