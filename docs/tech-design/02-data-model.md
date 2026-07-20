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
**On MemeFile:** original dimensions/format/size, active derivative pointer,
source/output audio state, derivative profile/verification time, SHA-256,
perceptual hash, quality score, blur hash, and processing status.

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

A specific media file belonging to a meme. Key fields: `meme_id` (FK →
Meme), `status` (pending/processing/ready/failed), original-upload metadata
(`mime_type`, dimensions, byte size), `s3_original_key`, active nullable
`s3_web_video_key`, `source_has_audio`, `web_video_has_audio`,
`web_video_profile`, `web_video_verified_at`, globally unique non-null
`sha256_hex`, `perceptual_hash`, `quality_score`, and `blur_hash`. The canonical
default file is stored on `Meme.primary_file_id`.

An active generation key has a sibling poster derived from the key itself:
`.../generations/{generation_id}/web.mp4` maps to sibling `preview.png`.
Legacy `{file_id}/web.mp4`, `{file_id}/web_video.mp4`, and
`{file_id}/preview.png` remain readable during migration. Static image variants
are served on-the-fly by imgproxy; moving-media thumbnails use the active stored
poster. Derived artifacts never overwrite original MIME metadata or dimensions.

### MediaGeneration

A durable immutable attempt to produce one moving-media web generation. It
links the file and optional recovery item and stores expected/replacement video
and poster keys, selected profile, retry limit, source/output media observations
(dimensions, frame rate, duration, bitrate, size, video/audio codecs and audio
presence), status, attempts, safe failure, activation/supersession timestamps,
and cleanup state. Generation object keys are unique. Indexes cover file/status,
recovery linkage, cleanup eligibility, and old superseded generations.

Both artifacts are generated and verified locally, uploaded to immutable keys,
and only then activated by one fenced database update. A failed/stale generation
cannot change `MemeFile.status` or the active pointer. Cleanup may delete only a
recognized, old, unreferenced generation and never an active, young, unknown, or
otherwise referenced object.

### RecoveryJob and RecoveryJobItem

`RecoveryJob` stores immutable requester and idempotency request ID, optional
current assignee, action, replay scope, retry limit, explicit/query selector and
selection snapshot, `preparing` materialization cursor/lease/generation,
selected-root and expanded-step counts, grouped exclusions, detailed status
counts, source job for failed-item retry, preview/schedule/cancellation times,
and version. Job history is indexed by status/creation and requester/creation;
materialization leases are independently reclaimable.

`RecoveryQuerySnapshotMember` is the immutable root-membership ledger for one
uncapped query preview. Each row stores the job/root key, canonical work
identity, captured version, optional file/stage identity, and whether the root
came from the outdated-video selector. Its bounded context fingerprint covers
selected-action eligibility, prerequisite state, active reservations, and exact
downstream row/missing-marker topology and versions. A unique job/root key
prevents duplicate capture, while the job/UUIDv7 index is the restart-safe
expansion cursor. The rows cascade only with their recovery job; canonical IDs
intentionally are not foreign keys so deleting or transitioning live work
cannot erase reviewed snapshot membership.

`RecoveryJobItem` stores one planned stage step, its root/parent/source item,
file/stage identity, expected and canonical versions, reservation state,
preserve-READY fence, terminal acknowledgement, dispatch event, starting
attempt baseline, retryable failures consumed, retry limit, sanitized result,
and timestamps. Unique job/work and job/file/stage constraints make resumed
materialization idempotent. Parent/status and failed-first indexes support
dependency dispatch and paginated operator reads. A partial unique active-
stage-reservation index prevents concurrent replay jobs from owning the same
canonical file/stage. A sibling partial unique work-reservation index fences
active non-stage targets by canonical work kind and ID, so concurrent admins
cannot queue duplicate ingest, outbox, backfill, source-post, or dead-letter
work.

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

