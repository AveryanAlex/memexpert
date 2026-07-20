# Search & Discovery

## Hybrid Search

### Candidate Retrieval

Every search query may use several retrieval paths in parallel:

1. **Semantic:** Qdrant returns top candidates ranked by embedding cosine similarity.
2. **Text:** Meilisearch returns top candidates ranked by text relevance (`_rankingScore`, 0–1).
3. **Popularity/trending:** PostgreSQL/materialized views provide fallback and boosting signals.
4. **Taste rerank:** an available persisted profile contributes a small score
   only after query-led candidates are collected; it never injects unrelated
   candidates into an explicit text search.

Both engines return the same fixed-size candidate pool, controlled by
`SEARCH_CANDIDATE_POOL_LIMIT_PER_SOURCE` (default: 200 candidates from each
engine, bounded to 100–500). The pool size is independent of the requested page
`limit` and `offset`. Results are merged by `meme_id` and scored with
configurable weights.

The `personalized_v2` starting contract for Text Search is `0.45 semantic +
0.35 text + 0.10 taste + 0.05 popularity + 0.05 quality`. Public Trends,
taxonomy, and SEO surfaces remain global and deterministic. Search keeps its
bounded offset URLs for shareability. Search attribution uses the dedicated
`*_hybrid_taste_v2` ranking version rather than the underlying index schema
version; Similar uses `*_similar_taste_v4`, so either formula can evolve
without being mistaken for an index-only change.

Weights are **configuration and experiment inputs**, not fixed truth. Every response/log entry that feeds analytics should include `algorithm_version`, active weights, score components, and result attribution.

### Access-Aware Search Scope

Search must be user-aware before final display data is returned. The same service contract is used by FastAPI, SvelteKit, Mini App, and aiogram bot.

Scopes:

- `public` / `common`: public catalog only
- `private`: non-public memes in collections accessible to the current user
- `all`: public + accessible private/shared collections
- `collections`: explicit `collection_ids[]` selected by the user

Access control rules:

- Qdrant/Meilisearch can prefilter by `is_public` and collection payload hints, but **PostgreSQL is the final authority**.
- Candidate IDs from search indexes must be filtered through collection membership/ownership before DTOs are returned.
- There is no author/owner authorization shortcut. Public state or collection access is required, including for uploaders.
- `private` scope contains only accessible non-public memes. A public meme saved in Favorites remains in public/all/collection scopes, not private scope.
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

Baseline MVP uses bounded offset pagination over a fixed candidate pool with a
deterministic merge/rerank. The response `total` is the number of unique
candidates in that bounded union that pass final PostgreSQL access and content
filters; it is not an exact corpus-wide match count. With the default, at most
400 provider hits are merged, and cross-engine duplicates reduce the candidates
that enter PostgreSQL filtering.
Because retrieval depth and score normalization do not depend on the requested
page size, `total`, scores, and ordering remain stable across `limit` and
`offset` while the provider indexes and PostgreSQL ranking inputs are unchanged.
The final sort uses `meme_id` as a tie-breaker after score, popularity, and
creation time. Requests beyond the bounded total return an empty page rather
than increasing provider retrieval depth.

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
| Qdrant down | Search uses text + popularity/trending, and Similar falls back through tag, template, and public-popular tiers. With Redis healthy, Home omits vector sources but still fuses, ranks, and freezes a bounded pool of PostgreSQL trend/exploration candidates; Redis-unavailable Home uses its signed PostgreSQL keyset fallback. All degraded paths retain attribution. |
| Meilisearch down | Semantic + popularity/trending. Text-specific ranking unavailable. |
| Both search engines down | Trending/popular materialized view fallback. |
| Redis down | Search recomputes without its optional cache. Home serves `public_meme_trends_mv` through a PostgreSQL keyset cursor and hydrates only the requested page. |

Circuit breakers can protect Qdrant/Meilisearch/Voyage from repeated failing calls. Exact thresholds are operational config. Reweighting uses the same configurable weight model with missing components zeroed or renormalized.

## Qdrant

The current implementation uses one configured collection (default
`memexpert-memes`) with one point per classified and synced `MemeFile`.

