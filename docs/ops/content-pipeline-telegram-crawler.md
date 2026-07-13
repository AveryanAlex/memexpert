# Telegram Crawler + Freshness SLO Runbook

This runbook covers the Telegram crawler chain (Telethon catch-up + live listener →
raw ingest accept → media-inspect materialization → transcode → ocr → embed →
classify → sync_qdrant + sync_meili) plus the freshness endpoint that measures
end-to-end p50/p95 against the numbers configured in
`memexpert.core.config.Settings`.

This file focuses on Telegram crawler runtime operation, freshness inspection,
per-channel replay/repair, and flood-wait/ban recovery.

## Overview

The crawler ships five operator-facing pieces:

1. **Telethon adapter** (`memexpert/crawlers/telegram/telethon_adapter.py`)
   bound to DB-backed Telethon `StringSession` material. Flood-wait, session
   ban, auth-required, provider-unavailable, and malformed-message failures all
   land in the typed crawler-error taxonomy.
2. **Crawler manager/runtime** (`memexpert/crawlers/telegram/manager.py` plus
   `runtime.py`) supervising all runnable Telegram sessions in one process.
   The manager discovers runnable DB rows, keeps one cached client/rate limiter
   per session, and delegates each session's catch-up, live listener, and replay
   work to the per-session runtime executor.
3. **Browser-admin workspaces** for cookie-authenticated durable admins:
     `/admin/telegram` manages Telegram accounts and login/repair,
     `/admin/sources` manages source suggestions, public-reference add,
     assignment, health, and ingestion; the supporting API is under
     `/api/v1/admin/*`.
4. **Operator crawler API surface** (`memexpert/api/routes/v1/crawler.py`)
    exposing `/api/v1/crawler/sessions`, `/channels`, `/pause`, `/resume`,
    `/reassign`, `/replay-post`, and `/freshness` for runtime inspection and
    replay automation.
5. **Freshness SLO snapshot** (`GET /api/v1/crawler/freshness`) that evaluates
   the freshness SLO numbers configured via
`Settings.crawler_freshness_slo_p50_seconds` (default **60s**) and
`Settings.crawler_freshness_slo_p95_seconds` (default **180s**).

## Prerequisites

- Docker Compose healthy: `IMGPROXY_PORT=18080 docker compose up -d`.
  Postgres, RabbitMQ, Qdrant, Meilisearch, and MinIO must report healthy
  before runtime diagnostics are meaningful.
- Alembic head applied: `uv run alembic upgrade head`.
- The native API running on `http://127.0.0.1:8000`: `uv run memexpert-api`.
- The SvelteKit frontend serving the `/admin` workspaces. For local development,
  run the frontend from `frontend/` with `pnpm dev`; set `API_BASE_URL` when
  the API is not reachable at the frontend default.
- The heavy workers running: `uv run memexpert-workers`. These
  process both `media_inspect_requested` events from raw crawler accept and
  the later `transcode → ocr → embed → classify → sync_qdrant → sync_meili`
  chain that materialized crawler content feeds. Keep
  `PIPELINE_WORKER_PREFETCH_COUNT=1` for the first large channel; each worker
  queue consumer then has at most one unacknowledged item, and the worker image
  also caps Paddle/OpenBLAS native threads at one.
- A browser session cookie for a user with the durable admin flag. The
  `/api/v1/admin/telegram/*` routes use the normal cookie-authenticated admin
  guard; they do not accept the operator token.
