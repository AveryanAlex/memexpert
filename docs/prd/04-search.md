# Search & Discovery

## How Search Works

Search combines full-text search (OCR text, tags, captions, Russian morphology via Meilisearch), semantic search (Voyage AI multimodal embeddings via Qdrant), popularity/trending signals, and user-specific recommendation signals.

Ranking weights are **tunable algorithm parameters**, not product requirements. The initial implementation should expose/configure weights, record `algorithm_version`, and log per-result score components so they can be tuned after real traffic and analytics exist.

## Search Scope

Search must work across both public and user-accessible private content:

- **Public/common catalog** — public memes crawled from source channels.
- **User private data** — the user's Favorites and private uploads.
- **Shared collections** — private collections where the user is owner/editor/viewer.
- **All accessible memes** — public + every private/shared collection the user can access.

The bot and web share the same service-level search contract. Telegram inline search resolves/creates the full account from `telegram_id` first, then searches the user's accessible scope. The bot does not need to visually distinguish public vs private results; the web should subtly indicate private/shared results.

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

Computed from: growth rate of reposts across channels (new source appearances in 24–48h) + growth in platform engagement (sends, saves, downloads, views, impressions, likes on MemeXpert). Both signals combined.

Trending can be materialized/precomputed and used as the cold-start fallback for users without enough interaction history.

## "Meme of the Day"

Automatically selected from recent high-quality, non-NSFW candidates using popularity/trending growth and novelty. Displayed prominently on the home page. No manual override in V1 (could add admin override later).

---

## Recommendations

### Similar Memes

Embedding similarity from Qdrant. Shown on meme pages for all users. The minimum acceptable implementation uses the source meme's primary file vector and returns similar accessible memes filtered by NSFW/access rules.

When the user has interaction history, the similar-memes block may blend in a small share of personalized recommendations, but the response must keep attribution (`similarity`, `tag_fallback`, `personalized_blend`, etc.) so analytics can distinguish why a meme appeared.

### Personalized Feed

For all users with interaction history (including guests). MVP recommendation service uses Qdrant recommend/search APIs over recent positive interactions:

- strong positives: favorite/like, save, pin, download, Telegram send/chosen inline result
- medium positives: detail view, repeated view, collection add
- weak signals: screen impression and ignored impressions (recorded for later ranking work)

The personalized feed appears on the home page and can be blended into similar/related sections. Collaborative filtering is planned as a later enhancement.

### Trending / Cold Start

Fastest growth in channel reposts + platform engagement. Default feed for users without enough history.

## Result Attribution

Every search/recommendation surface must be able to report why a meme was shown: query, filters, collection scope, rank, score components, source algorithm, source meme (for related sections), and request/impression identifiers. This attribution powers analytics, personalization, and later algorithm tuning.