- **Vector:** Voyage AI `voyage-multimodal-3.5`, 1024 dimensions, cosine distance
- **Payload:** safe PostgreSQL-derived metadata for candidate prefiltering and
  ranking: `meme_id`, `meme_file_id`, `search_index_algorithm_version`,
  `is_public`, `is_primary_file`, internal `uploader_user_ids`, `media_type`,
  `language`, `is_nsfw`, `tags`,
  `template_id`, `template_slug`, `seo_page_slug`, derived `popularity_score`,
  `like_count`, `quality_score`, `created_at`, `updated_at`, collection id
  buckets by visibility, shared collection ids, collection owner ids, and
  collection member ids.
- `uploader_user_ids` exists only to prefilter private approximate-dedupe candidates. It is not used for user-facing access checks and is not returned by public APIs.
- `is_primary_file` is true only for `Meme.primary_file_id`. Recommendation
  retrieval requires it so alternative crops cannot occupy multiple candidate
  slots; semantic deduplication and general file-level search may still inspect
  every file.
- **Payload indexes:** collection ensure idempotently provisions keyword indexes
  on `search_index_algorithm_version`, `uploader_user_ids`, `media_type`,
  `language`, `tags`, `collection_ids`, `collection_owner_user_ids`, and
  `collection_member_user_ids`, plus boolean indexes on `is_public`,
  `is_primary_file`, and `is_nsfw`. These are performance hints only;
  PostgreSQL remains the authorization and final filter boundary.

The long-lived sync adapter caches successful collection provisioning. If an
upsert receives `404` after the collection was replaced out of band, it clears
that readiness flag, reruns collection ensure for all eleven payload indexes,
and retries the upsert once; persistent provider failures remain durable sync
failures rather than an unbounded retry loop.

The recommendation adapter accepts a batch of named nearest-neighbor requests
for short-term intent and long-term profile centroids plus a Qdrant
`RecommendQuery(strategy=best_score)` over recent positive primary-file IDs.
One shared filter enforces public/content preconditions, primary-file-only
candidates, and source-point exclusions. It returns per-source candidate ranks
and scores; Qdrant does not apply dynamic authorization, reciprocal-rank fusion,
item features, or the final formula.

Nearest-vector and `best_score` requests run as separate batches. A missing or
stale positive seed can therefore degrade the multi-positive source to an empty
result without discarding already successful short-/long-term nearest results.
The lightweight API and Telegram adapters share one process-wide lazy Qdrant SDK
client instead of opening a transport pool per request/update; FastAPI lifespan
and bot shutdown close and clear that shared client.

Used for: semantic search, similar memes, personalized retrieval, and
deduplication during ingestion (high-threshold similarity search).

## Meilisearch

Single index `memes`. The current pipeline writes one document per `MemeFile`,
keyed by the `meme_file_id` hex string, so operator lookups and Qdrant point ids
share the same file identity.

- **Searchable:** `ocr_text` plus tag/template terms already present on the
  document. SEO prose/modeling fields other than `seo_page_slug`, moderation
  notes, invite data, and raw auth/session data are intentionally not indexed.
- **Filterable/prefilter hints:** `is_public`, `is_nsfw`, `media_type`,
  `language`, `tags`, `template_slug`, collection id buckets
  by visibility, shared collection ids, collection owner ids, and collection
  member ids. Collection roles are deliberately not indexed; PostgreSQL applies
  the final permission predicate.
- **Sortable/ranking hints:** derived `popularity_score`, `like_count`, `quality_score`,
  `created_at`, `updated_at`, and `search_index_algorithm_version`.

The document-sync client explicitly reconciles filterable attributes when it
first ensures the index. Stop words, custom typo-tolerance thresholds, and
searchable attributes otherwise use Meilisearch defaults.

Curated synonyms have a separate control plane. PostgreSQL stores independent
English and Russian catalogs with one mutable draft and immutable published
history. The compiler normalizes newline/comma mutual groups into deterministic
directional maps, records its compiler version and validation snapshot, and
warns when a phrase exceeds Meilisearch's three-token source-key limit. Long
phrases may still be targets. Publishing rejects an empty effective catalog,
cross-locale key conflicts, and unconfirmed large key reductions.