`NULL` counters are meaningful: they mean the source did not expose the
counter. A known zero remains `0`. Ranking models may coalesce unknown to zero
for scoring, but canonical snapshots and public source/insight DTOs preserve
the distinction and publish coverage.

Initial ingestion writes an `ingest_initial` baseline snapshot. The first known
value contributes no invented activity. Public activity read models advance
each counter only above its running high watermark, so decreases contribute
zero and `100 → 90 → 100` is not double-counted. The separate per-meme
observed-counter series starts from an opening absolute baseline containing the
latest known aggregate state as of the selected range's `start_at`, then uses
the final absolute aggregate state in each server-selected bucket containing a
real observation. Its point time is the latest real `captured_at` represented
in the bucket, never an artificial bucket end or every raw capture timestamp,
and it may decrease after an upstream correction. Follow-up schedule slots are
anchored to the Telegram post date (`+1h`, `+3h`, `+12h`, `+1d`, `+3d`, `+7d`,
`+1month`, then monthly), not to ingest time.

`forward_count` is Telegram's public forward/repost count and feeds derived public `latest_source_reposts`. It is unrelated to forwarded-message provenance such as `forwarded_from_*`.

### Public Trend Read Models

`public_meme_trends_mv`, `public_tag_trends_mv`, `public_template_trends_mv`,
`public_tag_trend_points_mv`, `public_template_trend_points_mv`, and
`public_meme_recommendation_features_mv` are derived-cached read models. They
are rebuildable from
`meme_source_engagement_snapshots`, `analytics_events`, and current
meme/template metadata. Current source totals use the latest successful
snapshot per source post; historical/window source metrics use positive
increases above each counter's prior running high, matching per-meme Recorded
activity semantics. Internal platform metrics come from canonical event aliases;
the inline `meme_send` compatibility event is counted once rather than again as
`inline_sent`.

The public DTO names remain stable (`latest_source_views`, `latest_source_reactions`, `latest_source_reposts`, `latest_popularity_score`), but these values are derived read-model metrics. There is no canonical `memes.popularity_score` column and no `meme_popularity_snapshots` table.

`public_meme_recommendation_features_mv` contains one row per currently public
meme with a primary file. It records the latest live-source publication
time; all live source-channel IDs; a deterministic representative channel;
75th-percentile source popularity and quality quantiles; primary-file technical
quality; Bayesian-smoothed matched high-intent response; trend and popularity
quantiles; template ID; exposure/live-source counts; and per-feature coverage
flags. Source percentiles are computed within channel and publication-age
cohorts (`0–1d`, `2–7d`, `8–30d`, `31–180d`, and older). Source quality uses a
100-view cohort prior over `(reactions + 3 × forwards + 0.5 × comments) /
views`; platform response uses the global keyed-exposure mean across web-card
and Telegram-inline exposure kinds as a 20-impression prior.
Latest source publication falls back to meme creation only when provenance is
absent. A missing derived quantile or quality input is represented as neutral
`0.5` plus a false coverage flag, never as zero quality. A live source lacking
a successful engagement snapshot with a measured view count still contributes
its source metadata and any publication time, but not source-popularity or
source-quality coverage. Equal source or trend metrics share the same
percentile. Technical quality is covered only when a
successful transcode journal or attempt proves the primary file's score was
derived; the non-null `quality_score` storage default is not coverage. The view
is a ranking projection, not an authorization source; response hydration still
rechecks the canonical meme and primary file.

### Public Meme Insights Read Model

`PublicMemeInsightsService` derives one meme's public source and analytics DTOs
from canonical tables without creating another mutable truth store. Source
eligibility is strict: join `MemeSource -> MemeFile -> Meme`, require a visible
public meme, `platform=telegram`, and `source_kind=public_crawler`, and include
all matching files. A source page uses the latest successful engagement and
audience observations at or before its server-issued `snapshot_at`; the same
cutoff also excludes later source rows, keeping metric sorting and offset
pagination stable. Its query reads only one latest engagement row per post, one
latest audience row per channel, and the nearest eligible audience observation
within 48 hours before each post publication; it does not load indefinitely
growing history merely to render current source rows. The analytics projection
loads the history its selected range/baseline requires. Platform days and trend
periods are truncated explicitly in UTC rather than inheriting the database
session timezone.

