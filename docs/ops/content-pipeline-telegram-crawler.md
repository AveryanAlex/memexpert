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
3. **Browser-admin Telegram surface** (`/admin/telegram` plus
   `/api/v1/admin/telegram/*`) for cookie-authenticated session CRUD,
   StringSession import/validation, and channel assignment management.
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
- The SvelteKit frontend serving `/admin/telegram`. For local development,
  run the frontend from `frontend/` with `pnpm dev`; set `API_BASE_URL` when
  the API is not reachable at the frontend default.
- The heavy workers running: `uv run memexpert-workers`. These
  process both `media_inspect_requested` events from raw crawler accept and
  the later `transcode → ocr → embed → classify → sync_qdrant → sync_meili`
  chain that materialized crawler content feeds.
- A browser session cookie for a user with the durable admin flag. The
  `/api/v1/admin/telegram/*` routes use the normal cookie-authenticated admin
  guard; they do not accept the operator token.
- Environment variables loaded through the project `.env` / `Settings` surface:
  - `PIPELINE_OPERATOR_TOKEN` — required by the operator-token crawler and
    pipeline routes used in the commands below.
  - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — the Telethon app
    credentials used by browser-admin validation and the CLI helper. These are
    per-environment secrets; never commit them.
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

## Browser-admin Telegram workflow

Use `/admin/telegram` for routine Telegram session and source-channel
management. The page is backed by `/api/v1/admin/telegram/*` and forwards the
admin browser cookie from SvelteKit to the API.

### Sessions

Before the runtime can ingest Telegram content, create or import one DB-backed
Telegram session for each account the manager should run. In the
"Import or Create Session" panel:

- Create a metadata-only session by leaving `StringSession` blank; the row will
  exist without secret material and is not runnable until valid secret material
  is supplied.
- Import an already-authorized Telethon `StringSession` by pasting it into the
  form. If `Validate after save` is checked, browser admin validates the
  pasted session with Telegram before saving account projection fields.
- Keep `enabled`, catch-up, live, engagement, and max requests/sec aligned with
  the environment. A session can still be visible while disabled or parked.
- Use "Validate access" on an existing session to decrypt the stored secret,
  validate it with Telegram, and optionally check access to a selected source
  channel.
- Use "Patch policy/status" to change session status, crawler policy toggles,
  flood-wait/error fields, or max requests/sec. Clearing errors and setting the
  status back to `active` clears parked/quarantine timestamps.

Admin responses and the Svelte page never return or render raw secret material:
`StringSession` and `encrypted_string_session` are excluded from reads. The
session projection exposes only `has_string_session` so operators can see
whether a secret is stored.

The existing headless helper is still useful for ops imports or replacing the
secret for a known session name without a browser:

```bash
uv run python scripts/auth_telegram_session.py \
  --session-name primary \
  --display-name "Primary crawler" \
  --string-session-file /run/secrets/telegram_string_session
```

You can also provide the existing StringSession through `--string-session` or
`TELEGRAM_STRING_SESSION`. The helper never creates `.session` files and never
prints the StringSession value.

### Channels and assignment

Use the "Add Telegram Channel" panel instead of SQL data setup:

- Add a Telegram channel with its `platform_id`, title, optional username,
  optional subscriber count, and `catchup_message_limit`.
- Assign it directly to a selected DB-backed Telegram session, or choose the
  `Orphaned, non-indexable` assignment intentionally when no session should
  index the source yet.
- For assigned channels, set catch-up, live, and engagement toggles according
  to the run. The `catchup_message_limit` is the per-channel sweep bound; if it
  is too high, lower it before raising session or global request rate limits.
- Orphaned channels are visible in the orphaned group but are non-indexable.
  Browser admin and the API force catch-up, live, and engagement off while a
  channel is orphaned, and the manager skips orphaned channels for catch-up,
  live listening, and replay.
- Use "Assign or move" to attach a channel to a session. If the channel was
  orphaned, explicitly save channel controls after assignment when indexing
  should resume.
