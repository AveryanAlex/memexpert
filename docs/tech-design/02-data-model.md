# Data Model

Field types, indexes, and constraints will be finalized during implementation.

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

Account for both guest (website) and full (Telegram/linked) users. Guest/full is a derived API/domain projection: an account is full when it has at least one linked login identity (`telegram_id`, `google_id`, `email`, or non-blank `password_hash`) and guest otherwise; no `account_type` column is stored. Key fields: `status`, `telegram_id`, `google_id`, `email`, `password_hash`, `active_save_collection_id`, `nsfw_enabled`, `language`, `token_nonce`.

`token_nonce` is the server-side revocation primitive for cookie-backed access/session JWTs. Refresh-token rows are not part of the current auth design.

Deletion-related fields may exist as schema placeholders, but user-initiated account deletion/export is deferred from MVP.

### Meme

The conceptual meme entity. Key fields: `media_type` (image/gif/video), `primary_file_id` (FK → MemeFile, best quality), `ocr_text`, `language` (ru/en/mixed/none), `is_nsfw`, `popularity_score`, `like_count` (denormalized), `tags` (text array — assigned by LLM during SEO generation, used for filtering and tag pages), `template_id`, `author_user_id` (set if user-uploaded), `is_public`.

### MemeFile

A specific media file belonging to a meme. Key fields: `meme_id` (FK → Meme), `status` (pending/processing/ready/failed), `s3_original_key`, `s3_web_video_key` (nullable — GIF/video only), `perceptual_hash`, `quality_score`, `blur_hash`, `is_primary`. Image variants (resize, format) served on-the-fly by imgproxy from the original.

### MemeSeoPage

1:1 with Meme. Generated async by a PydanticAI-backed provider. Key fields: `slug` (unique, URL-safe), `page_title`, `meta_description`, `alt_text`, `caption`, `body_text`, `tags[]`, `model_id`, `prompt_version`, `generated_at`, `edited_at` (nullable — set when admin edits).

### MemeTemplate

Template label (V1 — no editor, but schema is V2-ready). Key fields: `slug` (unique), `name`, `description`, `is_curated`, `base_image_url` (nullable — unused in V1), `text_regions` (nullable JSON array — unused in V1, each region: `{x, y, width, height, default_font_size, alignment}`). V2 populates these fields to power the meme editor — no schema migration needed.

### MemePopularitySnapshot

Periodic snapshots for historical charts and materialized-view refreshes. Key fields: `meme_id`, `timestamp`, source metrics (views, reactions, source_count), platform metrics (impressions, views, sends, saves, downloads, likes), `popularity_score`.

### MemeSource

Linked to **MemeFile**, not Meme. Tracks where a specific file was found. Key fields: `file_id` (FK → MemeFile), `platform` (telegram/reddit/vk), `source_id`, `post_id`, `views`, `reactions` (JSON), `is_first_source`, `source_alive`.

Unique constraint: `(platform, source_id, post_id)`.

### SourceChannel

Channels being crawled. Key fields: `platform`, `platform_id`, `username`, `title`, `subscriber_count`, `is_active`, `last_read_post_id` (platform-specific, e.g. Telegram message ID — used to resume on restart), `session_id` (which crawler session handles this channel).

### ChannelSuggestion

User-submitted channel suggestions. Key fields: `user_id`, `platform`, `channel_url`, `status` (pending/approved/rejected), `admin_note`.

### Collection

Every account has auto-created Favorites (not deletable). Full accounts can create additional private collections. Key fields: `owner_id`, `title`, `is_favorites`, visibility/internal flags.

### CollectionMember

Composite PK: `(collection_id, user_id)`. Role: owner/editor/viewer. Search and browsing access are derived from this table.

### CollectionInvite

Reusable invite record for a collection. Key fields: `collection_id`, `token_hash` or opaque code, `role`, `created_by_user_id`, `expires_at`, `revoked_at`, `max_uses`, `uses_count`. Invites are consumed by web, bot PM, and Mini App deep links.

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

### AccountMergeLog

Audit trail for guest → full account merges. Key fields: `guest_account_id`, `target_account_id`, `favorites_transferred`, `views_transferred`, `interaction_events_transferred`, `inline_usage_events_transferred`.

### AccountDeletionLog

Deferred/reserved. If present, it is a schema placeholder for a future account deletion flow; MVP does not require user-facing deletion/export behavior or scheduled hard delete jobs.

### AnalyticsEvent

General-purpose product analytics event stream. Key fields: `user_id`, `event_type`, `payload` (JSON — event-specific data), `occurred_at`.

### MemeInteractionEvent

Append-only, recommendation-oriented interaction stream. Implemented as a strict, versioned payload schema on `AnalyticsEvent` so MemeExpert keeps one durable `analytics_events` stream instead of splitting interaction writes into a second table.

Key fields:

- `user_id` (nullable only for truly anonymous telemetry; normal web guests have users)
- `meme_id`
- `event_type` keeps legacy values (`impression`, `view`, `click`, `favorite`, `save`, `share`, `meme_view`, `meme_send`, `meme_like`, `meme_save`, `search_query`, `inline_query`) and adds canonical meme-scoped/foundation values (`meme_impression`, `meme_detail_click`, `meme_download`, `meme_pin`, `meme_share`, `meme_report`, `inline_served`, `inline_chosen`, `inline_sent`, `collection_action`, `auth_event`, `account_merge`, `miniapp_open`, `channel_suggest`)
- `surface` (`web_home`, `web_search`, `web_related`, `web_collection`, `telegram_inline`, `telegram_pm`, `miniapp`)
- `source_algorithm` (`search`, `similarity`, `personalized`, `tag_related`, `trending`, `motd`, `collection`, `fallback`)
- `source_meme_id` for related/similar flows
- `collection_id`, `query`, `rank`, `score`, `score_components`, `reason`
- `request_id` / `impression_id`
- Payload envelope always stores `schema_version`, `actor_type`, `actor_account_type` (when a user exists), typed internal UUID refs under `refs`, and a JSON-safe `properties` bag for extra attribution.
- Compatibility reads still accept legacy flat `payload.meme_id`, but new strict writes store meme attribution under `payload.refs.meme_id`.
- Raw `group_id`, `chat_id`, tokens, authorization/cookie headers, IP addresses, user agents, and request headers are forbidden. External/chat identifiers must be hashed before storage.
- `occurred_at`

Indexes: `(user_id, occurred_at)`, `(meme_id, occurred_at)`, `(event_type, occurred_at)`, and optionally `(request_id)` / `(impression_id)` for attribution joins.

### InlineUsageEvent

Tracks inline bot usage per chat group for viral coefficient measurement. Key fields: `user_id`, `group_hash` (SHA256 truncated, for privacy), `timestamp`.
