# Search & Discovery

## Hybrid Search

Every search query runs text search (Meilisearch) and semantic search (Qdrant) in parallel. Results are merged and re-ranked using a weighted formula:

```
combined_score = 0.4 × semantic_score + 0.3 × text_score + 0.3 × normalized_popularity
```

Weights are initial values, tunable via A/B testing post-launch. Meilisearch provides native `_rankingScore` (0–1) — no naive rank normalization needed for text results.

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

Documents include display data (thumbnail URLs, blur hash) to avoid PG round-trips for search result rendering.

## Private Meme Search

Public and private memes live in the same indexes (both Qdrant and Meilisearch). Filtered using OR clauses:

- `is_public = true` OR `author_user_id = {current_user_id}`

This ensures private memes appear only for their owner. API endpoints additionally verify ownership before returning meme details.

## Search Result Caching

Search results cached in Redis. **Cache key must include `user_id`** alongside query hash and filter hash — otherwise a cached result containing user A's private memes could be served to user B.

## Sync Strategy

**On write:** TaskIQ tasks enqueued immediately after PG writes. If the external service is temporarily down, the task retries with exponential backoff.

- MemeFile becomes ready → sync to Qdrant (embedding + payload)
- Meme data changes → sync to Meilisearch (full document upsert)
- Meme-level field changes (popularity, likes, tags) → update Qdrant payload for all files of that meme
- Like count changes → batched sync every 5 minutes (not per-like)

**Full resync:** Uses Qdrant collection aliases for zero-downtime rebuild — create new collection, populate, atomic alias switch, delete old. Same pattern used for embedding model upgrades.

## Pagination

| Context | Strategy | Cursor fields |
|---------|----------|---------------|
| Search results | Cursor-based | `(combined_score, meme_id)` |
| Collection browsing | Cursor-based | `(added_at, meme_id)` |
| Trending / feeds | Cursor-based | `(score, meme_id)` |

No offset-based pagination — avoids drift on inserts and O(N) degradation on deep pages.

## Recommendations

### Similar Memes

Qdrant recommend API from a meme's primary file. Filtered to public, primary files only, excluding the source meme.

### Personalized Feed

Qdrant recommend API using recent interaction history (up to 20 positive examples). Excludes already-seen memes. Falls back to trending for users without history.

### Trending

Computed from: growth rate of channel reposts (new source appearances in 24–48h) + growth in platform engagement (sends, saves, views). Precomputed periodically, cached in Redis.

## Russian Query Translation

Behind a feature flag. For Russian queries, optionally translate to English via Google Translate and search both, merging results. Improves recall on English-text memes.

## Deduplication Search

During ingestion, Qdrant is queried with a high similarity threshold (cosine > 0.92) to find visually similar files. This supplements pHash-based dedup for cases where perceptual hashing misses (e.g., significant crops). See Content Pipeline for the full dedup flow.