- Use "Orphan and disable indexing" to clear the session assignment and force
  catch-up, live, and engagement off.
- Use "Edit indexing controls" for per-channel catch-up/live/engagement
  toggles and catch-up limit. A channel is indexable only when it is assigned,
  active, not paused, and at least one crawler/indexing control is enabled.

Deleting a Telegram session requires pasting the exact session id into the
confirmation field. The delete action removes the `telegram_sessions` row and
its encrypted secret material, then explicitly orphans every assigned channel
and forces those channels non-indexable by disabling catch-up, live, and
engagement.

### Audit trail

Telegram admin writes insert rows into `telegram_admin_audit_logs`. The audit
row records the admin user id, action, affected Telegram session/source-channel
ids, before/after snapshots, and operator note when supplied. Session secret
material is not written to the audit snapshot; only `has_string_session` is
recorded.

### Browser admin vs crawler API

Use browser admin for CRUD and assignment: create/import/validate/patch/delete
sessions, add channels, move channels between sessions, orphan channels, and
edit indexing controls. Use the operator-token `/api/v1/crawler/*` endpoints
for runtime tasks: list the runtime projection, pause/resume channels, replay
one Telegram post, and read freshness snapshots. Browser admin is the preferred
surface for session and channel management.

## Starting the crawler runtime

Run the dedicated Telegram crawler process before expecting fresh crawler data:

```bash
uv run memexpert-telegram-crawler
```

The command resolves normal project settings, builds its own async database
engine/session factory, constructs `TelegramSessionManager`, runs
`catch_up_all()`, then starts live listeners with `start_live_all()`. It stays in
the foreground until stopped.

Signal behavior:

- `SIGHUP` calls `manager.reload()` and then continues waiting. Send this after
  browser-admin session/channel assignment or policy changes that should affect
  live listeners without a full restart.
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
- Orphaned channels remain visible in `/admin/telegram` and the crawler API, but
  they are non-indexable and are skipped by catch-up, live listening, and
  replay.
- The manager keeps one cached Telethon client and one rate limiter per runnable
  session. Sessions are loaded from encrypted DB `StringSession` material; no
  filesystem `.session` files are created or read.
- Flood-wait, auth-required, or quarantine state affects only the failing
  session. Healthy sessions continue catch-up/live work in the same manager
  process.
- Send `SIGHUP` after admin disable/delete/assignment changes to close stale
  clients/listeners and rebuild live listeners from current DB state.

Watch for these structured lifecycle events in crawler logs:

- `telegram_crawler_runtime_starting`, `telegram_crawler_catchup_completed`,
  `telegram_crawler_runtime_started`, and `telegram_crawler_runtime_stopped`.
- `telegram_crawler_reload_requested`, `telegram_crawler_reload_started`, and
  `telegram_crawler_reload_completed` when `SIGHUP` is received.
- `telegram_crawler_stop_requested` when `SIGINT` or `SIGTERM` is received.

The freshness endpoint only observes freshness - it does not trigger catch-up or
start the crawler runtime.

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

1. **Session flood-wait or ban.** Check
   `GET /api/v1/crawler/sessions` for the owning session's `status` and
   `flood_wait_until`. If the session is parked, no channel bound to
   it can produce items.
2. **Channel orphaned or non-indexable.** Check `/admin/telegram` for the
   assigned/orphaned group and channel controls. Assign the channel to a
   runnable session and enable the intended catch-up/live/engagement controls.
3. **Channel paused.** Check `is_paused` on the channel row via
   `GET /api/v1/crawler/channels`. Resume with `POST /channels/{id}/resume`.
4. **Crawler manager not started.** The freshness endpoint only observes; it
   does not start the runtime. Re-read the "Starting the crawler runtime"
   section above.
5. **Empty channel.** If the Telegram channel genuinely had no new
   messages in the window, a stalled entry is the honest signal.

## Per-channel replay + repair

