# Content Pipeline

## Crawler Architecture

Plugin interface per platform. All crawlers normalize output to a common `RawMeme` dataclass (media bytes, media type, source metadata).

- **Telegram (Telethon):** Long-running userbot sessions listen to channel updates in real time via Telethon event handlers. Each channel's `last_read_post_id` is persisted in `SourceChannel`. On startup/reconcile, the manager waits until live handlers are registered and then runs bounded catch-up, so posts arriving around the handoff are either queued by the listener or found by the forward sweep. While idle, it polls an immutable projection of session/source control fields at the configured cadence; an in-flight reconciliation can delay the next poll. Runtime-owned checkpoints, fetch timestamps, heartbeats, and refreshed Telegram metadata are excluded from this projection so normal crawling does not cause reconnect churn. Multiple sessions (2–3) distribute channels for rate limit safety (≤30 req/s per session).
- **Reconciliation completion:** A control snapshot is considered applied only
  after catch-up and listener rebuilding complete without retryable report or
  session failures. Incomplete reconciliation remains pending and is retried on
  the normal interval even when the durable configuration itself did not
  change. Those same-snapshot retries preserve healthy live listeners; a newer
  control snapshot causes a full catch-up/listener rebuild. `SIGINT`/`SIGTERM`
  cancels an in-flight sweep before shutdown so deployments do not rely on a
  forced container kill.
- **Reddit (PRAW), VK (VK API):** Planned. Same plugin interface. Platforms without real-time push will poll on intervals, storing `last_read_post_id` in `SourceChannel`.

**Risk:** Telethon userbot accounts are subject to bans. Mitigation: multiple sessions, conservative rates, monitoring. Consider Bot API channel forwarding as a fallback read path.

## Browser Admin Contracts

The browser-admin API is cookie-authenticated and requires both a full account
and the durable admin flag.
The SvelteKit routes are a nested shell around task workspaces: `/admin`
(overview), `/admin/sources`, `/admin/telegram`, `/admin/moderation`,
`/admin/moderation/patterns`, `/admin/content/seo`,
`/admin/content/templates`, and `/admin/memes/[id]`. `/admin/content` responds
with a 303 redirect to `/admin/content/seo`. Desktop uses a sidebar and mobile
uses the same navigation as a horizontal scrolling strip.

### Overview read

`GET /api/v1/admin/overview` returns only aggregate counts:
`open_report_count`, `pending_suggestion_count`, `source_attention_count`,
`orphaned_source_count`, `stale_source_count`, `waiting_source_count`,
`healthy_source_count`, `telegram_account_attention_count`,
`ready_telegram_account_count`, `missing_seo_count`, and
`uncurated_template_count`. It is intentionally a bounded aggregate query, not
a dashboard payload of every admin collection.

Operational source counts include only active, unpaused rows. A source with no
successful fetch is waiting during the first 15 minutes after creation; after
that it needs attention when unassigned or stale. Removed and intentionally
paused rows do not inflate attention. Orphaned and stale are overlapping
subcounts: an old never-fetched orphan appears in both but contributes once to
source attention. An account needs attention when disabled, missing authorized
stored material, not `active`, currently flood-waited, or quarantined; a ready
count requires enabled, active, stored material without a current flood-wait or
quarantine. The frontend calls the human-facing objects **sources** and
**Telegram accounts** even though the durable crawler model retains
`SourceChannel` and `TelegramSession` names.

### Telegram account login lifecycle

Browser QR and phone login use a two-phase lifecycle. Starting a new connection
creates only a provisional `TelegramSessionLoginAttempt`; it does not create a
`TelegramSession`. The attempt owns its creator, method, expiry, sanitized phone
hint, provider continuation data, and encrypted temporary Telethon
`StringSession`. Its nullable `telegram_session_id` is an existing reconnect
target before authorization and the promoted/reused result afterward. Attempt
ids are opaque orchestration tokens that the UI does not render; temporary and
final secret material is never exposed through read models. A reconnect target
must not let one durable account be silently replaced with a different Telegram
user.

The browser contract is attempt-oriented:

- `POST /api/v1/admin/telegram/login-attempts/qr` and `/phone` start a
  provisional attempt;
- `POST /api/v1/admin/telegram/login-attempts/{attempt_id}/qr/complete`,
  `/phone/code`, and `/password` advance the matching flow;
- `DELETE /api/v1/admin/telegram/login-attempts/{attempt_id}` explicitly
  cancels an unfinished attempt.

