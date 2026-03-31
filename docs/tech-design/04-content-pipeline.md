# Content Pipeline

## Crawler Architecture

Plugin interface per platform. All crawlers normalize output to a common `RawMeme` dataclass (media bytes, media type, source metadata).

- **Telegram (Telethon):** Userbot sessions, ~100 channels, ≤30 req/s, 2–3 sessions, exponential backoff. TaskIQ scheduled tasks per channel based on `crawl_frequency`.
- **Reddit (PRAW), VK (VK API):** Planned. Same plugin interface.

**Risk:** Telethon userbot accounts are subject to bans. Mitigation: multiple sessions, conservative rates, monitoring. Consider Bot API channel forwarding as a fallback read path.

## Deduplication

Operates at the **file level**. Three stages:

| Stage | Condition | Result |
|-------|-----------|--------|
| 1. Exact match | pHash hamming distance ≤ 3 | Same file (repost). Add MemeSource to existing MemeFile. |
| 2. Similar match | pHash hamming 4–8 OR Qdrant cosine > 0.92 | Same meme, different file. Create new MemeFile, link to existing Meme. Update primary if better quality. |
| 3. No match | Below all thresholds | New meme. Create Meme + MemeFile. |

Stage 2b uses Qdrant for embedding similarity (fast ANN over the existing index).

**User upload dedup:** Incoming upload is checked against the public database. If a match is found, the existing public meme is added to the user's collection instead of creating a duplicate. Private memes from other users are not cross-linked — a new private meme is created.

**Admin merge:** When admins merge memes, all files, collection memberships, pins, and popularity snapshots are moved to the primary meme. Qdrant payloads are updated for moved files, deleted memes are removed from Meilisearch.

## Processing

TaskIQ workers with **separate queues** by resource profile to prevent contention:

| Queue | Workers | Resource Profile | Tasks |
|-------|---------|-----------------|-------|
| `transcode` | CPU-bound | Pillow, FFmpeg | Image resize, GIF→MP4, video transcode, blur hash, EXIF strip |
| `ocr` | GPU-bound | PaddleOCR, Qwen2.5-VL | Text extraction, language detection |
| `embed` | API-bound | Voyage AI | Image embedding computation |
| `llm` | API-bound | LLM provider | SEO page generation, template assignment |
| `sync` | I/O-bound | Qdrant, Meilisearch | Index sync, payload updates |
| `scheduled` | Mixed | Various | Popularity snapshots, like count reconciliation, guest cleanup |

Processing flow for a new meme:
1. **Transcode** → generate all media variants (full, medium, thumb, poster, web_video, tg_photo)
2. **OCR** → extract text (PaddleOCR primary, Qwen2.5-VL fallback for low confidence or stylized text)
3. **Embedding** → compute via Voyage AI, store in embedding cache
4. **Classification** → NSFW detection, language detection from OCR text
5. **MemeFile status → `ready`**
6. **Sync** → push to Qdrant and Meilisearch
7. **SEO generation** (async, prioritized by popularity) → generate slug, title, description, tags, template assignment

### OCR Pipeline

PaddleOCR PP-OCRv5 (`lang=cyrillic`) as primary engine. If confidence < 0.6 or empty result, falls back to Qwen2.5-VL-2B (VLM) for stylized/artistic text. Language detected from OCR output by Cyrillic character ratio.

### Embedding Pipeline

Voyage AI `voyage-multimodal-3.5` for both image and text embeddings. 1024 dimensions with Matryoshka support (can reduce to 512 later for cost/speed). Embeddings cached in PG — the cache table is the source of truth, Qdrant is a search index.

## Media Storage

S3 (Cloudflare R2 or Backblaze B2), keyed by MemeFile ID:

```
/files/{meme_file_id}/original.{ext}
/files/{meme_file_id}/full.webp       (1280px)
/files/{meme_file_id}/medium.webp     (600px)
/files/{meme_file_id}/thumb.webp      (200px)
/files/{meme_file_id}/poster.jpg      (first frame, GIF/video)
/files/{meme_file_id}/web_video.mp4   (H.264, GIF/video)
/files/{meme_file_id}/tg_photo.jpg    (JPEG for Telegram)
```

CDN-served with immutable cache headers (content-addressed by file ID). imgproxy handles on-the-fly transforms beyond the pre-generated variants.

~205 GB at 100K memes, ~1 TB at 500K, ~2 TB at 1M (all variants).

## Primary File Selection

When multiple files belong to one meme, the primary file is selected by `quality_score` — a heuristic based on resolution and file size. Higher resolution wins. When primary changes, OCR text is re-evaluated from the new primary.

## SEO Generation

LLM-based, async, prioritized by popularity score (~2,000 memes/day at ~$60/month budget). Each meme receives: URL slug, page title, meta description, alt text, caption, body text (2–4 paragraphs), tags, template assignment.

URL strategy: memes with SEO content → `/meme/{slug}` (indexed). Without → `/meme/{id}` (accessible, not indexed). When SEO is generated, `/meme/{id}` 301-redirects to `/meme/{slug}`.

Template assignment: LLM identifies known templates from image. Fuzzy-matched against existing templates — linked or new template created. Admins curate uncurated templates.

## Popularity Scoring

Composite score from source channel metrics (views, reactions, repost count) and platform metrics (sends, saves, likes, views). Logarithmic scaling. Computed periodically (every 6h), stored as snapshots for historical charts. Updated scores synced to Qdrant and Meilisearch in batch.

"Meme of the Day" — highest popularity growth over 24 hours. Cached in Redis (1h TTL).

## Like System

Like = add to Favorites collection. Unlike = remove. `like_count` on Meme is denormalized. Periodic reconciliation job fixes any drift. Like count changes synced to indexes in batches (every 5 minutes), not per-like.
