# Content Pipeline

## Crawler Architecture

Plugin interface per platform. All crawlers normalize output to a common `RawMeme` dataclass (media bytes, media type, source metadata).

- **Telegram (Telethon):** Long-running userbot sessions that listen to channel updates in real-time via Telethon event handlers. Each channel's `last_read_message_id` is persisted in `SourceChannel`. On startup, the crawler catches up on all messages since `last_read_message_id` per channel, then switches to live listening. Multiple sessions (2–3) distribute channels for rate limit safety (≤30 req/s per session).
- **Reddit (PRAW), VK (VK API):** Planned. Same plugin interface. Platforms without real-time push will poll on intervals, storing `last_read_post_id` in `SourceChannel`.

**Risk:** Telethon userbot accounts are subject to bans. Mitigation: multiple sessions, conservative rates, monitoring. Consider Bot API channel forwarding as a fallback read path.

## Deduplication

Two-phase dedup — fast pHash at ingestion, precise embedding-based merge after the embed stage.

### Phase 1: pHash at Ingestion (synchronous)

Runs during ingestion before the file enters the processing pipeline. Uses perceptual hash only — no embedding needed.

| Condition | Result |
|-----------|--------|
| pHash hamming distance ≤ 3 | Exact match. Add MemeSource to existing MemeFile. File does not enter pipeline. |
| pHash hamming 4–8 | Similar match. Create new MemeFile, link to existing Meme. Update primary if better quality. File enters pipeline. |
| No pHash match | New meme. Create Meme + MemeFile. File enters pipeline. |

### Phase 2: Embedding-Based Merge (post-embed)

After the embed stage computes the file's embedding, a `meme_embedded` consumer queries Qdrant for cosine similarity > 0.92 against existing memes. If a match is found for a meme that was created as "new" in Phase 1 (pHash missed it), the memes are **auto-merged**: files, sources, collection memberships, and popularity data are consolidated into the older meme. The duplicate meme entity is deleted.

This catches near-duplicates that pHash misses (significant crops, overlays, quality differences).

### User Upload Dedup

Incoming upload is checked against the public database (pHash first, then embedding after processing). If a match is found, the existing public meme is added to the user's collection instead of creating a duplicate. Private memes from other users are not cross-linked — a new private meme is created.

### Admin Merge

When admins merge memes manually, all files, collection memberships, pins, and popularity snapshots are moved to the primary meme. Qdrant payloads are updated for moved files, deleted memes are removed from Meilisearch.

## Processing

Event-driven pipeline using **FastStream + RabbitMQ**. Each processing stage is a FastStream subscriber that consumes from one exchange/queue and publishes to the next. Stages are independent processes — they can be scaled, deployed, and restarted separately.

### Event Topology

```
raw_meme ──→ [Ingest & pHash Dedup] ──→ meme_created
                                             │
                                             ▼
                                       [Transcode] ──→ meme_transcoded
                                                            │
                                                            ▼
                                                         [OCR] ──→ meme_ocr_done
                                                                        │
                                                                        ▼
                                                                  [Embed] ──→ meme_embedded
                                                                                    │
                                                                          ┌─────────┴─────────┐
                                                                          ▼                   ▼
                                                                   [Embedding Dedup]   [Classify]
                                                                     (auto-merge        │
                                                                      if match)         ▼
                                                                                    meme_ready
                                                                                        │
                                                                           ┌────────────┼────────────┐
                                                                           ▼            ▼            ▼
                                                                    [Sync Qdrant] [Sync Meili] [SEO Generate]
```

`meme_embedded` fans out to two consumers: Embedding Dedup (checks Qdrant for cosine > 0.92, auto-merges if match found) and Classify. Both bind their own queue via a fanout exchange. Fan-out from `meme_ready` works the same way — each sync consumer binds its own queue, so adding a new consumer requires no changes to the producing stage.

### Queues by Resource Profile

| Queue | Resource Profile | Consumer |
|-------|-----------------|----------|
| `q.transcode` | CPU-bound (FFmpeg) | GIF→MP4, video transcode, blur hash, EXIF strip |
| `q.ocr` | GPU-bound (PaddleOCR, Qwen2.5-VL) | Text extraction, language detection |
| `q.embed` | API-bound (Voyage AI) | Image embedding computation |
| `q.classify` | CPU-light | NSFW detection, language classification from OCR text |
| `q.sync.qdrant` | I/O-bound (Qdrant) | Vector + payload sync |
| `q.sync.meili` | I/O-bound (Meilisearch) | Document sync |
| `q.seo` | API-bound (LiteLLM) | SEO page generation, template assignment (prioritized by popularity) |

Each queue has a configurable prefetch count to control concurrency per worker process.

### Message Schemas

All events are Pydantic models — FastStream validates on both publish and consume. Example:

```python
class MemeCreated(BaseModel):
    meme_id: int
    meme_file_id: int
    s3_original_key: str
    media_type: MediaType
    source: SourceInfo

class MemeTranscoded(BaseModel):
    meme_id: int
    meme_file_id: int
    variants: dict[str, str]  # variant name → S3 key
```