`PublicMemeSourcePageRead` contains safe channel/post URLs, post availability,
nullable counters, measured-post coverage, ratio-of-sums rates, audience fields,
summary totals, and pagination only. `PublicMemeAnalyticsRead` contains UTC
window metadata, exact activity buckets, an opening absolute baseline with the
latest known aggregate state as of `start_at`, server-bucketed absolute end
states grounded in real observations, source performance, forward-only audience
change, and separate web/inline funnels.
Neither DTO contains raw platform/source IDs, uploader or operator provenance,
Telegram session/lease/error data, `raw_metrics`, source text,
forwarded-original fields, user/request/query data, or raw ranking scores.

### TelegramSession

Canonical registry for successfully promoted Telethon userbot sessions. Key fields: `name`, `display_name`, encrypted `encrypted_string_session`, account projection fields (`account_user_id`, `account_username`, `account_phone_hint`), `status`, `enabled`, per-session feature flags, and `max_requests_per_second`. Non-null `account_user_id` values are unique, so one Telegram account cannot be registered as multiple crawler sessions. API read schemas deliberately omit `encrypted_string_session`; runtime code decrypts it only when constructing `TelegramClient(StringSession(...))`.

### TelegramSessionLoginAttempt

Short-lived, standalone state for browser-admin QR and phone login. A new-account attempt exists without a `TelegramSession`; nullable `telegram_session_id` either targets an existing session for re-authentication or points to the session assigned after successful promotion. `created_by_admin_user_id` records the initiating operator without preventing user deletion. Temporary encrypted `StringSession`, phone-code, and QR fields are never exposed through API schemas.

Terminal attempt status includes `completed`, `failed`, `expired`, and `cancelled`. Cleanup is tracked independently through `cleanup_status` (`pending`, `promoted`, `discarded`, or `failed`), retry count, error details, and completion timestamp. This permits cancellation and TTL cleanup to revoke abandoned Telegram auth keys while retaining enough encrypted state for retry; successful promotion clears temporary secrets without logging out the canonical crawler credential.

### SourceChannel

Channels being crawled. Key fields: `platform`, `platform_id`, `username`,
`title`, latest-success `subscriber_count` cache and
`subscriber_count_updated_at`,
audience-capture state (`last_audience_capture_at`,
`next_audience_capture_at`, lock owner/time, attempt count, and last error),
`is_active`,
`last_read_post_id` (platform-specific, e.g. Telegram message ID — used to
resume on restart), `telegram_session_id` (nullable FK to the
`telegram_sessions` row that handles this channel), and live/catch-up/engagement
flags. Failed or not-exposed audience captures never clear a valid subscriber
cache. If a Telegram session is deleted, `ON DELETE SET NULL` leaves that
**Telegram** source channel as an orphan; it remains visible for operator repair
but is not runnable until reassigned. `orphaned` is deliberately Telegram-only:
a non-Telegram source is never made orphaned merely because this nullable
Telegram-specific field is empty.

The browser-admin list adds rebuildable aggregate fields rather than denormalized
columns: `latest_post_at = max(source_channel_posts.published_at)`,
`observed_post_count = count(source_channel_posts)`, and `meme_count = count`
of distinct canonical `meme_files.meme_id` values reached through matching
`meme_sources(platform, source_id)`. The service computes these for the bounded
source inventory in one aggregate read and combines them with the separately
bounded latest-backfill projection. The source-post channel/time index and the
`meme_sources(platform, source_id, post_id)` unique index support these reads;
there is no per-source query loop and no migration-owned counter that can drift.

### SourceChannelAudienceSnapshot