Use `/admin/telegram` for session/channel CRUD, assignment, and indexing-control
edits. The commands below use `/api/v1/crawler/*` only for runtime inspection,
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
`memexpert-telegram-crawler`.

Recovery steps:

1. Check `/admin/telegram` or `GET /api/v1/crawler/sessions` and find the
   parked session.
2. Confirm `flood_wait_until` is in the future.
3. Wait it out. Do not try to bypass — Telegram will escalate the
   cooldown if the client keeps hitting it.
4. Once the window expires, patch the session back to `active` and clear errors
   in `/admin/telegram`, or validate access if secret material changed.
5. Send `SIGHUP` to `memexpert-telegram-crawler` so stale clients/listeners are
   closed and live listeners are rebuilt. Freshness snapshots pick up recovery
   on the next poll.

If the flood-wait keeps recurring, lower the session row's
`max_requests_per_second` in `/admin/telegram` (preferred) or the
`CRAWLER_MAX_REQUESTS_PER_SECOND` fallback, then send `SIGHUP` to reload the
crawler process.

## Session auth/ban recovery

Flood-wait, auth-required, and quarantine failures are session-scoped: the
affected session is marked non-runnable while healthy sessions continue. A
permanent ban surfaces as `PipelineTelegramSessionBannedError` →
`telegram_session_banned` / HTTP 503. The session row transitions into
`status=quarantined` and the runtime refuses to use it for any further work.
An unauthorized imported StringSession surfaces as
`PipelineTelegramSessionAuthRequiredError` / `crawler_session_not_runnable`; the
row is marked `status=auth_required`, not quarantined. Recovery options are:

1. **Move affected channels** to another healthy session in `/admin/telegram`
   with "Assign or move". The operator-token `POST /channels/{id}/reassign`
   route still exists for headless scripts, but browser admin is preferred for
   human-driven assignment changes.
2. **Import valid secret material.** For a replacement session, create/import it
   through `/admin/telegram` and move channels to it. To replace the secret for
   an existing session name without a browser, use
   `scripts/auth_telegram_session.py --session-name <name> --string-session-file <path>`;
   the helper validates Telegram authorization, replaces `encrypted_string_session`,
   clears error fields, and sets `status='active'`.
3. **Validate and check recovery.** Use `/admin/telegram` "Validate access"
   with an optional channel check, send `SIGHUP` to `memexpert-telegram-crawler`,
   then read `/api/v1/crawler/freshness` to confirm freshness recovers.

## Common failure modes

- **`telegram_flood_wait` cascade.** Every catch-up tick hits the same
  cooldown. Root cause is usually an oversized
  `catchup_message_limit` combined with a too-high
  `CRAWLER_MAX_REQUESTS_PER_SECOND`. Lower both and reload or invalidate the
  affected session.
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
- **`crawler_session_not_runnable` for Telegram.** The `telegram_sessions` row
  is missing, disabled, not `active`, auth-required, or lacks encrypted
  StringSession material. Use `/admin/telegram` to validate/import a runnable
  session or move the channel to one.
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
   `CRAWLER_DEFAULT_CATCHUP_MESSAGE_LIMIT` or raise
   `CRAWLER_MAX_REQUESTS_PER_SECOND` (but never both in the same run).
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

- **No automatic admin-to-runtime reload hook yet** — assigning or moving a
  channel in `/admin/telegram` updates `source_channels.telegram_session_id`, but
  a live crawler process only observes the new binding after `SIGHUP` or process
  restart.
- **Catch-up is manager-driven** — neither browser admin nor the operator API can
  trigger a catch-up sweep on demand. The freshness endpoint only observes the
  freshness the running manager has already produced.
- **Freshness is measured on live items** — `MemeSource.published_at`
  must be set for the freshness query to score the item. Catch-up items
  backfilled from a historical window are NOT scored by the SLO; that
  is by design because their freshness is a function of the backfill
  job, not the live pipeline.
