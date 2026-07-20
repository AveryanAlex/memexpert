# Search & Discovery

## How Search Works

Search combines full-text search (OCR text, tags, captions, and curated
same-language aliases in Meilisearch), semantic search (Voyage AI multimodal
embeddings via Qdrant), popularity/trending signals, and user-specific
recommendation signals. Meilisearch synonym expansion is not stemming or a
general morphology engine: operators must curate useful lexical and meme-culture
variants explicitly, such as Russian inflections or `жаба,лягушка`.

Ranking weights are **tunable algorithm parameters**, not product requirements. The initial implementation should expose/configure weights, record `algorithm_version`, and log per-result score components so they can be tuned after real traffic and analytics exist.

`personalized_v2` is the repository target contract. Its code and schema may be
present before it is activated on the live beta: activation requires the
backend/frontend compatibility rollout, Qdrant primary-file payload backfill,
shadow evaluation, and a stable-user-hash canary. Documentation of this
contract is not evidence that those rollout steps have happened.

## Search Scope

Search must work across both public and user-accessible private content:

- **Public/common catalog** — public memes crawled from source channels.
- **User private data** — non-public memes in collections the user can access.
- **Shared collections** — private collections where the user is owner/editor/viewer.
- **All accessible memes** — public + every private/shared collection the user can access.

The bot and web share the same service-level search contract. Telegram inline search resolves/creates the full account from `telegram_id` first, then searches the user's accessible scope. The bot does not need to visually distinguish public vs private results; the web should subtly indicate private/shared results.

Private authorization comes only from collection ownership/membership. Upload provenance does not grant a separate author shortcut, and non-admin search/detail/media responses never reveal uploader IDs or private source records. A public meme saved in Favorites remains public and is excluded from the `private` scope, though it remains available in `all` and explicit collection scopes.

Any cached search candidate pool or result key must include normalized query text, content filters, viewer identity, scope, normalized collection ids, NSFW allowance or user preference, and algorithm/version fields. Cached candidates are only an optimization; final PostgreSQL access filtering still decides what can be returned.

## Filters

In the website search sidebar, available at launch:

- **Search scope** — public/common only, private/shared only, all accessible, or a specific set of collections
- **Collections** — multi-select specific private/shared collections the user can access
- **Tags/categories** — select one or more
- **NSFW** — show/hide (respects user's default from settings)
- **Media type** — image / GIF / video
- **Language** — Russian / English / any

Collection filters are URL-backed like the other filters so search result links remain shareable within the permissions of the receiving user.

## Trending

Computed from positive source view/reaction/repost increases above each counter's running high watermark plus MemeExpert views, sends, saves, and likes. Downloads and impressions remain reportable but have zero public-ranking weight.

Trending can be materialized/precomputed and used as the cold-start fallback for users without enough interaction history.

## "Meme of the Day"

Automatically selected from recent high-quality, non-NSFW candidates using popularity/trending growth and novelty. Displayed prominently on the home page. No manual override in V1 (could add admin override later).

---

## Recommendations

### Similar Memes

Embedding similarity from Qdrant. Shown on meme pages for all users. The minimum acceptable implementation uses the source meme's primary file vector and returns similar accessible memes filtered by NSFW/access rules.

Candidate generation remains source-meme-led. The complete bounded candidate
pool—including tag-overlap, same-template, and public-popular backfill—receives
this light rerank:

```text
0.75 similarity + 0.10 taste + 0.05 quality + 0.05 popularity + 0.05 freshness
```

Similarity intent remains dominant. Only vector candidates have a calibrated
`similarity` value; fallback candidates use a neutral zero for that term rather
than treating tag/template membership as vector similarity. Their deterministic
tier scores still govern candidate admission, while taste, quality, popularity,
and freshness can reorder the resulting bounded pool. Each signed attribution
identifies `visual_similarity`, `tag_overlap`, `same_template`, or
`public_popular` through a typed candidate-source contribution in addition to
server-authored `source_algorithm`, reason, related-source identity, and score
components. Similar URLs retain bounded offset pagination for shareability.

### Text Search

Text-search candidate generation remains query-only: personal history must not
inject unrelated candidates into an explicit query. The optional light rerank
contract is:

```text
0.45 semantic + 0.35 text + 0.10 taste + 0.05 popularity + 0.05 quality
```

Search URLs remain offset-based and shareable. Public Trends, taxonomy pages,
and SEO pages remain global and deterministic and never receive personalized
ordering.

### Personalized Feed

Home and Mini App Discover use the full `personalized_v2` contract for guests
and full users. PostgreSQL is authoritative for profiles, exact cooldowns,
visibility, moderation, and features; Qdrant supplies bounded candidates and
Redis freezes presentation order.

Signal policy:

| Signal | Weight and behavior |
|---|---:|
| Current Favorite, Save, or Pin | `5`; contributes only while durable state exists |
| Download, Send/Share, inline chosen/sent | `4` |
| Engaged view | `2` |
| Detail view/click | `1` |
| Impression only | `0`; cooldown and evaluation only |
| Recent successful search vector | `3`; current-intent context only |
| Favorite/Save/Pin removal | Cancels durable contribution; never becomes a negative |

Ignored impressions are not dislikes. Raw events remain in PostgreSQL
indefinitely, while online representations are bounded and time-decayed:

- short term: 24-hour half-life and at most a seven-day event window;
- current intent: a Redis vector with a 30-minute half-life and two-hour TTL;
  Redis stores no raw query;
- long term: current durable preferences plus high-intent history with a
  90-day half-life and no history cutoff, materializing at most 500 meme
  signals in PostgreSQL.

Clustering activates only after 20 distinct strong-positive memes. It uses
deterministic cosine farthest-first initialization and at most five spherical
centroid iterations, produces two to four clusters, drops clusters below three
items, and always retains a global centroid. Sparse users use direct positive
examples plus the global centroid.

Candidate budgets are 120 short-term, 120 current-intent, up to 240 across the
long-term global/cluster sources, 120 recent multi-positive, 80 trending, and
40 exploration candidates. The union is capped at 600. Weighted reciprocal-rank
fusion uses constant `60`; cluster contributions are normalized as a group so
additional clusters cannot dominate.

Feature loading and formula scoring are bounded by the fused union of at most
600 candidates. Only the best 200 scored candidates enter diversity reranking,
and the frozen pool remains capped at 200. The starting configurable home formula
is:

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
platform_response`. Freshness uses latest live upstream publication time with a
45-day half-life, falling back to meme creation. Missing derived features are
neutral `0.5` with explicit coverage, never zero quality. Popularity alignment
is neutral until the user has five qualifying strong positives.

One quality-controlled exploration position is reserved per 20 results when an
eligible item exists. Exploration is recent public content, below the top
popularity quintile, above source/technical quality floors, and subject to all
normal safety and cooldown checks.

Greedy diversity reranks the top 100: semantic similarity to the last ten,
source repetition in the last five, and template repetition in the last three
are penalized. A source and template are capped at two per 20, with deterministic
relaxation only when necessary to fill the page. This presentation control does
not replace exact/near-duplicate merging.

The ordered 200-item pool is frozen in Redis for two hours. A signed opaque
cursor carries the pool identity, next index, and a compact exact set of the
already-returned pool meme IDs; it is bound to viewer, filters, language/NSFW
policy, and algorithm version and is capped at 8 KiB. The bounded ID set lets a
continuation switch to PostgreSQL keyset fallback without repeating results if
Redis disappears before impression telemetry is durable. Every continuation
rechecks public visibility, moderation, NSFW, and the exact PostgreSQL cooldown
state. At most four live pools are retained per viewer by default; freezing a
fifth atomically evicts the oldest pool, whose otherwise-valid cursor then
returns `410 feed_cursor_expired`. The legacy `offset` is accepted for one
compatibility release and is mutually exclusive with `cursor`.

Browser Back restoration treats session storage only as a presentation hint.
Before installing any saved cards, the client submits at most 200 meme IDs and
their opaque Home attribution tokens to
`POST /api/v1/memes/home-feed/reauthorize`. The server verifies token expiry, viewer,
meme, and `web_home` surface and rechecks current PostgreSQL public,
moderation, NSFW, and filter state, returning only the still-authorized subset.
An invalid/expired restore is discarded rather than rendered under a new
viewer or stale access policy.

Before cursorless personalized generation, Home performs a bounded Redis
availability preflight. If Redis is unavailable, or pool freezing later fails,
Home uses `public_meme_trends_mv` with a PostgreSQL `(trending_score, meme_id)`
keyset cursor and hydrates only the returned rows; it does not spend the
candidate-generation budget, recompute an unstable personalized offset page,
or load the catalog. If Redis instead fails during a frozen-pool continuation,
the signed cursor's exact returned-ID set is carried into the keyset session so
the fallback preserves rank progress and cannot replay an earlier pool page.

Exact state hard-excludes a meme for 72 hours after an impression and seven
days after a strong action, regardless of how many other impressions exist.
Guest-to-full merging takes the minimum first-seen time, maximum event
timestamps, and summed impression count, then invalidates affected profiles and
feed pools.

Empty Telegram inline queries place explicit pins first, including currently
accessible private/shared pins, and then use the public-only Home recommender.
Both tiers obey the linked viewer's NSFW policy and Telegram sendability
filtering. The first request starts the Home continuation session even if pins
fill that page, and a compact Redis-backed next-offset cursor resumes the same
pin/Home session. If that compact continuation cannot be saved, the already
computed page is still returned without `has_more` or a next offset; it never
switches empty-query discovery to an unstable numeric offset.
Non-empty inline queries remain query-led search.

### Trending / Cold Start

Fastest growth in channel reposts + platform engagement. Default feed for users without enough history.

## Result Attribution

Every returned result carries a server-issued, viewer- and meme-bound
`attribution_token`. Its signed claims contain request/impression identity,
surface, global rank, candidate-source contributions, algorithm/profile
versions, score, related-source identity, and expiry. It intentionally omits raw
query text and collection identifiers. Consumers render none of these internals
and submit the opaque token with later events or mutations. Legacy nested
client-authored attribution is accepted only during the backend-first rollout,
marked untrusted, and removed after compatible clients ship.

## Rollout and Non-goals

Backend compatibility and trusted telemetry ship before cursor-aware clients.
`personalized_v2` then runs in shadow mode, followed by a stable-user-hash
canary before full activation. Sparse beta traffic is insufficient for reliable
online A/B conclusions, so offline chronological replay selects algorithms and
canarying limits operational risk.

Runtime defaults are fail-safe: recommendations are disabled, shadow mode is
on, and the canary is zero percent. Shadow evaluation and each canary expansion
require explicit configuration; Search and Similar apply no profile-derived
taste term for viewers who are not eligible for the same serving canary. When
explicitly enabled, shadow candidate generation is bounded to 0.25 seconds by
default and can record evaluation diagnostics but never changes the returned
trending page.

The serving gate applies when a cursorless Home session starts. A previously
issued, still-valid personalized pool cursor is verified and continued before
the gate is reevaluated, preserving its frozen order for up to the pool TTL when
the algorithm version is unchanged. Reducing the canary or switching to shadow
therefore stops new personalized sessions but is not an immediate revocation of
already-issued pools.

There is no inferred dislike, “Not interested” action, personalization reset,
opt-out, event deletion/archival, collaborative filtering, matrix
factorization, BPR/LightFM, or learned ranker in this roadmap. Home returns only
public catalog content, although any accessible private/shared or NSFW
interaction may shape taste; output authorization and the active NSFW filter
remain authoritative.
