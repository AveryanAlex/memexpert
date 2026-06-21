# Content Pipeline

## Crawler Architecture

Plugin interface per platform. All crawlers normalize output to a common `RawMeme` dataclass (media bytes, media type, source metadata).

- **Telegram (Telethon):** Long-running userbot sessions that listen to channel updates in real-time via Telethon event handlers. Each channel's `last_read_message_id` is persisted in `SourceChannel`. On startup, the crawler catches up on all messages since `last_read_message_id` per channel, then switches to live listening. Multiple sessions (2–3) distribute channels for rate limit safety (≤30 req/s per session).
- **Reddit (PRAW), VK (VK API):** Planned. Same plugin interface. Platforms without real-time push will poll on intervals, storing `last_read_post_id` in `SourceChannel`.

**Risk:** Telethon userbot accounts are subject to bans. Mitigation: multiple sessions, conservative rates, monitoring. Consider Bot API channel forwarding as a fallback read path.

## Content Identity And Deduplication

The content model has three separate owners of truth:

- `PipelineIngestRequest` is the pre-content raw-ingest source of truth. API-safe entrypoints create this row after stdlib SHA256/idempotency checks and temporary original-object upload. Heavy workers later inspect media and either materialize `Meme`/`MemeFile` rows or mark a terminal failure.
- `Meme` is the conceptual meme. It owns public/private moderation state, popularity, collections, SEO page linkage, and the canonical `primary_file_id` pointer.
- `MemeFile` is one physical media file. `sha256_hex` is the only exact same-bytes identity and is unique. File rows own physical metadata, S3 keys, pHash, ingest origin, and optional match lineage.
- `MemeSource` is one provenance observation. Source rows preserve where a file or duplicate was observed and carry the attach reason plus any matched file id.

### Ingest-Time Identity

SHA256 is computed immediately after bytes are available, before media inspection, blocked pHash checks, canonical S3 writes, or pHash duplicate lookup.

| Condition | Result |
|-----------|--------|
| Source identity `(platform, source_id, post_id)` already has a raw ingest request | Return the existing `PipelineIngestRequest`; do not upload or enqueue again. |
| SHA256 matches an existing non-blocked file | Create/update the ingest request as `resolved_sha_duplicate`, do not inspect media or enqueue media-inspect. Attach a `MemeSource` to the existing file with `attach_reason=sha256_exact_existing_file`. |
| SHA256 matches an existing blocked/quarantined file | Create/update the ingest request as `resolved_sha_duplicate`, do not inspect media or enqueue media-inspect. Attach a `MemeSource` to the existing blocked file with `attach_reason=blocked_sha256_existing_file`. |
| SHA miss | Store bytes under the temporary-original prefix, create a `PipelineIngestRequest` with `status=media_inspect_pending`, and write a `rabbitmq_outbox_messages` row in the same DB transaction for a media-inspect worker event carrying `ingest_request_id`. |
| Worker cannot inspect/read media | Mark the ingest request `failed_invalid_media`, record failure code/detail, create no `Meme`/`MemeFile`, and retain the temporary object for operator retention/debugging. No downstream event is written. |
| Worker finds active blocked pHash | Promote the original to the canonical key, create hidden failed `Meme`/`MemeFile`/`MemeSource` audit rows, mark the ingest request `failed_blocked_phash`, clean the temporary object, and write no normal transcode event. |
| Worker finds exact pHash match | Treat this as the same conceptual meme but a new physical file. Promote the original to the canonical key, create a new `MemeFile` under the matched file's `Meme`, set `ingest_origin=phash_exact_existing_meme` and `matched_meme_file_id`, attach source with `attach_reason=phash_exact_new_file`, mark the request `materialized`, and write a downstream transcode outbox event transactionally. |
| Worker finds no pHash match | Promote the original to the canonical key, create a new `Meme` plus primary `MemeFile` with `ingest_origin=new_meme`, attach source with `attach_reason=new_file`, mark the request `materialized`, and write a downstream transcode outbox event transactionally. |