Forward-only Telegram channel-audience observation. Key fields:
`source_channel_id`, nullable `telegram_session_id`, `captured_at`, UTC
`capture_slot`, `capture_reason` (`initial_resolution`, `crawler_refresh`, or
`scheduled`), `fetch_status` (`success`, `not_exposed`, or `failed`), nullable
nonnegative `subscriber_count`, and safe `error_code`. For each unique
`(source_channel_id, capture_slot, capture_reason)` key, the first `success` or
`not_exposed` row is terminal and immutable; later same-slot attempts return it
unchanged. Only a `failed` row remains retryable and may be replaced by a later
`failed`, `success`, or `not_exposed` result. Known zero is preserved;
non-success rows cannot carry a count.

There is no historical reconstruction. A post's `audience_at_publish` is the
latest successful snapshot at or before publication only when it is at most 48
hours old. `current_audience` is the latest successful snapshot at the public
read cutoff. Per-1,000-subscriber ratios require a positive eligible audience
denominator and publish eligible/total coverage; summed subscriber counts are
never presented as unique reach.

### SourceChannelPost

The durable per-message Telegram inventory is the source of truth for post
context before deduplication. In addition to fetch/attempt state, it stores
`first_observed_text` and `latest_text`, their normalized allowlisted JSONB text
entities, `media_group_id`, `reply_to_post_id`, `telegram_edited_at`, metadata
first/last observation timestamps, `metadata_version`, and deletion state. No
raw Telethon object crosses this boundary. Existing rows begin at metadata
version `0`; successful captures use version `1`, where null text explicitly
means Telegram exposed no text. First-observed fields are populated only while
the prior version is below `1`; every successful observation updates latest
fields and clears a stale deletion marker.

`(source_channel_id, post_id)` remains the message identity. A partial
`(source_channel_id, media_group_id, post_id)` index supports album-membership
lookups only when `media_group_id` is present. Because `post_id` is stored as
text for platform compatibility, callers order Telegram album members by its
numeric value rather than lexical order. Albums do not introduce another model:
each member remains an independent source post/meme, and `Meme.files` continues
to mean alternative physical versions of one meme. `reply_to_post_id` is
populated only from Telegram's explicit reply header; chronology alone creates
no relationship.

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

### Recommendation Serving State and Profiles

`UserMemeRecommendationState` is the exact per-viewer, per-meme projection used
for cooldown correctness. Its composite key is `(user_id, meme_id)` and its
fields are `first_seen_at`, `latest_impression_at`,
`latest_engaged_view_at`, `latest_strong_action_at`, and nonnegative
`impression_count`. It is updated from idempotent events and current durable
collection/pin state. Serving hard-excludes the latest impression for 72 hours
and the latest strong action for seven days; this lookup is not bounded to the
most recent N event rows, so an older item cannot escape because a user has more
than 80 impressions. Ignored impressions are not negative preferences.

During guest-to-full merge, colliding state rows take the minimum
`first_seen_at`, maximum of each latest-event timestamp, and the sum of
`impression_count`. The source row is removed only as part of the account merge
transaction. Both viewer feed-pool namespaces are invalidated, and the target
`UserRecommendationProfileStatus` is marked dirty.

`UserRecommendationProfileStatus` has one row per user with recommendation
history. `dirty_since` makes rebuild work claimable; `last_rebuilt_at` and
`event_watermark` make lag and successful convergence observable. A persisted
profile whose embedding model or profile base version differs from current
configuration is also bounded rebuild work even when this ledger is clean;
serving ignores that stale vector until the rebuild converges.

`UserRecommendationProfile` stores PostgreSQL-authoritative long-term vectors,
never Qdrant user vectors. Slot `0` is the global centroid and slots `1–4` are
deterministic taste clusters. Each row records the embedding model and profile
versions, signal count, total weight, event watermark, encoded vector bytes,
and generation time. Clusters are materialized only from at least 20 distinct
strong-positive memes; deterministic cosine farthest-first initialization and
up to five spherical-centroid iterations produce two to four clusters, dropping
clusters below three items while retaining the global centroid.