- Environment variables loaded through the project `.env` / `Settings` surface:
  - `PIPELINE_OPERATOR_TOKEN` — required by the operator-token crawler and
    pipeline routes used in the commands below.
  - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — the Telethon app
    credentials used by browser-admin login/validation, scheduler cleanup, and
    the CLI helper. These are per-environment secrets; never commit them.
  - `TELEGRAM_SESSION_ENCRYPTION_SECRET` — high-entropy secret used to
    encrypt/decrypt DB-backed Telethon `StringSession` values in
    `telegram_sessions.encrypted_string_session` for browser admin, the CLI
    helper, and runtime reads. Production must set a real value and keep it
    stable across deploys.
  - `CRAWLER_FRESHNESS_SLO_P50_SECONDS` / `CRAWLER_FRESHNESS_SLO_P95_SECONDS`
    — override the defaults when running against a slower or faster
    stack profile.
  - `CRAWLER_DEFAULT_CATCHUP_MESSAGE_LIMIT` — the default catch-up
    message window the runtime consumes when a channel row does not
    override it. Keep it bounded — the runtime will refuse to walk more
    than the configured limit per sweep.
  - `CRAWLER_MAX_REQUESTS_PER_SECOND` — fallback token-bucket ceiling applied
    by each Telethon adapter until the session row's `max_requests_per_second`
    policy is loaded. Lower this before raising
    `CRAWLER_DEFAULT_CATCHUP_MESSAGE_LIMIT`.
  - `CRAWLER_LIVE_MODE_ENABLED` — guards the live listener start-up. Set
    to `false` in environments where catch-up is the only sanctioned
    mode.
  - `CRAWLER_RECONCILE_INTERVAL_SECONDS` — cadence at which an idle crawler
    polls for committed session/source control changes. The default is **10s**;
    an in-flight catch-up/reload can delay the next poll. Polling reads
    PostgreSQL only; Telegram clients are rebuilt only when the control
    projection changes.

## Browser-admin workspaces

The browser UI is a cookie-authenticated control plane that requires both a
full account and the durable admin flag. It calls `/api/v1/admin/*` through
SvelteKit and deliberately calls the routine objects **Telegram accounts** and
**sources**. The runtime/data model still uses
`telegram_sessions` and `source_channels` internally; use those terms in SQL or
operator-token API diagnostics, not as the normal browser workflow.

| Workspace | Routine purpose |
| --- | --- |
| `/admin` | Linked attention counts only. |
| `/admin/sources` | Suggestions, add flow, source health, account assignment, and ingestion controls. |
| `/admin/telegram` | Account login, validation, policy repair, and disconnect. |
| `/admin/moderation` and `/admin/moderation/patterns` | Report review and blocked visual-pattern policy. |
| `/admin/content/seo` and `/admin/content/templates` | SEO queue and template curation. `/admin/content` redirects to SEO. |

The overview excludes paused/removed sources from operational counts. A
never-fetched active source is **waiting** for its first 15 minutes; it becomes
source attention once it remains orphaned or stale. Orphaned and stale counts
overlap, so do not add their displayed subcounts: an old never-fetched orphan is
one source-attention row in both diagnostics. Account attention means the
account is disabled, inactive, missing authorized stored material, currently
flood-waited, or quarantined. Counts are triage links, not a real-time crawler
command.

### Telegram accounts

Before the runtime can ingest Telegram content, connect each account the
manager should run. QR is the primary workflow: scan the QR in Telegram →
Settings → Devices → Link Desktop Device and wait for automatic polling. The QR
code refreshes before expiry. Phone sign-in is the disclosed fallback: enter the
phone number once, then the Telegram code and account password only when
requested.

The UI never asks an operator to copy/paste a login attempt id and never renders
raw credentials. Full phone numbers, passwords, temporary login state, final
StringSession material, and encrypted database values are excluded from API
reads, rendered HTML, logs, and audit snapshots. Diagnostics may say only that
an authorized credential is present; account identity, readiness, source count,
and heartbeat are the routine card contents.

- Starting QR or phone login creates a temporary login attempt only. A new
  account does not appear in the account list until Telegram authorization and
  identity lookup both succeed. Closing the dialog sends a best-effort cancel;
  expiry in the database remains authoritative when the browser or API process
  disappears.
- Successful login stores the authorized Telethon material encrypted in the DB
  and only then creates or updates the account projection. Canonical Telegram
  account identity is unique: reconnecting an existing identity rotates its
  credential instead of creating another card. An explicit reconnect target
  must resolve to the same identity. The account is enabled, returned to
  `active`, and has stale parked/error state cleared. Newly created accounts
  enable catch-up/live/engagement by default and use
  `max_requests_per_second=1`.
- Temporary credentials are retired differently depending on the outcome. A
  successfully promoted client is only disconnected. A failed, cancelled, or
  expired attempt is disconnected when unauthorized, but an authorized
  abandoned auth key is revoked with Telegram logout before disconnect. This
  prevents unfinished login flows from accumulating as Telegram devices.
- Use **Validate account** for a routine access check. Diagnostics holds raw
  timestamps/status/error category; Advanced settings holds policy/rate/repair
  controls. A separate **Disconnect account** danger disclosure requires
  `DISCONNECT` and unassigns every source while turning its ingestion off.
