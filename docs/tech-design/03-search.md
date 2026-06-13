# Search & Discovery

## Hybrid Search

### Candidate Retrieval

Every search query may use several retrieval paths in parallel:

1. **Semantic:** Qdrant returns top candidates ranked by embedding cosine similarity.
2. **Text:** Meilisearch returns top candidates ranked by text relevance (`_rankingScore`, 0–1).
3. **Popularity/trending:** PostgreSQL/materialized views provide fallback and boosting signals.
4. **Personal recommendations:** Qdrant recommend/search APIs provide user-history candidates for feed surfaces and optional query-independent blending.

Both engines return a fixed-size candidate pool (configurable, default: 200 candidates each). Results are merged by `meme_id` and scored with configurable weights. Example shape:

```text
combined_score = w_semantic × semantic_score
               + w_text × text_score
               + w_popularity × normalized_popularity
               + w_personal × personal_score
```

Weights are **configuration and experiment inputs**, not fixed truth. Every response/log entry that feeds analytics should include `algorithm_version`, active weights, score components, and result attribution.

### Access-Aware Search Scope

Search must be user-aware before final display data is returned. The same service contract is used by FastAPI, SvelteKit, Mini App, and aiogram bot.

Scopes:

- `public` / `common`: public catalog only
- `private`: non-public memes in collections accessible to the current user
- `all`: public + accessible private/shared collections
- `collections`: explicit `collection_ids[]` selected by the user

Access control rules:

- Qdrant/Meilisearch can prefilter by payloads where possible (`is_public`, `author_user_id`, collection payload hints), but **PostgreSQL is the final authority**.
- Candidate IDs from search indexes must be filtered through collection membership/ownership before DTOs are returned.
- Collection filters are ignored or rejected for unauthenticated users without a guest/full user row.
- Cache keys, if used, must include the normalized query, every content filter, viewer/user identity, scope, normalized collection ids, NSFW allowance or user preference, and algorithm/version fields to prevent private result leaks.

### Collection Filters

Web search exposes collection filtering:

- public/common only
- private/shared only
- all accessible
- specific selected private/shared collections

Bot inline search normally uses `all` after resolving/creating the Telegram full account. PM/Mini App surfaces may later expose explicit collection selection.

### Paginating Hybrid Results

True cursor-based pagination is hard on a score computed from independent engines because neither engine knows the other's scores.

Baseline MVP may use bounded offset pagination with over-fetch + deterministic merge/rerank. This is acceptable while candidate pools are small and traffic is low.

Optional stabilization when ranking becomes more expensive or pagination instability becomes visible: **cached candidate pool**.

1. On the first page request, execute retrievals, merge, score, sort.
2. Cache the ordered list of `(meme_id, combined_score, attribution)` in Redis.
3. Subsequent page requests read from the cache and slice by offset.

Candidate-pool cache key must include: normalized query, content filters, viewer/user identity, search scope, normalized collection ids, NSFW allowance or user preference, and `algorithm_version` plus any ranking/version fields that affect candidate selection. TTL should be short (60–120s). This cache is an optimization, not a correctness dependency for the first implementation.

Even when a cached candidate pool is used, those cached IDs are only an optimization. The final PostgreSQL access predicate and NSFW/content filters must still run before DTOs are returned so stale cache/index entries cannot leak private memes.

### Degraded Mode and Fallbacks

Fallbacks should be explicit and observable, not silent behavior changes. Search responses/logs should record `degraded=true` and missing components when a source is skipped.

| Failure | Behavior |
|---------|----------|
| Query embedding provider fails | Skip semantic query path; use text + popularity/trending. |
| Qdrant down | Text + popularity/trending. Similar/recommendation endpoints fall back to tag-related/trending with attribution. |
| Meilisearch down | Semantic + popularity/trending. Text-specific ranking unavailable. |
| Both search engines down | Trending/popular materialized view fallback. |
| Redis down | Recompute; no candidate-pool cache/rate-limit cache. Search correctness should remain intact. |

Circuit breakers can protect Qdrant/Meilisearch/Voyage from repeated failing calls. Exact thresholds are operational config. Reweighting uses the same configurable weight model with missing components zeroed or renormalized.

## Qdrant

Single collection `meme_files`. One point per `MemeFile` that the pipeline has
classified and synced.

- **Vector:** Voyage AI `voyage-multimodal-3.5`, 1024 dimensions, cosine distance
- **Payload:** safe PostgreSQL-derived metadata for candidate prefiltering and
  ranking: `meme_id`, `meme_file_id`, `search_index_algorithm_version`,
  `is_public`, `author_user_id`, `media_type`, `language`, `is_nsfw`, `tags`,
  `template_id`, `template_slug`, `seo_page_slug`, `popularity_score`,
  `like_count`, `quality_score`, `created_at`, `updated_at`, collection id
  buckets by visibility, shared collection ids, collection owner ids, and
  collection member ids.
- **Payload indexes:** should be created on fields used by Qdrant-side
  prefilters. These indexes are performance hints only; PostgreSQL filters are
  still the authorization boundary.

Used for: semantic search, similar memes, personalized feed (recommend API / vector search from positives), and deduplication during ingestion (high-threshold similarity search).