Crawler duplicate-post idempotency is separate from media identity: an already-seen `(platform, source_id, post_id)` returns the existing source row before service-owned media processing. Telegram `file_unique_id` is not a content identity and there is no separate unique media identity table.

### Post-Embed Semantic Merge

After the embed stage computes a file embedding, semantic merge may query Qdrant for high cosine similarity and merge two `Meme` concepts. This is not exact-byte identity and not the exact-pHash same-meme ingest path. When semantic merge fires, files, sources, collection memberships, pins, and like counts are consolidated into the target meme and the duplicate meme entity is deleted according to the merge service's invariants. Public popularity is derived later from the moved source rows and analytics events.

### Admin Merge

When admins merge memes manually, all files, sources, collection memberships, pins, and like counts are moved to the primary meme. Qdrant payloads are updated for moved files, deleted memes are removed from Meilisearch, and public popularity is recomputed from the moved source rows and analytics events.

## Processing

Event-driven pipeline using **FastStream + RabbitMQ**. Each processing stage is a FastStream subscriber that consumes from one exchange/queue and publishes to the next. Stages are independent processes — they can be scaled, deployed, and restarted separately.

### Event Topology

```
raw_meme ──→ [API Accept: ingest_request + outbox] ──→ [Outbox Publisher] ──→ media_inspect_requested
                                                                   │
                                                                   ▼
                                                          [Media Inspect/Materialize]
                                                                   │
                                                                   ▼
                                                          [Outbox Publisher]
                                                                   │
                                                                   ▼
                                                              meme_created
                                             │
                                             ▼
                                  [Media Preparation] ──→ meme_transcoded
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
| `pipeline.media_inspect` | CPU-light to CPU-bound (Pillow/ImageHash/ffprobe) | Raw temp-object inspection, pHash duplicate/blocked checks, content materialization |
| `q.transcode` | CPU-light to CPU-bound | Media preparation: blur hash/quality for every file; GIF→MP4 and video re-encode only for moving media |
| `q.ocr` | CPU/GPU-bound (PaddleOCR) | Text extraction, language detection |
| `q.embed` | API-bound (Voyage AI) | Image embedding computation |
| `q.classify` | CPU-light | Conservative NSFW detection only |
| `q.sync.qdrant` | I/O-bound (Qdrant) | Vector + payload sync |
| `q.sync.meili` | I/O-bound (Meilisearch) | Document sync |
| `q.seo` | API-bound (PydanticAI provider) | SEO page generation, tag/template assignment (prioritized by popularity) |

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

RabbitMQ dead letter exchanges (DLX) handle worker-consume failures. Messages exceeding max retries (5, with exponential backoff) are routed to a dead letter queue (`dlq.*`) for inspection and manual replay. No message is silently lost.

Pipeline entrypoints, the media materializer, and stage transition services use a generic RabbitMQ transactional outbox instead of commit-then-publish. `rabbitmq_outbox_messages` rows are written in the same DB transaction as ingest/materialization/stage state, then the `rabbitmq-outbox-publisher` job in `memexpert-scheduler` starts or reuses the RabbitMQ pipeline broker, recovers stale `publishing` leases, claims due `pending`/`failed` rows with row locks, publishes by stored `exchange`, `routing_key`, JSON payload, headers, and stable `message_id`, and marks rows `published` or `failed` with retry metadata. This path handles raw-upload `media_inspect_requested` events, post-materialization transcode dispatches, stage fan-out, replay, and sync-success notifications.

### Periodic Tasks

Tasks that run on a schedule (not event-driven) are managed by APScheduler in a dedicated process:

- Public trend materialized-view refresh (e.g. every 5 min or adaptive)
- Source engagement capture enqueueing for due Telegram source posts
- Search-index sync (batched, not per-like)
- Generic RabbitMQ transactional outbox publishing for ingest/materialization/stage events
- Meme of the Day selection/cache refresh
- Scheduled SEO generation batches prioritized by backlog class and stable tie-breakers

The current implementation registers these scheduler jobs with independent enable and interval settings. Source engagement capture, public trend materialized-view refresh, Meme of the Day cache refresh, search-index sync, SEO backlog batches, and RabbitMQ outbox publishing contain production behavior.

Source engagement scheduling is stored in PostgreSQL on `meme_sources` and anchored to the Telegram post date. The scheduler only claims due rows and writes `source_engagement_capture_requested` messages through the transactional outbox; worker-side RabbitMQ consumers perform the Telegram fetch and append `meme_source_engagement_snapshots`. Public trends/search popularity are derived from those snapshots plus `analytics_events`, so no pipeline stage writes canonical popularity counters back to `memes` or `meme_sources`.

Guest TTL/deletion jobs are intentionally not part of the current product direction.

### OCR Pipeline

PaddleOCR is the live OCR engine for Russian/English meme text. The worker image keeps the main app on Python 3.14, but runs PaddleOCR from a separate Python 3.13 helper venv because PaddlePaddle does not currently publish CPython 3.14 wheels. The helper runs `PaddleOCR(lang="ru", use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False)` and returns JSON across the `PIPELINE_OCR_PADDLE_COMMAND` boundary. `PIPELINE_OCR_PROVIDER_MODE=fake` remains the deterministic CI/E2E path. There is no active Qwen/VLM fallback in this implementation; optional fallback metadata/commands are blank unless a real command is configured.

### Embedding Pipeline

Voyage AI `voyage-multimodal-3.5` for both image and text embeddings. 1024 dimensions with Matryoshka support (can reduce to 512 later for cost/speed). Embeddings cached in PG — the cache table is the source of truth, Qdrant is a search index.

## Media Storage

S3 (Cloudflare R2 or Backblaze B2). API-safe acceptance first writes raw bytes under the temporary-original prefix, keyed by `PipelineIngestRequest.id`. Worker materialization later copies valid originals into canonical keys keyed by `MemeFile.id` and then deletes the temp object after successful normal or blocked materialization:

```
/temp-originals/{ingest_request_id}/original.{ext}
/files/{meme_file_id}/original.{ext}
/files/{meme_file_id}/web_video.mp4   (H.264, GIF/video only)
```

Invalid/unreadable media is the exception: the temporary object is intentionally retained when the request is marked `failed_invalid_media` so operators have a bounded retention/debugging target. Retention/lifecycle policy should expire `pipeline/temp-originals/*` objects after the ops-approved window, not immediately on invalid media.

Only the original and optional `web_video.mp4` playback artifact are stored. Static JPEG/PNG/WebP uploads keep only the original object; they are never looped into synthetic videos. All static image variants (resize, format conversion, thumbnails) are generated on-the-fly by **imgproxy** from the original, CDN-cached by URL with immutable headers. GIF/video playback artifacts are derived at ingest time because GIF→MP4 and video re-encode work is expensive to do on-the-fly.

~40 GB at 100K memes, ~200 GB at 500K, ~400 GB at 1M (originals + transcoded videos only).

## Primary File Selection

`Meme.primary_file_id` is the single canonical primary truth. OCR text and language on `Meme` are derived from the current primary file's OCR result when classify completes or when merge/primary-selection code explicitly changes the primary. A duplicate or pHash-exact file finishing later must not overwrite canonical OCR/language unless it is the current primary.

Classification owns only NSFW. NSFW updates are conservative: `Meme.is_nsfw` can move from false to true, but classify must not overwrite an existing true value with false. SEO text, tags, and template assignment are outside classify and ingest.

## SEO Generation

### PydanticAI Integration

SEO generation goes through a typed provider boundary built on **PydanticAI**. The baseline prompt set should be ported from the project's v0 Rust branch, then tuned iteratively after enough generated pages can be reviewed.

- **Baseline provider:** configured per environment through PydanticAI-compatible model settings.
- **Prompt baseline:** v0 Rust prompts are the starting point; prompt tuning is explicitly expected later.
- **Structured output:** provider returns a Pydantic model with title, meta description, alt text, caption, body text, tags, template fields.
- **Provenance tracking:** every `MemeSeoPage` stores `model_id` and `prompt_version` so pages can be filtered by which model/prompt produced them.
- **Current runtime inputs:** live SEO generation passes OCR text, existing tags, language, current template metadata, safe media metadata, and eligible primary image bytes. Static/local generation and skipped image cases remain text-only, so providers must stay grounded in the attached image or explicit metadata and avoid inventing unsupported details.
- **Current provenance caveat:** the existing schema stores `model_id`, `prompt_version`, `generated_at`, and `edited_at`, but not full raw prompt/response captures. This POC preserves that schema instead of adding a migration.

### Auto-Generation

Async, prioritized by popularity/backlog. Each meme receives: URL slug, page title, meta description, alt text, caption, body text (2–4 paragraphs), tags, template assignment. Output is validated structurally before persistence; quality still depends on prompt tuning and later manual/admin review.

### Admin Editing

Admins can edit SEO pages through:

- **Manual edit:** direct field editing in admin UI
- **AI-assisted edit:** "Edit with AI" — admin selects fields, optionally provides instructions, LLM regenerates selected fields. Admin reviews and confirms before saving.

Bulk regeneration of pages is out of scope for the first SEO POC. Individual re-generation is a special case of AI-assisted edit (select all fields, no custom instructions).

### URL Strategy

Memes with SEO content → `/memes/{slug}` (indexed). Without → `/memes/{id}` (accessible, not indexed). When SEO is generated, `/memes/{id}` 301-redirects to `/memes/{slug}`.

### Template Assignment

LLM identifies known templates from image during SEO generation. Fuzzy-matched against existing templates — linked or new template created. Admins curate uncurated templates.

## Popularity Read Models

### Derived Popularity

Public `popularity_score` fields are derived read-model/index payload values, not canonical meme columns. They are recomputed from the latest successful source engagement snapshots plus platform interaction events and synced to Qdrant/Meilisearch in batch.

```text
popularity = log(1 + source_views) × source_view_weight
           + log(1 + source_reactions) × source_reaction_weight
           + log(1 + source_reposts) × source_repost_weight
           + log(1 + platform_sends) × send_weight
           + log(1 + platform_likes) × like_weight
           + log(1 + platform_saves) × save_weight
           + log(1 + platform_downloads) × download_weight
           + log(1 + platform_views) × view_weight
```

Logarithmic scaling prevents viral outliers from dominating. Source totals come from the latest successful `meme_source_engagement_snapshots` row per source post. Historical charts and velocity windows use snapshot-to-snapshot deltas; the first source snapshot is only a baseline and contributes no invented delta. Platform metrics come from `analytics_events`.

Snapshot `NULL` means Telegram did not expose a counter and is preserved in canonical storage. Public read models may coalesce unknown to `0` for ranking/output. Telegram `forward_count` is the public forward/repost counter that feeds derived source repost metrics; forwarded-message provenance (`forwarded_from_*`) is attribution, not engagement.

### Trending Score

Measures recent growth velocity. Prefer materialized views for rankings and aggregate trend surfaces when they simplify reads.

```text
trending = (engagement_last_24h - engagement_prev_24h) / (engagement_prev_24h + k)
```

Where `engagement` = weighted sum of sends, saves, downloads, likes, views, impressions, and new source appearances over the window. `k` dampens noise from low-volume memes.

### Meme of the Day

Selected on a schedule from recent high-quality, non-NSFW candidates using trending/popularity growth and novelty. Store/cache the selected meme with provenance (`selected_at`, score inputs, algorithm_version). Recompute at least daily; refresh cache more often if the selection window changes.

## Like System

Like = add to Favorites collection. Unlike = remove. `like_count` on Meme is denormalized — incremented/decremented synchronously in PostgreSQL on every like/unlike (single `UPDATE`, negligible cost). The user sees the updated count immediately (read-your-writes). Periodic reconciliation job fixes any drift. Like count changes synced to search indexes in batches, not per-like.