- A ready account is enabled, `active`, authorized, and outside flood-wait or
  quarantine. Source assignment and public-reference lookup enforce this again
  on the API even if the browser shows an account as ready.

The headless helper is an exceptional fallback for importing or replacing an
existing account credential without a browser:

```bash
uv run python scripts/auth_telegram_session.py \
  --session-name primary \
  --display-name "Primary crawler" \
  --string-session-file /run/secrets/telegram_string_session
```

You can also provide the existing StringSession through `--string-session` or
`TELEGRAM_STRING_SESSION`. The helper never creates `.session` files and never
prints the StringSession value.

#### Login-attempt cleanup

Normal cleanup is automatic and applies to both QR and phone flows:

1. When the connection dialog closes, the browser sends `DELETE` to
   `/api/v1/admin/telegram/login-attempts/{attempt_id}`. Treat this as latency
   optimization, not the only cleanup mechanism.
2. API-side completion/cancel/expiry cleanup retires the in-memory Telethon
   client and removes temporary QR, phone continuation, and encrypted session
   data after retirement succeeds.
3. The `telegram-login-cleanup` job in `memexpert-scheduler` runs every 60
   seconds. It claims expired `pending`/`password_required` attempts and
   terminal attempts whose cleanup is `pending` or `failed`, rebuilds the
   temporary client from its encrypted credential, revokes an authorized
   abandoned key, and disconnects it. Cleanup is idempotent and safe to retry
   after process or provider failures.
4. A cleanup-failed attempt retains the encrypted temporary credential only so
   the scheduler can retry revocation. It never becomes a crawler account and
   is not returned in the routine account list.

If unfinished attempts or Telegram devices appear to accumulate, first verify
that `memexpert-scheduler` is running and inspect `telegram-login-cleanup` for
provider/network failures. Do not delete cleanup-failed rows manually: removing
their encrypted temporary credential makes server-side Telegram logout
impossible. Once cleanup succeeds, the job sets cleanup to `discarded`, clears
the credential and provider continuation fields, and preserves only non-secret
terminal metadata for audit/debugging.

### Sources and assignment

Use `/admin/sources` instead of SQL setup or `/admin/telegram` for routine
source work. The normal **Add Telegram source** form accepts one public
reference: `@handle`, bare handle, or a one-path `t.me`/`telegram.me` URL. It
rejects invite links, private links, and other platforms. Choose the exact ready
account that should fetch it; the form selects automatically only when exactly
one account is ready.

The API resolves public metadata with the selected account outside a database
lock, rechecks readiness, then stores the canonical lowercase Telegram username
as both public source identity and handle. It assigns that account and enables
catch-up, live collection, and engagement with the latest 5,000 messages as the
bounded first catch-up by default. Public Telegram username renames are not followed
automatically; reconcile a renamed handle as an operator exception before
expecting continued fetches.

When the form is started from a Telegram suggestion, source creation/reuse and
approval of the matching pending suggestion happen atomically. A failed lookup
leaves the suggestion pending; retrying after a successful create returns the
same canonical source rather than duplicating it. Reddit and VK suggestions are
explicitly unsupported while their crawlers do not exist: reject them or leave
them pending, but do not create inert source rows.

Advanced manual entry is the fallback when the canonical Telegram identifier is
already known. It deliberately creates an unassigned, non-indexable source with
catch-up/live/engagement disabled. From the source card, assign a ready account
and enable the desired ingestion controls. An assigned source is indexable only
when active, unpaused, and at least one workload control is enabled. Removing
an account or disconnecting it forces the same safe unassigned/disabled state.

Source cards show health, **last fetched**, assigned account, and a
message-indexing link first. The source detail page compares every fetched post
with its pipeline/search state, including unsupported and failed observations.
"Indexed" means both Qdrant and Meilisearch report `synced`, not merely that an
ingest request exists. Pagination carries a server-issued observation snapshot,
so messages arriving while an operator changes pages cannot shift rows between
offsets; return to the source detail URL without that snapshot to see newer rows.

Use the older-history form on that page to queue a bounded manual backfill (for
example, 16,000 posts after an initial 1,000-message window). The crawler resumes
durable queued/running work independently of the live listener. Older history
is processed newest-to-oldest within each page so a failed page can resume
without skipping its unprocessed suffix. It advances `oldest_observed_post_id`
and the private history cursor only, never `last_read_post_id`; an empty older
page marks Telegram history exhausted. The form stays unavailable until the
bounded initial window completed and requires catch-up to be enabled on both
the source and its assigned ready Telegram account.