## Meilisearch

Single index `memes`. The current pipeline writes one document per `MemeFile`,
keyed by the `meme_file_id` hex string, so operator lookups and Qdrant point ids
share the same file identity.

- **Searchable:** `ocr_text` plus tag/template terms already present on the
  document. SEO prose/modeling fields other than `seo_page_slug`, moderation
  notes, invite data, and raw auth/session data are intentionally not indexed.
- **Filterable/prefilter hints:** `is_public`, `is_nsfw`, `media_type`,
  `language`, `tags`, `template_slug`, `author_user_id`, collection id buckets
  by visibility, shared collection ids, collection owner ids, and collection
  member ids. Collection roles are deliberately not indexed; PostgreSQL applies
  the final permission predicate.
- **Sortable/ranking hints:** `popularity_score`, `like_count`, `quality_score`,
  `created_at`, `updated_at`, and `search_index_algorithm_version`.
- **Russian stop words** configured
- **Typo tolerance** enabled (min 4 chars for 1 typo, 8 for 2)

Search results return `meme_id` lists from Meilisearch/Qdrant, then fetch full display data from PostgreSQL.

## Search Result Caching

Redis search/candidate caching is optional for MVP. If enabled, cache keys must include normalized query text, every content/access-shaping filter, viewer identity, scope, normalized collection ids, NSFW allowance or user preference, and algorithm/version fields. It must never be possible for a cached result containing user A's private memes to be served to user B, and cached candidate IDs must still be filtered through PostgreSQL before response DTOs are built.

## Sync Strategy

**Event-driven:** The content pipeline publishes sync dispatch events to RabbitMQ. Qdrant and Meilisearch sync consumers each bind their own queue, processing independently with retries via dead letter exchanges.

- `classify` success → Qdrant consumer syncs embedding + payload; Meilisearch consumer syncs a text document
- Meme-level field changes (popularity, likes, tags, access/publicity) → API publishes update event → both sync consumers update
- Like count and popularity changes → batched sync via Scheduler (not per-like)

Every sync attempt rebuilds its payload/document from PostgreSQL at consume time
via `load_search_index_state`; event payloads carry routing identity, not access
or ranking truth. This keeps public/private flips, collection membership or
visibility changes, SEO slug/template edits, and popularity/like updates safe to
replay without trusting stale snapshot JSON.

**Full resync/payload rebuild today:** enumerate ready `meme_file_id` values and
feed the per-target batch replay routes in bounded chunks. The runtime reloads
the current canonical row before each Qdrant/Meilisearch write. Qdrant alias
swaps remain a future zero-downtime optimization for embedding-model upgrades,
but the current safe metadata rebuild path is replay/batch replay through the
pipeline service.

## Pagination

| Context | Strategy | Details |
|---------|----------|---------|
| Search results | Offset or optional cached candidate pool | Deterministic merge/rerank; Redis pool added when needed for stable deep pagination |
| Collection browsing | Cursor-based | `(added_at, meme_id)` |
| Trending / feeds | Cursor-based | `(score, meme_id)` or materialized-view rank |
| Recommendations | Cursor-based/bounded pages | Qdrant recommend/search result pages plus DB access filtering |

## Recommendations

### Similar Memes

Service endpoint/function accepts a source `meme_id`, loads the primary file embedding, queries Qdrant for nearest neighbors, filters by access/NSFW, excludes the source meme, and returns result DTOs with similarity scores and attribution.

Fallback order:

1. Similarity results from Qdrant.
2. Tag/template-related accessible memes when no embedding/Qdrant is available.
3. Trending/popular fallback.

### Personalized Feed

Recommendation service builds a short-term user preference vector/candidate request from recent positive events:

- strong positives: favorite/like, save, pin, download, Telegram send/chosen inline result
- medium positives: detail view, repeated view, collection add
- weak/neutral signals: impression without click, later used for evaluation/reranking

The service calls Qdrant recommend/search APIs using recent positive examples, filters access/NSFW, excludes already-seen or recently-impressed memes where practical, and falls back to trending for cold start.

Personalized recommendations are used:

- on the home page feed;
- as a small blend in the meme detail related/similar section;
- as an optional boost in empty-query bot/Mini App discovery.

### Trending

Uses materialized-view backed public trend rankings and popularity snapshots. Default feed for users without interaction history.

## Interaction Attribution

Search/recommendation service should return enough metadata for event tracking:

- request id
- rank
- source algorithm/reason
- source meme id for related sections
- score and score components
- query/filter/scope/collection context

Frontend and bot presentation layers must pass this metadata back when recording impressions, views, detail clicks, sends, downloads, saves, and shares.

## Russian Query Translation

Behind a feature flag. For Russian queries, optionally translate to English via Google Translate and search both, merging results. Improves recall on English-text memes.

## Deduplication Search

After the embed stage computes a file's embedding, Qdrant is queried with a high similarity threshold (cosine > 0.92) to find near-duplicate memes that pHash missed (e.g., significant crops, overlays). Matches trigger auto-merge. See [Content Pipeline: Phase 2 Dedup](04-content-pipeline.md#phase-2-embedding-based-merge-post-embed) for the full flow.