After Telethon reports authorization, the service calls `get_me()` and promotes
the credential in one database transaction. Canonical `account_user_id`
identity drives an upsert: create a durable session for a new identity, or
rotate the encrypted credential on the existing session for an already known
identity. An explicit reconnect target is updated only when that identity
matches. Promotion restores the durable account to an enabled active state,
clears stale error/flood-wait/quarantine state as defined by account repair,
records the audit entry, sets the attempt to `completed` with
`cleanup_status=promoted`, and removes its QR URL, phone continuation data, and
encrypted temporary credential. The response contains the promoted/reused
durable account id; callers must not assume one before successful promotion.

Client retirement depends on ownership of the credential:

- after successful promotion, the temporary client is only `disconnect()`ed;
  `log_out()` would revoke the credential now owned by the crawler account;
- a failed, cancelled, or expired attempt is disconnected when it was never
  authorized;
- an abandoned attempt whose temporary auth key became authorized is first
  revoked with Telegram `log_out()`, then disconnected.

The explicit cancel operation makes dialog closure prompt but is best-effort;
the database TTL is authoritative. Terminal attempt status is one of
`completed`, `failed`, `expired`, or `cancelled`; nonterminal states are
`pending` and `password_required`. Cleanup is idempotent and separately tracked
as `pending`, `promoted`, `discarded`, or `failed`, with bounded attempt/error
metadata. If provider logout cannot be confirmed, the attempt retains only the
encrypted credential required to retry. The `telegram-login-cleanup` scheduler
job runs every 60 seconds, claims expired nonterminal attempts plus terminal
attempts whose cleanup is pending/failed, reconstructs the temporary client,
repeats revoke/disconnect safely, and clears temporary secrets after cleanup
succeeds. Cleanup therefore survives API process restarts and prevents both
dead database accounts and abandoned authorized devices in Telegram.

The focused lifecycle test matrix is:

| Scenario | Durable/account result | Client and attempt result |
| --- | --- | --- |
| Start QR or phone for a new account | No `TelegramSession` is created | Attempt is nonterminal with an encrypted temporary session when available |
| Complete authorization for a new identity | One active account is created | Credential is promoted; temporary client disconnects without logout |
| Complete authorization for an existing identity | Existing account credential rotates; no duplicate row | Result points to the existing account and disconnects without logout |
| Reconnect target authorizes a different identity | Target account is unchanged and completion conflicts | Temporary authorized key remains owned by discard cleanup |
| Cancel/expire before authorization | No account is created or modified | Attempt becomes terminal and client only disconnects |
| Cancel/expire after temporary authorization | No account is created or modified | Temporary key is logged out, then disconnected and discarded |
| Provider logout fails | No account is created or modified | Cleanup becomes `failed`, retains encrypted retry material, and increments attempts |
| Scheduler retry succeeds after restart | No account is created or modified | Cleanup becomes `discarded` and all temporary secret fields are cleared |

### Public Telegram source add

`POST /api/v1/admin/telegram/channels/from-reference` accepts a public Telegram
`reference`, a required selected `telegram_session_id`, optional `suggestion_id`,
and a bounded `catchup_message_limit` (default 5,000). It accepts only
`@handle`, bare handle, and one-path `t.me` or `telegram.me` public URLs. Invite
links, private/non-Telegram references, and paths with query/fragment content
are rejected. The selected account must be enabled, active, authorized, and
outside flood-wait/quarantine both before and after the bounded provider call.

The resolver returns secret-free metadata only. A public source's canonical
`platform_id` and `username` are the lowercase public username, allowing cold
crawler resolution without persisted access-hash material. This bounded flow
does not follow Telegram username renames; an operator must reconcile a rename
before treating the old public identity as current.

The service performs Telegram I/O without retaining a database row lock, then
locks/rechecks the selected account and canonical source identity before writing.
It atomically creates or reuses the source, assigns the selected ready account,
enables catch-up/live/engagement, and approves a matching pending suggestion in
the same transaction. The crawler discovers the committed control-state change
within `CRAWLER_RECONCILE_INTERVAL_SECONDS` (10 seconds by default), rebuilds
and confirms its live subscriptions, and runs the bounded initial catch-up. The HTTP request
does not wait for Telegram ingestion. A duplicate/retry reuses the canonical
source and still converges on the approved suggestion. The manual source endpoint
`POST /api/v1/admin/source-channels` remains an exception path for a known
canonical identifier: it creates an orphan with all ingestion controls disabled
and explicitly rejects Reddit/VK or any other non-Telegram platform. The list
projection remains platform-extensible for future crawlers. Reddit and VK are
not crawler implementations yet; their suggestions may be rejected but must
not create dormant source rows.