Diagnostics contains source/checkpoint identifiers; ingestion settings,
assignment, source access validation, and remove-source confirmation are
progressive disclosures. The catch-up limit is a per-source forward-sweep bound:
lower it before raising an account or global request rate.

The generic browser-admin `POST /api/v1/admin/source-channels` endpoint uses
the same Telegram-only creation policy as the page. It rejects Reddit/VK rather
than creating an orphaned row; list/read payloads retain platform data so a
future crawler can add support without changing operational reads.

### Moderation and content work

`/admin/moderation` is a media-first report queue. Its private/hidden previews
use the authenticated `/api/v1/media/files/{file_id}/{variant}` proxy, not S3
keys; full accounts with the durable admin flag can render them while unrelated
users still receive 404. `/admin/moderation/patterns` lists active/inactive pHash patterns before
disclosing raw hash/tolerance and danger controls. `/admin/content/seo` is a
25-row `?page=` review queue; templates are searchable/list-first at
`/admin/content/templates`, with edit/create/merge/delete controls disclosed.

### Audit trail

Telegram admin writes insert rows into `telegram_admin_audit_logs`. The audit
row records the admin user id, action, affected Telegram session/source-channel
ids, before/after snapshots, and operator note when supplied. Session secret
material is not written to the audit snapshot; only `has_string_session` is
recorded.

### Browser admin vs crawler API

Use browser admin for account login/validation/policy and source
add/assignment/ingestion. Use the operator-token `/api/v1/crawler/*` endpoints
for runtime tasks: list the runtime projection, pause/resume channels, replay
one Telegram post, and read freshness snapshots. Admin and operator writes do
not synchronously call Telegram. An idle crawler polls committed control state
at `CRAWLER_RECONCILE_INTERVAL_SECONDS`, performs catch-up, and then rebuilds
live listeners. An in-flight reconcile can delay the next poll. `SIGHUP` remains
the manual immediate-reconcile override.

## Starting the crawler runtime

Run the dedicated Telegram crawler process before expecting fresh crawler data:

```bash
uv run memexpert-telegram-crawler
```

The command resolves normal project settings, builds its own async database
engine/session factory, constructs `TelegramSessionManager`, registers live
subscriptions, and then performs the bounded catch-up reconciliation. Registering
first closes the gap in which a Telegram post could otherwise arrive between the
final forward poll and event-handler installation. It stays in the foreground until
stopped. While waiting, it compares the durable crawler control projection at
the configured reconciliation interval. A changed projection causes a full
client/listener rebuild with listener registration before catch-up; unchanged
polls make no Telegram requests unless the same projection has incomplete
work. Incomplete work is retried without stopping healthy live listeners.

Signal behavior:

- `SIGHUP` forces the same live-registration-then-catch-up reconciliation immediately and
  then continues waiting. Routine browser-admin/operator changes do not require
  the signal.
- `SIGINT` / `SIGTERM` request shutdown. The process closes signal handlers,
  calls `manager.shutdown()`, disposes its owned SQLAlchemy engine, and exits.

Production compose runs the same command as the first-class `telegram-crawler`
service from the worker image, because Telethon lives in the worker dependency
group.

`TelegramSessionManager` supervises many DB-backed Telegram sessions in one
process while `TelegramCrawlerRuntime` remains the per-session executor
underneath.

Manager discovery rules:

- Runnable sessions come from `telegram_sessions` rows that are `enabled`,
  `status='active'`, not future flood-waited, and have nonblank
  `encrypted_string_session` material. `catch_up_all()` additionally requires
  `catchup_enabled=true`; `start_live_all()` additionally requires
  `live_enabled=true` and global `CRAWLER_LIVE_MODE_ENABLED=true`.
- Channels are processed only when the `SourceChannel.telegram_session_id`
  assignment points at the runnable session, the channel is active, the channel
  is not paused, and the relevant workload toggle is enabled.
- Orphaned sources remain visible in `/admin/sources` and the crawler API, but
  they are non-indexable and are skipped by catch-up, live listening, and
  replay.
- The manager keeps one cached Telethon client and one rate limiter per runnable
  session. Sessions are loaded from encrypted DB `StringSession` material; no
  filesystem `.session` files are created or read.
