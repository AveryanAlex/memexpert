# Data Model (Draft)

All schemas are draft. Field types, indexes, and constraints will be finalized during implementation.

## Core Concept: Meme vs MemeFile

The data model separates the **conceptual meme** from **media files**. One meme can have multiple files — the same meme reposted with different crops, quality, borders, or compression. This separation is the foundation of the deduplication system.

```
MemeFile 1 (1200x900 JPEG, from @memes_channel)  ──┐
MemeFile 2 (1080x810 PNG, from @funny_pics)        ──┼── Meme "Drake - Monday"
MemeFile 3 (900x675 JPEG with border, from Reddit) ──┘     ↑ primary_file = File 1
```

**On Meme:** media type, OCR text, language, NSFW flag, popularity score, like count, template link, public/private flag.
**On MemeFile:** dimensions, format, file size, S3 URLs for all variants, perceptual hash, quality score, blur hash, processing status.

Embeddings live in a separate cache table, not on MemeFile. This decouples embedding computation from the relational model.

## Entities

### User

Account for both guest (website) and full (Telegram/linked) users. Key fields: `account_type` (guest/full), `status` (active/deletion_pending/deleted), `telegram_id`, `google_id`, `email`, `password_hash`, `active_save_collection_id`, `nsfw_enabled`, `language`.

### Meme

The conceptual meme entity. Key fields: `media_type` (image/gif/video), `primary_file_id` (FK → MemeFile, best quality), `ocr_text`, `language` (ru/en/mixed/none), `is_nsfw`, `popularity_score`, `like_count` (denormalized), `template_id`, `author_user_id` (set if user-uploaded), `is_public`.

### MemeFile

A specific media file belonging to a meme. Key fields: `meme_id` (FK → Meme), `status` (pending/processing/ready/failed), S3 URLs for each variant (original, full, medium, thumb, poster, web_video, tg_photo), `perceptual_hash`, `quality_score`, `blur_hash`, `is_primary`.

### MemeSeoPage

1:1 with Meme. Generated async by LLM. Key fields: `slug` (unique, URL-safe), `page_title`, `meta_description`, `alt_text`, `caption`, `body_text`, `tags[]`, `model_version`.

### MemeTemplate

Template label (V1 — no editor). Key fields: `slug` (unique), `name`, `description`, `is_curated`. V2 adds `base_image_url` and `text_regions` for the meme editor.

### MemePopularitySnapshot

Periodic snapshots for historical charts. Key fields: `meme_id`, `timestamp`, source metrics (views, reactions, source_count), platform metrics (views, sends, saves, likes), `popularity_score`.

### MemeSource

Linked to **MemeFile**, not Meme. Tracks where a specific file was found. Key fields: `file_id` (FK → MemeFile), `platform` (telegram/reddit/vk), `source_id`, `post_id`, `views`, `reactions` (JSON), `is_first_source`, `source_alive`.

Unique constraint: `(platform, source_id, post_id)`.

### SourceChannel

Channels being crawled. Key fields: `platform`, `platform_id`, `username`, `title`, `subscriber_count`, `is_active`, `crawl_frequency`, `last_crawled_at`.

### ChannelSuggestion

User-submitted channel suggestions. Key fields: `user_id`, `platform`, `channel_url`, `status` (pending/approved/rejected), `admin_note`.

### Collection

Every account has auto-created Favorites (not deletable). Full accounts can create additional collections. Key fields: `owner_id`, `title`, `is_favorites`, `invite_link`.

### CollectionMember

Composite PK: `(collection_id, user_id)`. Role: owner/editor/viewer.

### CollectionMeme

References **Meme**, not MemeFile. Composite PK: `(collection_id, meme_id)`. Also tracks `added_by` and `added_at`.

### PinnedMeme

Up to 20 per user. Composite PK: `(user_id, meme_id)`. `position` (1–20).

### TelegramFileIdCache

References **MemeFile**. Key fields: `file_id_tg` (PK), `meme_file_id`, `media_format` (photo/animation), `file_unique_id`.

### EmbeddingCache

PostgreSQL table — source of truth for computed embeddings. Synced to Qdrant for search.

Key fields: `input_hash` (SHA256 of input, unique), `input_type` (image/text), `embedding` (BYTEA, 1024 × float32 = 4096 bytes), `model_version`, `source_id` (FK → MemeFile for images, null for text queries).

Design notes:
- Stored as BYTEA, not a vector type — PG is not used for vector search.
- `model_version` enables future model upgrades: recompute with new model, keep both, switch Qdrant atomically.
- Text query embeddings act as a query cache — intentionally no eviction, as the table size is bounded by unique query volume.

### RefreshToken

Tracks active refresh tokens for JWT auth. Key fields: `user_id`, `token_hash` (SHA256, unique), `device_info`, `expires_at`, `revoked_at`.

### AccountMergeLog

Audit trail for guest → full account merges. Key fields: `guest_account_id`, `target_account_id`, `favorites_transferred`, `views_transferred`.

### DataDestructionLog

152-FZ compliance audit trail. Key fields: `user_id` (not FK — user row is deleted), `action` (deletion_requested/grace_period_expired/hard_deleted/cancelled), `details` (JSON).

### AnalyticsEvent

General-purpose event tracking table. Key fields: `user_id`, `event_type` (search_query/meme_view/meme_send/meme_like/meme_save/etc.), `payload` (JSON — event-specific data), `timestamp`. Schema to be designed during implementation based on analytics requirements.

### InlineUsageEvent

Tracks inline bot usage per chat group for viral coefficient measurement. Key fields: `user_id`, `group_hash` (SHA256 truncated, for privacy), `timestamp`.