### Retry & Dead Letter

RabbitMQ dead letter exchanges (DLX) handle failures. Messages exceeding max retries (5, with exponential backoff) are routed to a dead letter queue (`dlq.*`) for inspection and manual replay. No message is silently lost.

### Periodic Tasks

Tasks that run on a schedule (not event-driven) are managed by APScheduler in a dedicated process:

- Popularity snapshot computation (every 6h)
- Like count reconciliation (every 5 min)
- Guest account cleanup (daily)
- Trending recomputation (every 5 min)

### OCR Pipeline

PaddleOCR PP-OCRv5 (`lang=cyrillic`) as primary engine. If confidence < 0.6 or empty result, falls back to Qwen2.5-VL-2B (VLM) for stylized/artistic text. Language detected from OCR output by Cyrillic character ratio.

### Embedding Pipeline

Voyage AI `voyage-multimodal-3.5` for both image and text embeddings. 1024 dimensions with Matryoshka support (can reduce to 512 later for cost/speed). Embeddings cached in PG — the cache table is the source of truth, Qdrant is a search index.

## Media Storage

S3 (Cloudflare R2 or Backblaze B2), keyed by MemeFile ID:

```
/files/{meme_file_id}/original.{ext}
/files/{meme_file_id}/web_video.mp4   (H.264, GIF/video only)
```

Only the original and transcoded video are stored. All image variants (resize, format conversion, thumbnails) are generated on-the-fly by **imgproxy** from the original, CDN-cached by URL with immutable headers. Video transcoding (GIF→MP4, re-encode) is still done at ingest time since it's expensive to do on-the-fly.

~40 GB at 100K memes, ~200 GB at 500K, ~400 GB at 1M (originals + transcoded videos only).

## Primary File Selection

When multiple files belong to one meme, the primary file is selected by `quality_score` — a heuristic based on resolution and file size. Higher resolution wins. When primary changes, OCR text is re-evaluated from the new primary.

## SEO Generation

### LLM Integration

All LLM calls go through **LiteLLM**, providing a unified interface to swap models without code changes. The model is expected to change frequently as pricing and quality evolve.

- **Baseline model:** Gemini Flash 2.5 (tested, good quality/cost ratio for this task)
- **Model selection:** configured per environment, not hardcoded
- **Provenance tracking:** every `MemeSeoPage` stores `model_id` (e.g., `gemini/gemini-2.5-flash`) and `prompt_version` (e.g., `seo-v3`) so pages can be filtered by which model/prompt produced them

### Auto-Generation

LLM-based, async, prioritized by popularity score (~2,000 memes/day at ~$60/month budget). Each meme receives: URL slug, page title, meta description, alt text, caption, body text (2–4 paragraphs), tags, template assignment. No output validation — LLM output is stored as-is.

### Admin Editing

Admins can edit SEO pages through:

- **Manual edit:** direct field editing in admin UI
- **AI-assisted edit:** "Edit with AI" — admin selects fields, optionally provides instructions, LLM regenerates selected fields. Admin reviews and confirms before saving.

Bulk regeneration of pages is out of scope. Individual re-generation is a special case of AI-assisted edit (select all fields, no custom instructions).

### URL Strategy

Memes with SEO content → `/meme/{slug}` (indexed). Without → `/meme/{id}` (accessible, not indexed). When SEO is generated, `/meme/{id}` 301-redirects to `/meme/{slug}`.

### Template Assignment

LLM identifies known templates from image during SEO generation. Fuzzy-matched against existing templates — linked or new template created. Admins curate uncurated templates.

## Popularity Scoring

### Static Popularity

A baseline score reflecting a meme's cumulative engagement. Computed every 6h, stored as snapshots for historical charts. Updated scores synced to Qdrant and Meilisearch in batch.

```
popularity = log(1 + source_views) × 0.3
           + log(1 + source_reactions) × 0.2
           + log(1 + source_reposts) × 0.2
           + log(1 + platform_sends) × 0.15
           + log(1 + platform_likes) × 0.1
           + log(1 + platform_saves) × 0.05
```

Logarithmic scaling prevents viral outliers from dominating. Weights are initial values — tuning deferred post-launch alongside search ranking weights.

### Trending Score

Measures recent growth velocity. Computed every 5 min, cached in Redis.

```
trending = (engagement_last_24h - engagement_prev_24h) / (engagement_prev_24h + k)
```

Where `engagement` = weighted sum of sends, saves, likes, views, new source appearances over the window. `k = 10` dampens noise from low-volume memes (a meme going from 1 to 3 interactions shouldn't rank higher than one going from 100 to 200).

### Meme of the Day

Highest `trending` score over a 24h window. Cached in Redis (1h TTL). Recomputed alongside trending.

## Like System

Like = add to Favorites collection. Unlike = remove. `like_count` on Meme is denormalized — incremented/decremented synchronously in PostgreSQL on every like/unlike (single `UPDATE`, negligible cost). The user sees the updated count immediately (read-your-writes). Periodic reconciliation job fixes any drift. Like count changes synced to search indexes in batches (every 5 minutes), not per-like.