- Flood-wait, auth-required, or quarantine state affects only the failing
  session. Healthy sessions continue catch-up/live work in the same manager
  process.
- Configuration reconciliation fingerprints only session/source control fields.
  Assignment, active/paused state, catch-up/live flags, limits, credential
  replacement, and account policy changes trigger a reconcile; checkpoints,
  fetch timestamps, heartbeats, and metadata refreshes do not.

Watch for these structured lifecycle events in crawler logs:

- `telegram_crawler_runtime_starting`, `telegram_crawler_catchup_completed`,
  `telegram_crawler_runtime_started`, and `telegram_crawler_runtime_stopped`.
- `telegram_crawler_reload_requested`, `telegram_crawler_reload_started`, and
  `telegram_crawler_reload_completed` when `SIGHUP` is received;
  `telegram_crawler_reload_failed` leaves the process alive for a later retry.
- `telegram_crawler_reconcile_started` and
  `telegram_crawler_reconcile_completed` when polling finds changed durable
  control state; `telegram_crawler_reconcile_failed` is retried on a later poll.
  Completion logs include `retry_required` and failed session names. Retryable
  provider/download failures leave the control snapshot pending, so the next
  interval retries even if no admin field changed. That retry preserves healthy
  listeners; a concurrent durable control change still selects the full rebuild
  path.
- `telegram_crawler_channel_catchup_completed` for per-channel scanned,
  ingested, unsupported, deduplicated, and error counts.
- `telegram_crawler_stop_requested` when `SIGINT` or `SIGTERM` is received.
  In-flight startup/reconciliation work is cancelled before clients and the DB
  engine are closed.

The freshness endpoint only observes freshness - it does not trigger catch-up or
start the crawler runtime.

## Verifying a newly activated source

Initial historical catch-up is intentionally excluded from freshness SLO
scoring, so `/crawler/freshness` alone cannot prove a new source is indexing.
Verify the durable chain instead:

1. Confirm the source is active, unpaused, assigned to a ready account, and has
   catch-up/live enabled.
2. Wait up to `CRAWLER_RECONCILE_INTERVAL_SECONDS` and find
   `telegram_crawler_reconcile_completed` plus the source's
   `telegram_crawler_channel_catchup_completed` log.
3. Confirm `source_channels.last_fetched_at` is non-null. A non-empty channel
   should also advance `last_read_post_id`; an empty successful poll leaves the
   checkpoint empty but still updates `last_fetched_at`.
4. Open the source's admin message-indexing page and confirm
   `source_channel_posts` rows appear for supported, unsupported, and failed
   observations.
5. Confirm `pipeline_ingest_requests` rows appear for supported messages with the source's
   `(source_platform='telegram', source_id=<platform_id>)` identity.
6. Follow the materialized file through `pipeline_stage_journal` and
   `meme_file_sync_target_snapshots`; both Qdrant and Meilisearch must reach a
   synced state before claiming search indexing is complete.

## Inspecting freshness snapshots

```bash
curl -s "http://127.0.0.1:8000/api/v1/crawler/freshness?limit_per_channel=100" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN" | jq
```

Key fields:

| field | meaning |
|-------|---------|
| `observed_item_count` | How many items in the snapshot reached both sync targets. |
| `p50_seconds` / `p95_seconds` | End-to-end freshness percentiles. |
| `slo_p50_seconds` / `slo_p95_seconds` | The configured SLO thresholds. |
| `slo_p50_pass` / `slo_p95_pass` | Whether the observed percentiles are inside the SLO. |
| `channels` | Per-channel roll-up with per-channel item counts and SLO pass flags. |
| `sample_items` | Per-item breakdown with freshness, current pipeline stage/status, and Qdrant/Meili status + failure reason fields. |

### `slo_bucket` tags

Classify each sample item while triaging:

- `pass` — freshness inside `slo_p50`.
- `breached_p50` — between `slo_p50` and `slo_p95`.
- `breached_p95` — at or above `slo_p95`.
- `incomplete` — the sync chain never reached both targets (sample has
  `freshness_seconds=None`). Treat as "no data", not as a breach; read the
  same row's pipeline and target fields to see whether the item is blocked,
  partially searchable, or still in flight.

### Per-item freshness evidence

The freshness endpoint carries enough per-item evidence to choose the next
diagnostic surface without guessing:

