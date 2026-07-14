# Data Model

Field types, indexes, and constraints will be finalized during implementation.

## Core Concept: Meme vs MemeFile

The data model separates the **conceptual meme** from **media files**. One meme can have multiple files — the same meme reposted with different crops, quality, borders, or compression. This separation is the foundation of the deduplication system.

```
MemeFile 1 (1200x900 JPEG, from @memes_channel)  ──┐
MemeFile 2 (1080x810 PNG, from @funny_pics)        ──┼── Meme "Drake - Monday"
MemeFile 3 (900x675 JPEG with border, from Reddit) ──┘     ↑ primary_file = File 1
```

**On Meme:** media type, OCR text, language, NSFW flag, like count, template link, visibility policy, and materialized public/private state.
**On MemeFile:** dimensions, format, file size, S3 URLs for all variants, SHA-256, perceptual hash, quality score, blur hash, processing status.

Embeddings live in a separate cache table, not on MemeFile. This decouples embedding computation from the relational model.

## Entities

### User

Account for both guest (website) and full (Telegram/linked) users. Guest/full is a derived API/domain projection: an account is full when it has at least one linked login identity (`telegram_id`, `google_id`, `email`, or non-blank `password_hash`) and guest otherwise; no `account_type` column is stored. Key fields: `status`, `telegram_id`, `google_id`, `email`, `password_hash`, `active_save_collection_id`, `nsfw_enabled`, `language`, `token_nonce`.

`token_nonce` is the server-side revocation primitive for cookie-backed access/session JWTs. Refresh-token rows are not part of the current auth design.

Deletion-related fields may exist as schema placeholders, but user-initiated account deletion/export is deferred from MVP.

### Meme

The conceptual meme entity. Key fields: `media_type` (image/gif/video), `primary_file_id` (FK → MemeFile, best quality), `ocr_text`, `language` (ru/en/mixed/none), `is_nsfw`, `like_count` (denormalized), `tags` (text array — assigned by LLM during SEO generation, used for filtering and tag pages), `template_id`, `visibility_mode` (`auto`, `force_public`, `force_private`), and materialized effective `is_public`.

There is no singular meme owner or author. Users do not own canonical meme metadata. Upload provenance belongs to `MemeSource`; private read authority comes only from collection access.

### MemeFile

A specific media file belonging to a meme. Key fields: `meme_id` (FK → Meme), `status` (pending/processing/ready/failed), original-upload metadata (`mime_type`, dimensions, byte size), `s3_original_key`, `s3_web_video_key` (nullable — GIF/video playback artifact only), globally unique non-null `sha256_hex`, `perceptual_hash`, `quality_score`, `blur_hash`. The canonical default file is stored on `Meme.primary_file_id`. A non-null `s3_web_video_key` also guarantees a deterministic `preview.png` companion under the file's derivative prefix; its key is derived from `MemeFile.id` rather than duplicated in PostgreSQL. Static image variants (resize, format) are served on-the-fly by imgproxy from the original, while moving-media thumbnail/display variants use that stored preview frame. Derived artifacts never overwrite original MIME metadata.

### MemeSeoPage

1:1 with Meme. Generated async by a PydanticAI-backed provider. Key fields: `slug` (unique, URL-safe), `page_title`, `meta_description`, `alt_text`, `caption`, `body_text`, `tags[]`, `model_id`, `prompt_version`, `generated_at`, `edited_at` (nullable — set when admin edits).

### MemeTemplate

Template label (V1 — no editor, but schema is V2-ready). Key fields: `slug` (unique), `name`, `description`, `is_curated`, `base_image_url` (nullable — unused in V1), `text_regions` (nullable JSON array — unused in V1, each region: `{x, y, width, height, default_font_size, alignment}`). V2 populates these fields to power the meme editor — no schema migration needed.

### MemeSource

Linked to **MemeFile**, not Meme. Tracks where a specific file was found. Key fields: `file_id` (FK → MemeFile), `platform` (telegram/reddit/vk), `source_id`, `post_id`, `source_kind` (`user_upload`, `public_crawler`, `operator_upload`), nullable `uploader_user_id`, `published_at`, `is_first_source`, `source_alive`, and source-engagement scheduling state (`last_engagement_check_at`, `next_engagement_check_at`, lease owner/time, attempt count, last error). Stable provenance stays here; volatile Telegram counters do not.

Unique constraint: `(platform, source_id, post_id)`.

### Visibility and provenance invariants

- New user and operator uploads start private; public crawler discoveries start public.
- In `auto` mode, the existence of any historical `public_crawler` source makes the meme public. Marking that source dead does not demote the meme.
- `force_private` suppresses crawler promotion; `force_public` stays public regardless of provenance.
- Exact SHA reuse may attach multiple uploaders to the same canonical meme. Each uploader gets access through their own collection membership; uploader/source metadata is internal and absent from non-admin DTOs.
- Approximate deduplication may merge public with public, or private with private only when both memes have the same single uploader. It never crosses public/private or uploader boundaries.