`UserRecommendationProfileSignal` stores at most the top 500 decayed long-term
signals for one user, keyed by `(user_id, meme_id)`, with weight,
`last_signal_at`, and strong-positive status. Current Favorite, Save, or Pin
state contributes weight `5` only while it exists. Download, Send/Share, or
inline chosen/sent contributes `4` to both serving horizons. Send-family rows
with the same meme/impression identity collapse to one logical signal before
decay. Engaged view `2` and detail
view/click `1` contribute only to the seven-day short-term read; impression is
`0`. A current durable preference also enters the short-term read when its
add/pin timestamp is inside that seven-day window. Removal cancels a durable
contribution and is not a negative. Long-term
high-intent events have a 90-day half-life without a retention cutoff; the
online short-term read has a 24-hour half-life. The separate current-intent
vector lives only in Redis, has a 30-minute half-life and two-hour TTL, and never
stores raw query text.

`RecommendationDailyAggregate` is a bounded dashboard rollup uniquely keyed by
date, surface, algorithm version, profile version, and candidate source. It
stores impression, strong-action, attributed-send, result, exploration, and
impression-level fallback counts plus a bounded JSON metrics bag. The bag holds
strong/send rates, cooldown-repeat count/rate, fallback rate, catalog/long-tail
coverage, source/template concentration, exploration share/conversion, and
unique-meme count. Repeated contributions with the same typed source on one
keyed impression are deduplicated. `cache_expiry_count` is a reserved column
written as zero because cursor expiry exists only in request-path structured
logs; no synthetic analytics event is created. These rows do not replace raw
events as the audit or offline-evaluation source.

These tables and the migration that defines them are a repository contract,
not proof that revision `0043` has run or that profiles/features have been
backfilled on the live beta.

### TelegramFileIdCache

References **MemeFile**. Key fields: `file_id_tg` (PK), `meme_file_id`, `media_format` (photo/animation), `file_unique_id`.

### EmbeddingCache

PostgreSQL table — source of truth for computed embeddings. Synced to Qdrant for search.

Key fields: `input_hash` (SHA256 of input, unique), `input_type` (image/text), `embedding` (BYTEA, 1024 × float32 = 4096 bytes), `model_version`, `source_id` (FK → MemeFile for images, null for text queries).

Design notes:
- Stored as BYTEA, not a vector type — PG is not used for vector search.
- `model_version` enables future versioned recomputation. An atomic Qdrant alias
  switch requires the evidence-gated Phase-3 collection/dual-write migration;
  it is not part of the current single-collection architecture.
- Text query embeddings act as a query cache — intentionally no eviction, as the table size is bounded by unique query volume.

### SearchSynonymCatalog and SearchSynonymRevision

`search_synonym_catalogs` has one row per supported locale (`en`, `ru`). A
catalog owns exactly one mutable draft revision and at most one published
revision; older publications are archived rather than overwritten.

`search_synonym_revisions` stores the authored newline/comma source, lifecycle
status and revision number, deterministic compiled map, compiler version and
hash, validation/stats snapshots, optimistic-lock version, change note, admin
attribution, and publication/archive timestamps. Partial unique indexes enforce
one draft and one published revision per catalog. Published and archived rows
are immutable application history; rollback copies a selected snapshot into the
draft and requires a later publish.

### SearchSynonymSyncState

`search_synonym_sync_states` is a singleton durable reconciliation record for
the combined Meilisearch synonym map. It stores desired, applied, and last
observed hashes; desired/applied locale revision IDs; sync status; provider task
UID; bounded error text; attempt/success/failure timestamps; and an
monotonic row version. Publication updates desired state transactionally.
Only the scheduler performs the external settings replacement and advances
applied state.

### AccountMergeLog

Audit trail for guest → full account merges. Key fields: `guest_account_id`, `target_account_id`, `favorites_transferred`, `views_transferred`, `interaction_events_transferred`, `inline_usage_events_transferred`.