Publishing changes only durable desired state. The singleton scheduler is the
sole settings writer: on startup and periodically it combines all published
locale snapshots, recompiles their source to verify compiler version, snapshot,
and hash integrity, compares desired and observed provider hashes, submits one
full asynchronous replacement, waits for the Meilisearch task, and records the
applied revision set or a safe retryable failure. Every state write verifies
that the publication generation is still current; a superseded in-flight task
cannot overwrite a newer publish and is followed by an immediate convergence
pass. Every locale and the combined map must be non-empty. The per-run HTTP
client is closed after reconciliation. Operators manage drafts, history,
publication, and retry state at
`/admin/search/synonyms`. Exact query-side expansion behavior and limitations
are documented in
[Meilisearch Synonym Behavior](../research/meilisearch-synonym-behavior.md).

Search results return `meme_id` lists from Meilisearch/Qdrant, then fetch full display data from PostgreSQL.

## Search Result Caching

Redis search/candidate caching is optional for MVP. If enabled, cache keys must include normalized query text, every content/access-shaping filter, viewer identity, scope, normalized collection ids, NSFW allowance or user preference, and algorithm/version fields. It must never be possible for a cached result containing user A's private memes to be served to user B, and cached candidate IDs must still be filtered through PostgreSQL before response DTOs are built.

## Sync Strategy

**Event-driven:** The content pipeline publishes sync dispatch events to RabbitMQ. Qdrant and Meilisearch sync consumers each bind their own queue, processing independently with retries via dead letter exchanges.

- `classify` success → Qdrant consumer syncs embedding + payload; Meilisearch consumer syncs a text document
- Meme-level field changes (likes, tags, access/publicity) → API publishes update event → both sync consumers update
- Source engagement captures and analytics events update derived popularity/read-model values; search payload rebuilds compute `popularity_score` from those sources instead of reading a `memes.popularity_score` column
- Like count changes → batched sync via Scheduler (not per-like)

Every sync attempt rebuilds its payload/document from PostgreSQL at consume time
via `load_search_index_state`; event payloads carry routing identity, not access
or ranking truth. This keeps public/private flips, collection membership or
visibility changes, SEO slug/template edits, and derived popularity/like updates safe to
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
| Similar memes | Bounded offset pages | Stable ordered pool of at most 200 candidates; 12-result UI pages and empty responses beyond `total` |
| Collection browsing | Cursor-based | `(added_at, meme_id)` |
| Trending / public fallback | Cursor-based | PostgreSQL `(score, meme_id)` keyset or materialized-view rank |
| Home recommendations | Signed pool cursor | Frozen Redis pool identity/index; PostgreSQL MV keyset cursor when Redis is unavailable |

## Recommendations

### Similar Memes

`GET /api/v1/memes/{meme_id}/similar` accepts `limit` and `offset` and returns the existing discovery-page DTO. Candidate retrieval is independent of those page parameters: the service builds one deterministic ordered pool capped at 200 candidates, slices it by `offset`/`limit`, and hydrates only the returned page. `total` is the bounded pool size, not a corpus-wide count; offsets at or beyond `total` return an empty page. Ordering, `total`, scores, and global ranks remain stable across page sizes while Qdrant, PostgreSQL rows, and the public trend materialized view are unchanged.

The API, rather than the frontend, fills the pool in this order:

1. A fixed Qdrant prefix for the source file embedding, bounded to four times the 200-result pool size.
2. Public memes with overlapping tags.
3. Public memes using the same template.
4. Public-popular memes from `public_meme_trends_mv`.

Each tier excludes the source and IDs already selected by an earlier tier. PostgreSQL applies the final public, NSFW, and content filters even to Qdrant candidates. Fallback tiers order IDs deterministically by their tier signal, popularity, creation time, and meme ID. PostgreSQL returns no more than the remaining slots in the 200-item lightweight reference pool; the service scores that full bounded pool, slices it, and hydrates at most the requested result page.

Candidate generation remains source-meme-led. `personalized_v2` applies this
light rerank to the complete bounded pool, including fallback tiers:

```text
0.75 similarity + 0.10 taste + 0.05 quality + 0.05 popularity + 0.05 freshness
```

Similarity remains the dominant contract. Only Qdrant candidates receive a
non-zero calibrated `similarity` component. Tag-overlap, same-template, and
public-popular candidates use zero for that component rather than presenting a
tier signal as vector similarity; their deterministic source scores govern
admission, while taste, quality, popularity, and freshness rerank the complete
pool. Stable pre-rerank pool position breaks equal-score ties. The endpoint
keeps its bounded offset URLs, and a profile/index refresh may still move the
best-effort pool between requests.