Until `initial_catchup_completed` is durable, the adapter repeatedly reads the
newest bounded window and yields it oldest-to-newest; this prevents a partial
initial sweep or an early live post from silently switching the source to
forward-only mode. Later forward catch-up remains contiguous above
`last_read_post_id`. Older-history pages are processed newest-to-oldest and use a
separate exclusive cursor, so a mid-page failure resumes without skipping the
unprocessed suffix and never moves the live high-water mark backward. `source_channel_posts`
durably inventories every observed message before media handling, including
unsupported and provider-failed messages. Browser admin joins that ledger to
`pipeline_ingest_requests`, `pipeline_stage_journal`, and
`meme_file_sync_target_snapshots`; an item is indexed only when both Qdrant and
Meilisearch are synced. The Meilisearch adapter waits for asynchronous settings
and document tasks to finish before recording sync success. Admin message pages
are filtered by a server-issued observation snapshot to keep offset pagination
stable while ingestion continues. `source_channel_backfill_jobs` stores bounded
manual older-history requests and progress so crawler restarts can resume them.

### Admin media and content workspaces

Admin meme projections include a `primary_file` with a private authenticated
render URL. `GET /api/v1/media/files/{file_id}/{variant}` grants private-file
access to a full account with the durable admin flag; unrelated non-admin users
receive 404 unless the normal public-or-collection access rule applies. The
DTO and render URL never disclose an S3 object key. This lets moderation and SEO review safely
render image, video, or audio previews for hidden media.

Moderation reports are list-first at `/admin/moderation`; blocked pHash patterns
are isolated at `/admin/moderation/patterns` and keep raw hash tuning/lifecycle
actions disclosed. SEO review is list-first and paginated through `?page=` in
25-row pages. Templates are list-first and client-searchable; their create,
edit, merge, and delete controls are disclosed. Technical diagnostics, policy
repair, and danger actions remain available but are never the routine default.

## Content Identity And Deduplication

The content model has three separate owners of truth:

- `PipelineIngestRequest` is the pre-content raw-ingest source of truth. It records `source_kind` plus nullable `uploader_user_id`. API-safe entrypoints create this row after stdlib SHA256/idempotency checks and temporary original-object upload. Heavy workers later inspect media and either materialize `Meme`/`MemeFile` rows or mark a terminal failure.
- `Meme` is the conceptual meme. It owns `visibility_mode`, materialized `is_public`, popularity, collections, SEO page linkage, and the canonical `primary_file_id` pointer. It has no singular owner.
- `MemeFile` is one physical media file. `sha256_hex` is the only exact same-bytes identity and is unique. File rows own physical metadata, S3 keys, pHash, ingest origin, and optional match lineage.
- `MemeSource` is one provenance observation. Source rows preserve where a file or duplicate was observed, `source_kind`, nullable uploader, attach reason, and any matched file id.

### Ingest-Time Identity

SHA256 is computed immediately after bytes are available, before media inspection, blocked pHash checks, canonical S3 writes, or pHash duplicate lookup. Exact decisions take a PostgreSQL transaction advisory lock derived from the SHA, then recheck every `MemeFile` (not only primary files) before writing.

| Condition | Result |
|-----------|--------|
| Source identity `(platform, source_id, post_id)` already has a raw ingest request | Return the existing `PipelineIngestRequest`; do not upload or enqueue again. |
| SHA256 matches an existing non-blocked file | Reuse the canonical meme/file globally, create/update the ingest request as `resolved_sha_duplicate`, do not inspect media or enqueue media-inspect, attach a `MemeSource`, add the uploader's target collection membership when present, and recompute effective visibility atomically. |
| SHA256 matches an existing blocked/quarantined file | Create/update the ingest request as `resolved_sha_duplicate`, do not inspect media or enqueue media-inspect. Attach a `MemeSource` to the existing blocked file with `attach_reason=blocked_sha256_existing_file`. |
| SHA miss | Store bytes under the temporary-original prefix, create a `PipelineIngestRequest` with `status=media_inspect_pending`, and write a `rabbitmq_outbox_messages` row in the same DB transaction for a media-inspect worker event carrying `ingest_request_id`. |
| Worker cannot inspect/read media | Mark the ingest request `failed_invalid_media`, record failure code/detail, create no `Meme`/`MemeFile`, and retain the temporary object for operator retention/debugging. No downstream event is written. |
| Worker finds active blocked pHash | Promote the original to the canonical key, create hidden failed `Meme`/`MemeFile`/`MemeSource` audit rows, mark the ingest request `failed_blocked_phash`, clean the temporary object, and write no normal transcode event. |
| Worker finds eligible exact pHash match | Treat this as the same conceptual meme but a new physical file only when both sides are public or both are private with the same sole uploader. Promote the original to the canonical key, create a new `MemeFile` under the matched `Meme`, attach source and collection membership, and write the downstream event transactionally. |
| Worker finds no eligible pHash match | Promote the original to the canonical key and create a separate `Meme` plus primary `MemeFile`. User/operator content starts private; crawler content starts public. |