### AccountDeletionLog

Deferred/reserved. If present, it is a schema placeholder for a future account deletion flow; MVP does not require user-facing deletion/export behavior or scheduled hard delete jobs.

### AnalyticsEvent

General-purpose product analytics event stream. Key fields: `user_id`,
`event_type`, `payload` (JSON — event-specific data), `occurred_at`.

For `meme_impression`, `meme_engaged_view`, and `meme_detail_click`, the client
generates a UUIDv7 that is used directly as `analytics_events.id`. A repeated
write with the same ID and same canonical content succeeds as an idempotent
no-op; reuse for different content is a conflict. The interaction batch API
accepts at most 50 events. The browser further caps serialized batches at 48
KiB, starts bounded page-hide keepalive work even when a normal request is in
flight, isolates permanent 4xx poison events during ordinary delivery, and
drops pending viewer-bound tokens when authentication changes. It strips
optional properties from a single oversized event before discarding it only if
the required identity/token fields still exceed the byte limit.
The writer also collapses `meme_impression` and `meme_engaged_view` to one row
per viewer/meme/impression/stage even if a client mistakenly regenerates the
UUID. Client timestamps more than five minutes ahead of server time are
rejected so cooldown and profile watermarks cannot be pushed arbitrarily into
the future.
`meme_engaged_view` is emitted once per placement only after at least 50%
visibility for three accumulated foreground seconds; autoplay by itself does
not qualify.

Public per-meme analytics accepts both strict `payload.refs.meme_id` and legacy
root `payload.meme_id` references. PostgreSQL expression indexes on each shape,
followed by `event_type` and `occurred_at`, keep bounded public history queries
from scanning the full event stream; the response boundary still exposes only
aggregate counts, never raw payloads or actor data.

`page_view` stores only one approved coarse consumer surface (for example
`web_home`, `web_search`, or `web_meme_detail`) and an optional authenticated
actor. It never accepts or persists a raw URL, pathname, route parameter, query
string, referrer, IP address, or user-agent. A frontend route tracker emits one
event for each pathname navigation, excluding admin/API/auth/unknown routes.

Strict first-page `search_query` events store normalized raw query text and
request ID at the top of the versioned payload; `properties` stores
`result_total`, `returned_count`, `latency_ms`, `has_more`, and safe filters.
Raw query text is admin-only operational data, never a public/ordinary-user
read field. Admin aggregation associates a later detail-click/download event
with a search only through the shared request ID; it does not infer a
query-to-meme relationship from user identity, time proximity, or meme ID
alone.

The strict payload's `actor_account_type` is a guest/full snapshot resolved at
event write time. Admin active-account mix uses this event-time value so an
upgrade does not rewrite earlier behavior. For legacy events lacking the
snapshot, aggregation may look up the current `User.account_type` as a clearly
weaker compatibility fallback.

Auth lifecycle properties distinguish `full_account_created` from
`merge_performed`: a merge into an existing canonical account is a conversion
but never a new full account. `guest_was_persistent` keeps guest-to-full
conversion metrics from treating a one-request anonymous sign-up as a guest
conversion. Where historical lifecycle telemetry is absent, account creation
with the current derived `User.account_type` is a per-day compatibility
fallback.

Retention cohort membership is merge-stable: `guest_created` events use
`refs.source_user_id` as the immutable cohort identity and their current
`user_id` as the activity identity. Account linking may reassign that `user_id`
to an older canonical account, allowing later activity to count without moving
or deleting the original guest cohort member. Current `User.created_at` rows
supplement lifecycle events for legacy and direct-full accounts, but do not
duplicate a source identity already represented by `guest_created` telemetry.

Admin query reads derive an opaque, domain-separated HMAC `query_key` from the
normalized query. Lists and top-query aggregates return that key alongside raw
query text only to authorized admins; drill-down URLs carry the key and date
controls, never the raw query. The protected detail response resolves the key
within its reporting range under a bounded raw-event ceiling; ranges above that
ceiling fail explicitly instead of materializing an unbounded analytics stream
in the API process. A protected list or detail response, never the route URL,
delivers raw query text to the browser.