Every page carries a server-issued attribution token binding source meme,
algorithm/profile versions, request/impression identity, score components,
server-authored source algorithm, and global rank so appended cards remain
attributable. Its typed candidate-source contribution distinguishes
`visual_similarity`, `tag_overlap`, `same_template`, and `public_popular` and
preserves the source-native rank and score.
Expanded fields remain compatibility-only and untrusted when sent back by a
client. Missing embeddings, unavailable or failed Qdrant, filtered-out semantic
candidates, and partially filled semantic pools all degrade through the same
API-owned tiers.

### MV-Backed Public Popular Paging

Public-popular fallback paths for Search, Browse, tag pages, and template pages use the same `public_meme_trends_mv` row-ID pager: PostgreSQL orders and slices IDs first, and the service hydrates only the requested page. These surfaces retain their existing pagination totals and visibility semantics; private and mixed-access ranking paths are unchanged.

The materialized view is refreshed on the scheduler's default five-minute cadence, so offset pages are eventually consistent rather than snapshot-isolated. A refresh can move candidates between requests; deterministic ties and client-side ID deduplication make this best effort, and no snapshot token or Redis dependency is introduced. This is a read-path optimization only: it does not add embedding backfills, ingestion or admin recovery behavior, or new operational logging.

### Personalized Feed

`services/recommendations/` owns `personalized_v2` signals, exact state,
profiles, candidate fusion, item features, ranking, diversity, attribution, and
feed sessions. `MemeSearchService` remains responsible for index search and
bounded PostgreSQL hydration. PostgreSQL is authoritative for users,
interactions, access, cooldowns, features, and profile vectors; Qdrant remains a
candidate index and Redis a disposable presentation cache.

The shared signal policy is current Favorite/Save/Pin `5`,
download/Send/Share/inline chosen or sent `4`, engaged view `2`, detail
view/click `1`, and impression `0`.
Removal cancels durable preference state and never becomes a negative. The
online representations are:

- short-term: 24-hour half-life over at most seven days of item signals,
  including current Favorite/Save/Pin rows added inside that window;
- current intent: successful search vectors at weight `3`, combined in Redis
  with a 30-minute half-life and two-hour TTL, without raw query text;
- long-term: durable preferences plus high-intent history at a 90-day half-life
  and no history cutoff, materializing the top 500 meme signals and vectors in
  PostgreSQL.

At 20 distinct strong positives, deterministic cosine farthest-first
initialization and up to five spherical-centroid iterations produce two to four
clusters. Clusters under three items are discarded and a global centroid is
always retained. Users below the threshold use recent direct positives plus one
global long-term centroid.

Candidate source budgets are 120 short-term, 120 current-intent, up to 240
across long-term global/cluster queries, 120 recent multi-positive
`best_score`, 80 MV-trending, and 40 exploration. The deduplicated union is
capped at 600. Weighted reciprocal-rank fusion uses constant `60`; all cluster
rankings share one normalization group so users with more clusters do not
receive more total long-term influence. At most 200 candidates continue to
diversity reranking and the frozen pool; PostgreSQL feature/embedding loading
and formula scoring remain bounded by the preceding union of at most 600.

Persisted taste vectors are eligible only when both their embedding model and
profile base version match current configuration. Home, Search, and Similar
ignore stale rows; the bounded profile scheduler discovers and replaces them.

The configurable starting score is:

```text
0.40 personal_fit
+ 0.15 current_intent
+ 0.10 fused_candidate_score
+ 0.15 quality_prior
+ 0.10 freshness
+ 0.05 popularity_alignment
+ 0.05 exploration_bonus
```

`quality_prior = 0.40 source_quality + 0.30 technical_quality + 0.30
platform_response`. Item features come from
`public_meme_recommendation_features_mv`; missing values are neutral `0.5` with
coverage flags. Source metric coverage requires a successful engagement
snapshot with a measured view count, and technical-quality coverage requires a
successful primary-file transcode journal or attempt; non-null storage defaults
do not count as measurements. Percentile ties retain equal quantiles. Freshness
uses latest live-source publication with a 45-day half-life and falls back to
meme creation. Popularity alignment compares the item quantile with the
viewer's median strong-positive quantile and remains neutral until five
qualifying positives exist.