### MemeSourceEngagementSnapshot

Append-only source metric observations for a `MemeSource`. Key fields: `meme_source_id`, `captured_at`, `scheduled_for`, `capture_reason`, `schedule_label`, `view_count`, `reactions`, `reaction_count`, `comment_count`, `forward_count`, `comments_state`, `fetch_status`, `source_alive`, `error_code`, and `raw_metrics`.

`NULL` counters are meaningful: they mean the source did not expose the counter. A known zero remains `0`. Public ranking/read models may coalesce unknown to zero for presentation, but canonical snapshots preserve the distinction.

Initial ingestion writes an `ingest_initial` baseline snapshot. Historical source deltas are computed with `lag()` per `meme_source_id`; the first snapshot contributes no invented delta. Follow-up schedule slots are anchored to the Telegram post date (`+1h`, `+3h`, `+12h`, `+1d`, `+3d`, `+7d`, `+1month`, then monthly), not to ingest time.

`forward_count` is Telegram's public forward/repost count and feeds derived public `latest_source_reposts`. It is unrelated to forwarded-message provenance such as `forwarded_from_*`.

### Public Trend Read Models

`public_meme_trends_mv`, `public_tag_trends_mv`, `public_template_trends_mv`, `public_tag_trend_points_mv`, and `public_template_trend_points_mv` are derived-cached read models. They are rebuildable from `meme_source_engagement_snapshots`, `analytics_events`, and current meme/template metadata. Current source totals use the latest successful snapshot per source post; historical/window metrics use snapshot-to-snapshot deltas; internal platform metrics come from `analytics_events`.

The public DTO names remain stable (`latest_source_views`, `latest_source_reactions`, `latest_source_reposts`, `latest_popularity_score`), but these values are derived read-model metrics. There is no canonical `memes.popularity_score` column and no `meme_popularity_snapshots` table.

### TelegramSession

Canonical registry for successfully promoted Telethon userbot sessions. Key fields: `name`, `display_name`, encrypted `encrypted_string_session`, account projection fields (`account_user_id`, `account_username`, `account_phone_hint`), `status`, `enabled`, per-session feature flags, and `max_requests_per_second`. Non-null `account_user_id` values are unique, so one Telegram account cannot be registered as multiple crawler sessions. API read schemas deliberately omit `encrypted_string_session`; runtime code decrypts it only when constructing `TelegramClient(StringSession(...))`.

### TelegramSessionLoginAttempt

Short-lived, standalone state for browser-admin QR and phone login. A new-account attempt exists without a `TelegramSession`; nullable `telegram_session_id` either targets an existing session for re-authentication or points to the session assigned after successful promotion. `created_by_admin_user_id` records the initiating operator without preventing user deletion. Temporary encrypted `StringSession`, phone-code, and QR fields are never exposed through API schemas.

Terminal attempt status includes `completed`, `failed`, `expired`, and `cancelled`. Cleanup is tracked independently through `cleanup_status` (`pending`, `promoted`, `discarded`, or `failed`), retry count, error details, and completion timestamp. This permits cancellation and TTL cleanup to revoke abandoned Telegram auth keys while retaining enough encrypted state for retry; successful promotion clears temporary secrets without logging out the canonical crawler credential.

### SourceChannel

Channels being crawled. Key fields: `platform`, `platform_id`, `username`, `title`, `subscriber_count`, `is_active`, `last_read_post_id` (platform-specific, e.g. Telegram message ID — used to resume on restart), `telegram_session_id` (nullable FK to the `telegram_sessions` row that handles this channel), and live/catch-up/engagement flags. If a Telegram session is deleted, `ON DELETE SET NULL` leaves the source channel as an orphan; it remains visible for operator repair but is not runnable until reassigned.

### ChannelSuggestion

User-submitted channel suggestions. Key fields: `user_id`, `platform`, `channel_url`, `status` (pending/approved/rejected), `admin_note`.

### Collection

Every account gets a Favorites collection lazily on first collection interaction; it is not deletable. Full accounts can create additional collections. Key fields: `owner_id`, `title`, `kind`, and `visibility`. `CollectionMeme` plus collection ownership/membership is the only non-admin authority for private meme access.

### Exact-SHA reconciliation and migration

The provenance rollout is staged. Revision `0031` adds source-kind, uploader, and visibility-mode fields while legacy ownership columns remain and backfills private collection access. Before revision `0032`, `memexpert-reconcile-sha-duplicates` merges one duplicate SHA group per transaction, records obsolete meme/file and S3/Qdrant/Meilisearch identifiers in `MemeMergeLog.details`, transfers dependent rows, and recomputes likes from distinct Favorites owners. Revision `0032` refuses to run while duplicate non-null hashes remain, restores the unique partial SHA index, and removes legacy singular ownership columns.

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