### MemeExposure

Privacy-bounded, idempotent public funnel fact keyed by
`(meme_id, kind, exposure_key)`, where `kind` is `web_card` or
`telegram_inline`. Nullable first-observed stage timestamps are `exposed_at`,
web-only `detail_clicked_at` and `high_intent_action_at`, and inline-only
`inline_chosen_at` and `inline_sent_at`. Chosen and sent remain distinct stages
even when Telegram reports both for one result. Upserts preserve the earliest
timestamp and allow an out-of-order conversion to arrive before the exposure
fact.

The table intentionally stores no user, query, request, rank, collection,
surface payload, or external/chat identifier. Public funnel denominators and
conversions come only from matched keys in this table. Analytics events without
an exposure key may contribute to a lower-confidence raw exposure total but
cannot be inferred into a funnel.

### MemeInteractionEvent

Append-only, recommendation-oriented interaction stream. Implemented as a strict, versioned payload schema on `AnalyticsEvent` so MemeExpert keeps one durable `analytics_events` stream instead of splitting interaction writes into a second table.

Key fields:

- `user_id` (nullable only for truly anonymous telemetry; normal web guests have users)
- `meme_id`
- `event_type` keeps legacy values (`impression`, `view`, `click`, `favorite`, `save`, `share`, `meme_view`, `meme_send`, `meme_like`, `meme_save`, `search_query`, `inline_query`) and adds canonical meme-scoped/foundation values (`meme_impression`, `meme_engaged_view`, `meme_detail_click`, `meme_download`, `meme_pin`, `meme_share`, `meme_report`, `inline_served`, `inline_chosen`, `inline_sent`, `collection_action`, `auth_event`, `account_merge`, `miniapp_open`, `channel_suggest`, `page_view`)
- `surface` (`web_home`, `web_search`, `web_related`, `web_collection`, `telegram_inline`, `telegram_pm`, `miniapp`)
- `source_algorithm` is a bounded server-authored string family rather than a
  database enum; current values include `hybrid_search`, `qdrant_similarity`,
  `personalized_recommendations`, `explicit_pins`, `public_trends_mv_*`, `motd`,
  and `fallback_*`
- `source_meme_id` for related/similar flows
- `collection_id`, `query`, `rank`, `score`, `score_components`, `reason`
- `request_id` / `impression_id`
- Payload envelope always stores `schema_version`, `actor_type`, `actor_account_type` (when a user exists), typed internal UUID refs under `refs`, and a JSON-safe `properties` bag for extra attribution.
- `actor_account_type` is an event-time snapshot, not a denormalized current
  `User.account_type`; this preserves guest/full behavior across an upgrade.
- Compatibility reads still accept legacy flat `payload.meme_id`, but new strict writes store meme attribution under `payload.refs.meme_id`.
- Raw `group_id`, `chat_id`, tokens, authorization/cookie headers, IP addresses, user agents, and request headers are forbidden. External/chat identifiers must be hashed before storage.
- `occurred_at`

Indexes: `(occurred_at)`, `(user_id, occurred_at)`, and
`(event_type, occurred_at)`. The standalone timestamp index bounds range scans;
payload grouping and query-to-meme attribution remain application-side until a
dedicated reporting/rollup model replaces raw-event materialization.

Raw interaction events are retained indefinitely in PostgreSQL. This design
does not add partitioning, archival, deletion, personalization reset, opt-out,
inferred dislikes, or a “Not interested” state. Current durable preference rows,
not historical add events, determine whether Favorite/Save/Pin contributions
remain active.

### InlineUsageEvent

Tracks inline bot usage per chat group for viral coefficient measurement. Key fields: `user_id`, `group_hash` (SHA256 truncated, for privacy), `timestamp`.