Greedy diversity reranks the top 100. It penalizes similarity to the last ten
selected items, repeated source within five, and repeated template within
three. Source and template are each capped at two per 20 results, with
deterministic relaxation only to fill the page. This is presentation diversity,
not a substitute for exact/near-duplicate merging. One eligible exploration
position is reserved per 20 results: it must be recent public content, have
source quality at least `0.55`, technical quality at least `0.50`, popularity
below quantile `0.80`, and pass every normal authorization, NSFW, moderation,
cooldown, and diversity check.

`user_meme_recommendation_state` enforces exact cooldowns independently of
bounded event reads: 72 hours after the latest impression and seven days after
the latest strong positive. The service does not infer dislike from an ignored
impression. Home output is public-only, although any accessible private/shared
or NSFW interaction may shape taste; current output NSFW and moderation policy
is rechecked in PostgreSQL.

The first Home request creates no more than a 200-item ordered Redis pool with a
two-hour TTL. Before a cursorless serving-eligible request spends profile or
Qdrant work, a Redis read preflight runs under
`RECOMMENDATION_REDIS_TIMEOUT_SECONDS` (default `0.5`).
`RecommendationFeedPageRead` returns `items`, request/feed-session identity,
`next_cursor`, `expires_at`, and `has_more`. The signed opaque cursor contains
`kind`, `version`, `mode`, keyed `viewer_key` and `filter_key` bindings,
`algorithm_version`, `next_index`, a compact exact set of already-returned pool
meme IDs, and `iat`/`exp`. Pool mode additionally carries `pool_id`;
PostgreSQL-trending mode instead carries `last_score` and `last_meme_id` for its
keyset. The returned-ID set is bounded by the 200-item pool and the complete
cursor by 8 KiB. The filter binding covers the active content filters,
language, and NSFW policy. Every page rechecks public status,
moderation, NSFW, and exact cooldown state, so an action after page one cannot
leak or skip arbitrary uncached items. Freeze retains at most
`RECOMMENDATION_FEED_POOL_MAX_PER_VIEWER` pools per viewer (default `4`) in a
creation-time sorted index and atomically deletes the oldest pool bodies beyond
that cap. An expired, missing, or retention-evicted pool returns `410
feed_cursor_expired`. Legacy `offset` is accepted for one compatibility release
and is mutually exclusive with `cursor`.

Session-storage Home restoration is not trusted hydration.
`POST /api/v1/memes/home-feed/reauthorize` accepts at most 200 saved meme/token
pairs, verifies each token's expiry, viewer, meme, and `web_home` surface, then
rehydrates only the subset that still passes current PostgreSQL public,
moderation, NSFW, and filter checks. The browser waits for that result before
installing restored cards; invalid/expired state is discarded.

If the Redis preflight, pool freeze, or continuation read is unavailable, Home
reads `public_meme_trends_mv` through a signed
PostgreSQL `(trending_score, meme_id)` keyset cursor and hydrates only the
requested page with algorithm version `public_trending_keyset_v1` and a typed
`trending` candidate-source contribution. A failed pool continuation transfers
its signed exact returned-ID set and next rank into the keyset cursor, avoiding
replays even before client impression delivery completes. With Redis available, Qdrant failure
or a cold-start profile instead omits unavailable vector sources and continues
through the normal bounded fusion/ranking/pool path using PostgreSQL trending
and exploration candidates; it does not discard those candidates or force a
keyset session. Neither path loads the catalog into Python. Guest-to-full merge
takes minimum first-seen, maximum event timestamps, and summed impression
count, then invalidates the target profile and both viewer feed-pool namespaces.

Empty Telegram inline queries put currently accessible explicit pins first,
including private/shared pins, then consume public Home ranking. Both tiers
apply the linked viewer's NSFW policy and Telegram sendability filtering. The
initial request starts the Home continuation session even when pins fill the
page, and a compact Redis-backed next-offset cursor resumes the same pin/Home
session. Served rows carry trusted algorithm/profile/candidate-source
dimensions; a compact
impression identity in Telegram result/callback IDs joins chosen/sent/send and
library-add events back to the matching served row. Non-empty inline requests
remain text-query-led.