| field | meaning |
|-------|---------|
| `searchability` | `ready`, `partially_searchable`, `blocked`, or `in_flight` based on current stage + target truth. |
| `pipeline_stage` / `pipeline_status` | Furthest active or completed stage from `pipeline_stage_journal`. Failed rows include `failure_reason` and `failure_text`. |
| `qdrant_status` / `meili_status` | Per-target status, preferring `meme_file_sync_target_snapshots` and falling back to sync stage-journal rows. |
| `qdrant_reason` / `meili_reason` | Normalized provider or payload failure reason for the target, when known. |

If an item is `partially_searchable`, user search may work through one target
but the product promise is not fully proven. Use
`GET /api/v1/pipeline/items/<meme_file_id>/detail` and the per-target sync
routes from `docs/ops/content-pipeline-search-sync.md` for the full stage and
target history.

### Channels with no fresh items

When an expected channel produced zero items in the snapshot window, check these
causes in priority order:

1. **Still inside the reconciliation window.** A just-created or reassigned
   source may wait up to `CRAWLER_RECONCILE_INTERVAL_SECONDS` before catch-up
   starts. Historical catch-up items are not scored by the freshness endpoint;
   use the verification sequence above.
2. **Session flood-wait or ban.** Check
   `GET /api/v1/crawler/sessions` for the owning session's `status` and
   `flood_wait_until`. If the session is parked, no channel bound to
   it can produce items.
3. **Source orphaned or non-indexable.** Check `/admin/sources` for its assigned
   account and ingestion controls. Assign it to a ready account and enable the
   intended catch-up/live/engagement controls.
4. **Channel paused.** Check `is_paused` on the channel row via
   `GET /api/v1/crawler/channels`. Resume with `POST /channels/{id}/resume`.
5. **Crawler manager not started.** The freshness endpoint only observes; it
   does not start the runtime. Re-read the "Starting the crawler runtime"
   section above.
6. **Empty channel.** If the Telegram channel genuinely had no new
   messages in the window, a stalled entry is the honest signal.

## Per-channel replay + repair

Use `/admin/telegram` for account login/policy and `/admin/sources` for source
assignment and ingestion-control edits. The commands below use `/api/v1/crawler/*` only for runtime inspection,
pause/resume automation, replay, and freshness checks. Pass the operator
token in the `X-Memexpert-Operator-Token` header.

```bash
export TOKEN="$PIPELINE_OPERATOR_TOKEN"
export BASE="http://127.0.0.1:8000"

# List Telegram sessions with owned channel counts.
curl -s "$BASE/api/v1/crawler/sessions" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq

# List tracked channels (filter by session + paused flag as needed).
curl -s "$BASE/api/v1/crawler/channels?session_name=<session-name>&include_paused=true" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq

# Pause one channel (idempotent).
curl -s -X POST "$BASE/api/v1/crawler/channels/<source_channel_id>/pause" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq

# Resume one channel (idempotent).
curl -s -X POST "$BASE/api/v1/crawler/channels/<source_channel_id>/resume" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq

# Replay one post by id WITHOUT advancing the channel checkpoint.
curl -s -X POST "$BASE/api/v1/crawler/channels/<source_channel_id>/replay-post" \
  -H "X-Memexpert-Operator-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"post_id": "12345"}' | jq

# Read the bounded freshness snapshot.
curl -s "$BASE/api/v1/crawler/freshness?limit_per_channel=100" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq
```

### Replay semantics

`replay-post` re-ingests one message through the full heavy chain. It
does **not** advance `last_read_post_id` on the channel row — operators
can safely replay ancient posts without disturbing the catch-up cursor.
Malformed or unknown post ids surface as HTTP 422 with the
`telegram_malformed_message` code; the runtime classifies them as
non-retryable so they do not poison the stage journal.

### Source engagement snapshots

When Telegram content is accepted, stable provenance is stored on
`meme_sources` and the first observed Telegram counters are stored in an
`ingest_initial` `meme_source_engagement_snapshots` row. Replays may refresh
that baseline for the same source/post slot, but volatile counters are never
written back to `meme_sources`.

Scheduled engagement refresh is split between PostgreSQL and RabbitMQ.
`meme_sources.next_engagement_check_at` is the durable DB schedule and lease
source; `memexpert-scheduler` claims due rows and writes
`source_engagement_capture_requested` messages through the transactional
outbox; worker-side RabbitMQ consumers fetch Telegram metadata and append or
update the scheduled snapshot.