Crawler duplicate-post idempotency is separate from media identity: an already-seen `(platform, source_id, post_id)` returns the existing source row before service-owned media processing. Telegram `file_unique_id` is not a content identity and there is no separate unique media identity table.

An exact crawler source attached to an `auto` private upload promotes the existing meme to public. `force_private` attaches the crawler provenance but suppresses promotion. Approximate crawler matches never consume private content: they create or merge into a separate public meme.

### Post-Embed Semantic Merge

After the embed stage computes a file embedding, semantic merge may query Qdrant for high cosine similarity and merge two `Meme` concepts. This is not exact-byte identity and not the exact-pHash same-meme ingest path. Qdrant filters candidates to public→public or private→private with the same sole uploader; the merge transaction locks both memes and repeats the PostgreSQL provenance check. When semantic merge fires, files, sources, collection memberships, pins, and like counts are consolidated into the target meme and the duplicate meme entity is deleted according to the merge service's invariants. Public popularity is derived later from the moved source rows and analytics events.

### Historical SHA reconciliation

The ownership/provenance migration is deliberately staged. Apply `0031`, keep ingestion paused for the final pass, and run `uv run memexpert-reconcile-sha-duplicates` until `--verify-only` succeeds. Each SHA group commits independently and transfers sources, nonduplicate files, collections, pins, moderation/SEO/pipeline/cache/history rows; visibility conflicts resolve `force_private` over `force_public` over `auto`, and likes are recomputed from distinct Favorites owners. Only then apply `0032`, remove unreferenced obsolete S3 objects from merge-log details, purge stale point/document IDs, and fully rebuild Qdrant and Meilisearch.

Classify success is the item-level readiness boundary. Qdrant and Meilisearch
sync start, success, failure, and replay update their own stage/snapshot truth
without demoting a ready `MemeFile`; either target may lag while the database
catalog remains available. Revision `0033` repairs historical post-classify
files left in `processing`/`failed` by sync work and restores unmoderated legacy
public-crawler rows to automatic public visibility.

### Admin Merge

When admins merge memes manually, the request transaction moves database
relationships (files, sources, collection memberships, pins, and like counts),
reselects the primary file, deletes the source meme, and records merge/destructive
audit rows. It does not synchronously write Qdrant or Meilisearch. The normal
search-sync/reconciliation path later updates external indexes and derived
popularity from durable PostgreSQL state.

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

Every worker subscriber uses `PIPELINE_WORKER_PREFETCH_COUNT` (default `1`) as
RabbitMQ consumer QoS. The limit is per queue consumer in each worker process,
which prevents a backlog from creating unbounded in-flight OCR subprocesses.
PaddleOCR receives `cpu_threads=1`, and worker images cap OpenMP/OpenBLAS/MKL/
NumExpr native thread pools at one by default; operators must opt up explicitly
after sizing the host.

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

PaddleOCR is the live OCR engine for Russian/English meme text. The worker image keeps the main app on Python 3.14, but runs PaddleOCR from a separate Python 3.13 helper venv because PaddlePaddle does not currently publish CPython 3.14 wheels. The helper runs `PaddleOCR(lang="ru", use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, cpu_threads=1)` and returns JSON across the `PIPELINE_OCR_PADDLE_COMMAND` boundary. `PIPELINE_OCR_PROVIDER_MODE=fake` remains the deterministic CI/E2E path. There is no active Qwen/VLM fallback in this implementation; optional fallback metadata/commands are blank unless a real command is configured.

### Embedding Pipeline

Voyage AI `voyage-multimodal-3.5` handles both image and text embeddings. Corpus
images use the provider retrieval intent `input_type=document`; user search text
uses `input_type=query` (the field is retrieval intent, not media modality).
Embeddings are 1024 dimensions with Matryoshka support (can reduce to 512 later
for cost/speed). Embeddings cached in PG — the cache table is the source of truth,
Qdrant is a search index. A missing Qdrant collection is treated as an empty
similarity corpus; the first sync creates a cosine collection with the configured
embedding dimension and retries its upsert. The first Meilisearch sync likewise
creates/configures the index before writing the document.

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

The current browser-admin SEO workspace is a paginated, list-first review queue.
Each row keeps its editor disclosed until needed and supports two exact actions:

- **Manual edit:** save the slug, title, description, alt text, caption, body,
  and tags for one meme.
- **Regenerate and overwrite:** an explicit `REGENERATE` confirmation lets the
  provider replace that row's SEO text/catalog tags, reassign its template, and
  clear manual edits.

Bulk regeneration and field-selective AI editing are not current browser-admin
features. Individual regeneration is intentionally destructive enough to remain
inside the row's danger disclosure.

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