Rollout is a three-part serving gate. Defaults are
`RECOMMENDATION_ENABLED=false`, `RECOMMENDATION_SHADOW_MODE=true`, and
`RECOMMENDATION_CANARY_PERCENT=0`; serving requires enabled, non-shadow, and a
stable-viewer bucket inside a nonzero canary. Enabled shadow work is cancelled
after `RECOMMENDATION_SHADOW_TIMEOUT_SECONDS` (default `0.25`) and returns the
global MV-backed page regardless of shadow success. Search and Similar retain
their global quality/popularity terms but zero profile taste outside the same
serving gate.

Cursor verification/continuation precedes that gate. A valid frozen pool (or
PostgreSQL trending cursor) whose algorithm binding still matches can continue
until expiry even after enabled/shadow/canary settings change. This preserves
session ordering; a gate reduction prevents new personalized pools but does not
revoke existing ones. Immediate revocation requires a separate, explicitly
authorized pool invalidation or versioned rollout.

This phase retains raw interaction history indefinitely and adds no inferred
negative, “Not interested,” personalization reset/opt-out, partition/archive or
deletion policy, collaborative filtering, matrix factorization, BPR/LightFM, or
learned ranker.

The repository contains this target contract, but schema/code presence is not
evidence that the live-beta migration, Qdrant payload backfill, index
provisioning, shadow run, canary, or feature activation has occurred.

### Trending

Uses materialized-view backed public trend rankings derived from source engagement snapshots and `analytics_events`. Default feed for users without interaction history.

## Interaction Attribution

Search and recommendation results include a signed server-issued
`attribution_token`, not trusted client-authored ranking facts. Claims bind the
viewer, meme, request/impression identity, surface, global rank, typed candidate
source contributions, algorithm/profile versions, score, related-source
identity, and expiry. They omit raw query text and collection identifiers. The
backend verifies signature, expiry, viewer, meme, and current access before an
event or mutation can use the attribution. A token issued to an anonymous public
reader may be accepted only by a guest-attributed event or write path to bridge
automatic guest bootstrap; tokens bound to any concrete viewer remain
non-transferable.

Frontend and bot clients treat the token as opaque and forward it with
impressions, engaged views, detail clicks, sends, downloads, saves, pins, and
related mutations. Legacy nested attribution is accepted only during the
backend-first compatibility window, marked untrusted, and must not override a
valid token.

## Evidence-Gated Vector Evolution

The active representation remains the existing 1024-dimensional Voyage
`voyage-multimodal-3.5` image-document vector, and Meilisearch remains the OCR
lexical engine. There is no deployed named-vector collection, Qdrant sparse OCR
index, or meme-level recommendation collection in this phase.

A chronological offline evaluator compares the current centroid, two-profile,
clustered, and multi-positive variants using Recall@K, NDCG@K, catalog coverage,
source/template concentration, and intra-list diversity. Add `ocr_dense` only
if it improves Recall@50 by at least 5% overall or 10% for text-heavy memes,
adds no more than 50 ms to p95 retrieval, and leaves projected Qdrant peak memory
below 70% of the 4 GB limit. If memory fails that gate, evaluate
512-dimensional Matryoshka vectors or scalar quantization first.

Before another item embedding is introduced, normalize storage into immutable
embedding artifacts plus file-to-artifact associations so identical OCR text is
reusable. A future migration then introduces a stable read alias, creates a
versioned named-vector collection and payload indexes, dual-writes new sync
events, backfills from PostgreSQL in bounded batches, verifies READY-primary
coverage/counts/dimensions/filters and sampled ranking parity, atomically
switches the alias, and retains the old collection for rollback. These are
Phase-3 steps, not a description of the repository's current collection or a
claim that any production alias/backfill has run.

## Russian Query Translation

Behind a feature flag. For Russian queries, optionally translate to English via Google Translate and search both, merging results. Improves recall on English-text memes.

## Deduplication Search

After the embed stage computes a file's embedding, Qdrant is queried with a high similarity threshold (cosine > 0.92) to find near-duplicate memes that pHash missed (e.g., significant crops, overlays). Candidate filtering is strict: public files can match only public memes; a private file can match only private memes whose Qdrant payload has exactly one `uploader_user_ids` value equal to its own sole uploader. PostgreSQL reloads and locks both memes and rechecks the same rule before merging, so a stale index cannot cross visibility or uploader boundaries. See [Content Pipeline](04-content-pipeline.md#post-embed-semantic-merge) for the full flow.