The cadence is anchored to the Telegram post date (`published_at`), not to the
time MemeXpert first ingested the post: `+1h`, `+3h`, `+12h`, `+1d`, `+3d`,
`+7d`, `+1month`, then monthly. Historical public trends use `lag()` between
successful snapshots for the same `meme_source_id`, so a baseline or old post
with missed intervals contributes no invented delta.

Snapshot `NULL` values mean Telegram did not expose that counter; known zero is
stored as `0`. Public trend/search read models may coalesce unknown to zero for
ranking. Telegram `forward_count` is the public forward/repost counter and maps
to `latest_source_reposts`; it is separate from `forwarded_from_*` provenance on
forwarded messages.

## Flood-wait recovery

When Telegram returns a `FloodWaitError`, the Telethon adapter raises
`PipelineTelegramFloodWaitError` with the server-side cooldown in
`wait_seconds`. The operator-surface error handler translates that into:

- HTTP `503 Service Unavailable`
- `code=telegram_flood_wait`
- `Retry-After: <seconds>` header

The affected `telegram_sessions` row transitions into
`status=flood_wait` and `flood_wait_until` is set to
`now() + wait_seconds`. Only that session becomes non-runnable; healthy sessions
continue running. The affected session is not runnable again until it is
`active` and `flood_wait_until` is no longer in the future, and stale cached
clients/listeners should be closed by sending `SIGHUP` to
`memexpert-telegram-crawler` or by waiting for configuration reconciliation
after the account is repaired.

Recovery steps:

1. Check `/admin/telegram` or `GET /api/v1/crawler/sessions` and find the
   parked account.
2. Confirm `flood_wait_until` is in the future.
3. Wait it out. Do not try to bypass — Telegram will escalate the
   cooldown if the client keeps hitting it.
4. Once the window expires, restore the account to `active` and clear errors in
   `/admin/telegram`, or validate the account if authorized material changed.
5. The crawler observes the repaired account on its next configuration poll,
   closes stale clients/listeners, catches up, and rebuilds live listeners.
   Send `SIGHUP` only when an immediate reconcile is required.

If the flood-wait keeps recurring, lower the account's
`max_requests_per_second` in `/admin/telegram` Advanced settings (preferred).
That account policy change triggers reconciliation; use `SIGHUP` only to avoid
waiting for the interval. Changing the process-wide
`CRAWLER_MAX_REQUESTS_PER_SECOND` environment fallback requires restarting the
crawler service because settings are cached at process start.

## Session auth/ban recovery

Flood-wait, auth-required, and quarantine failures are session-scoped: the
affected session is marked non-runnable while healthy sessions continue. A
permanent ban surfaces as `PipelineTelegramSessionBannedError` →
`telegram_session_banned` / HTTP 503. The session row transitions into
`status=quarantined` and the runtime refuses to use it for any further work.
An unauthorized or revoked stored session surfaces as
`PipelineTelegramSessionAuthRequiredError` / `crawler_session_not_runnable`; the
row is marked `status=auth_required`, not quarantined. Recovery options are:

1. **Move affected sources** to another ready account in `/admin/sources`
   with "Move source". The operator-token `POST /channels/{id}/reassign`
   route still exists for headless scripts, but browser admin is preferred for
   human-driven assignment changes.
2. **Log in with valid Telegram credentials.** For a replacement account, create
   it through `/admin/telegram`, finish QR or phone-code login, then move sources
   to it from `/admin/sources`. To replace authorized material for an existing
   technical session name without a browser, use
   `scripts/auth_telegram_session.py --session-name <name> --string-session-file <path>`;
   the helper validates Telegram authorization, replaces `encrypted_string_session`,
   clears error fields, and sets `status='active'`.
3. **Validate and check recovery.** Use `/admin/telegram` to validate the
   account or `/admin/sources` to validate source access. The crawler reconciles
   the committed repair automatically; use `SIGHUP` only for an immediate pass,
   then read `/api/v1/crawler/freshness` to confirm live freshness recovers.

## Common failure modes

- **`telegram_flood_wait` cascade.** Every catch-up tick hits the same
  cooldown. Root cause is usually an oversized
  `catchup_message_limit` combined with a too-high per-account
  `max_requests_per_second`. Lower both in browser admin; the crawler
  automatically reconciles the account policy change.
