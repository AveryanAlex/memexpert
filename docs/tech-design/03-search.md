# Search & Discovery

## Hybrid Search

### Candidate Retrieval

Every search query runs two retrieval paths in parallel:

1. **Semantic:** Qdrant returns top candidates ranked by embedding cosine similarity
2. **Text:** Meilisearch returns top candidates ranked by text relevance (`_rankingScore`, 0–1)

Both engines return a fixed-size candidate pool (configurable, default: 200 candidates each). Results are merged by `meme_id` (full outer join — a meme may appear in one or both result sets) and scored:

```
combined_score = 0.4 × semantic_score + 0.3 × text_score + 0.3 × normalized_popularity
```

Memes found by only one engine receive 0 for the missing component. Meilisearch provides native `_rankingScore` (0–1); Qdrant cosine scores are already 0–1. Popularity is min-max normalized from PostgreSQL.

Weights are hardcoded for launch. Tuning requires A/B testing infrastructure and sufficient traffic volume — deferred post-launch.

### Paginating Hybrid Results

True cursor-based pagination is impossible on a score computed from two independent engines — neither engine knows the other's scores, so neither can produce a stable cursor for the merged ranking.

Solution: **cached candidate pool.**

1. On the first page request, execute both retrievals, merge, score, sort
2. Cache the full ordered list of `(meme_id, combined_score)` in Redis (key: `search:{hash(query, filters, user_id)}`, TTL: 60s)
3. Subsequent page requests read from the cache and slice by offset

When the cache expires or the user scrolls through the entire pool, re-execute the query. The candidate pool size (up to 400 merged candidates — ~16–20 pages at 20–25 items per page) is a practical depth limit; users rarely scroll further. If exhausted, show a "Refine your search" prompt.

### Degraded Mode

When a circuit breaker trips on one engine, the remaining engine serves results alone with rebalanced weights:

| Mode | Weights |
|------|---------|
| Normal | semantic 0.4 + text 0.3 + popularity 0.3 |
| Qdrant down (text-only) | text 0.6 + popularity 0.4 |
| Meilisearch down (semantic-only) | semantic 0.6 + popularity 0.4 |

In degraded mode, results come from a single engine — standard cursor-based pagination applies directly, no candidate pool needed.

## Qdrant

Single collection `meme_files`. One point per MemeFile (only `status = ready` files indexed).

- **Vector:** Voyage AI `voyage-multimodal-3.5`, 1024 dimensions, cosine distance
- **Payload:** meme-level metadata for filtered search — `meme_id`, `is_primary`, `is_public`, `is_nsfw`, `media_type`, `language`, `popularity_score`, `like_count`, `tags`, `author_user_id`, `template_id`, `created_at`
- **Payload indexes** on all fields used in filtering

Used for: semantic search, similar memes (recommend API), personalized feed (recommend API), deduplication during ingestion (high-threshold similarity search).

## Meilisearch

Single index `memes`. One document per Meme.

- **Searchable:** `ocr_text`, `tags`, `caption`, `page_title`
- **Filterable:** `is_public`, `is_nsfw`, `media_type`, `language`, `tags`, `template_slug`, `author_user_id`
- **Sortable:** `popularity_score`, `like_count`, `created_at`
- **Russian stop words** configured
- **Typo tolerance** enabled (min 4 chars for 1 typo, 8 for 2)

Search results return `meme_id` lists from Meilisearch/Qdrant, then fetch full display data from PostgreSQL.

## Private Meme Search

Public and private memes live in the same indexes (both Qdrant and Meilisearch). Filtered using OR clauses:

- `is_public = true` OR `author_user_id = {current_user_id}`

This ensures private memes appear only for their owner. API endpoints additionally verify ownership before returning meme details.

## Search Result Caching

Search results cached in Redis. **Cache key must include `user_id`** alongside query hash and filter hash — otherwise a cached result containing user A's private memes could be served to user B.

## Sync Strategy

**Event-driven:** The content pipeline publishes `meme_ready` events to a RabbitMQ fanout exchange. Qdrant and Meilisearch sync consumers each bind their own queue, processing independently with retries via dead letter exchanges.

- `meme_ready` → Qdrant consumer syncs embedding + payload; Meilisearch consumer syncs full document
- Meme-level field changes (popularity, likes, tags) → API publishes `meme_updated` event → both sync consumers update
- Like count changes → batched sync every 5 minutes via APScheduler (not per-like)

**Full resync:** Uses Qdrant collection aliases for zero-downtime rebuild — create new collection, populate, atomic alias switch, delete old. Same pattern used for embedding model upgrades.

## Pagination

| Context | Strategy | Details |
|---------|----------|---------|
| Search results | Cached candidate pool | See [Hybrid Search](#paginating-hybrid-results) — offset within Redis-cached merged result set |
| Collection browsing | Cursor-based | `(added_at, meme_id)` |
| Trending / feeds | Cursor-based | `(score, meme_id)` |
| Recommendations | Cursor-based | Qdrant offset / page token |

Collections and feeds use cursor-based pagination (single data source, stable ordering). Search results use a cached candidate pool because the merged score cannot be expressed as a cursor in either engine.

## Recommendations

### Similar Memes

Qdrant recommend API from a meme's primary file. Filtered to public, primary files only, excluding the source meme.

### Personalized Feed

Qdrant recommend API using recent interaction history (up to 20 positive examples). Excludes already-seen memes. Falls back to trending for users without history.

### Trending

Uses the trending score from [Content Pipeline: Trending Score](04-content-pipeline.md#trending-score). Precomputed every 5 min, cached in Redis. Default feed for users without interaction history.

## Russian Query Translation

Behind a feature flag. For Russian queries, optionally translate to English via Google Translate and search both, merging results. Improves recall on English-text memes.

## Deduplication Search

After the embed stage computes a file's embedding, Qdrant is queried with a high similarity threshold (cosine > 0.92) to find near-duplicate memes that pHash missed (e.g., significant crops, overlays). Matches trigger auto-merge. See [Content Pipeline: Phase 2 Dedup](04-content-pipeline.md#phase-2-embedding-based-merge-post-embed) for the full flow.