- **`telegram_provider_unavailable`.** Telegram is reachable but refused
  the request. Transient; the runtime reports the stage as retryable and
  the worker will requeue it.
- **Network/provider blocked runtime.** If Telegram, Qdrant, Meilisearch,
  or the embedding/OCR provider is unreachable from local or staging, the
  correct result is failed or incomplete pipeline evidence in
  `pipeline_status` or per-target status fields. Do not mark provider calls as
  successful in fixtures to force a green run.
- **`telegram_malformed_message`.** Telethon returned a message shape
  the adapter cannot type-check. Non-retryable — the replay route will
  return 422 until Telegram fixes the underlying message. Do not
  replay; escalate if it reproduces across multiple posts.
- **Fresh session with empty catch-up.** A brand-new session that has not seen
  any messages yet will produce an empty snapshot. Wait for the live listener to
  accumulate posts before judging the SLO.
- **`crawler_session_not_runnable` for Telegram.** The underlying
   `telegram_sessions` row is missing, disabled, not `active`, auth-required, or
   lacks authorized material. Use `/admin/telegram` to log in and validate a
   ready account, or move the source to one in `/admin/sources`.
- **Channels without `published_at`.** Legacy `MemeSource` rows without
  `published_at` populated are invisible to the freshness query — they
  do not contribute to or block the SLO. This is deliberate; see
  `memexpert/services/crawler_freshness.py`.
- **Dead-letter inspection.** Crawler ingest failures are classified by
  the service layer and land in the same stage-journal surface the pipeline
  already expose. Use `GET /api/v1/pipeline/items/{id}/detail` to drill
  into a stuck item; use the per-target sync routes for search-index repair.

## Decision flowchart: "p95 breached the SLO"

When the freshness snapshot reports `slo_p95_pass=False`, work top-down:

1. **Check per-channel p95 in the snapshot.** If one channel dominates, focus
   there first. Channels with zero recent items should be chased via the session
   state surface.
2. **Is Telegram fetch the bottleneck?** Look at `memexpert-telegram-crawler`
   logs for flood-wait or provider-unavailable errors. If the listener is
   hitting the token-bucket ceiling, lower
   the source's `catchup_message_limit` or raise the assigned account's
   `max_requests_per_second` (but never both in the same run).
3. **Is the pipeline heavy chain slow?** Inspect item detail stage timestamps
   for sample items whose freshness is above p95.
4. **Is a sync target slow?** Compare Qdrant vs Meilisearch target timestamps in
   item detail and use `docs/ops/content-pipeline-search-sync.md` for replay +
   repair.
5. **Is the SLO too tight?** If the stack is healthy at every layer but
   the p95 still sits a few seconds above
   `crawler_freshness_slo_p95_seconds`, evaluate whether the threshold
   reflects the stack's real ceiling. Adjusting the threshold is a
   product decision — file a ticket before relaxing it.

## Known limitations

- **Catch-up is asynchronous and manager-driven** — browser admin and operator
  writes commit durable desired state but do not wait for Telegram. The manual
  older-history endpoint queues work and returns `202`; an idle crawler normally
  claims it within `CRAWLER_RECONCILE_INTERVAL_SECONDS`. Forward-control changes
  still converge on that cadence, and `SIGHUP` forces an immediate full
  reconcile. An in-flight pass can delay the next polling tick.
- **Reconciliation rebuilds all crawler sessions** — configuration changes are
  rare and currently trigger one full catch-up/listener rebuild. A future
  optimization may diff and restart only affected sessions when the channel
  fleet is large.
- **A later live-task failure needs an operator or control-state event** — a
  listener that exits after a fully applied reconcile is restarted by the next
  configuration change, `SIGHUP`, or crawler process restart; unchanged idle
  snapshot polls do not yet supervise completed listener tasks.
- **Paddle model cache is container-local** — replacing the worker removes
  `/app/.paddlex`; the first OCR attempt downloads the detector/recognizer models.
  Keep the crawler stopped during a deployment until an OCR canary repopulates
  the cache and completes within `PIPELINE_OCR_TIMEOUT_SECONDS`.
- **Freshness is measured on live items** — `MemeSource.published_at`
  must be set for the freshness query to score the item. Catch-up items
  backfilled from a historical window are NOT scored by the SLO; that
  is by design because their freshness is a function of the backfill
  job, not the live pipeline.
