# S04 Telegram Crawler + Freshness SLO Runbook

This runbook covers the operator proof loop for milestone **M002 / slice S04**:
the curated Telegram crawler chain (Telethon catch-up + live listener →
raw ingest accept → media-inspect materialization → transcode → ocr → embed →
classify → sync_qdrant + sync_meili) plus the freshness SLO proof harness
that measures end-to-end p50/p95 against the numbers configured in
`memexpert.core.config.Settings`.

S04 is additive to S02 + S03:

- `docs/ops/content-pipeline-smoke.md` — S01 upload/replay/duplicate proof.
- `docs/ops/content-pipeline-heavy-worker.md` — S02 heavy-worker proof.
- `docs/ops/content-pipeline-search-sync.md` — S03 per-target sync proof and
  smoke-proof surface.
- **this file** — S04 Telegram crawler proof, freshness SLO harness,
  per-channel replay + repair, flood-wait + ban recovery.

## Overview

S04 ships five operator-facing pieces:

1. **Telethon adapter** (`memexpert/crawlers/telegram/telethon_adapter.py`)
   bound to one curated session at a time. Flood-wait, session ban,
   provider-unavailable, and malformed-message failures all land in the
   typed crawler-error taxonomy.
2. **Crawler runtime** (`memexpert/crawlers/telegram/runtime.py`) driving
   catch-up and live-listener paths. Catch-up sweeps the configured
   message window; the live listener streams new messages into the
   ingest entrypoint.
3. **Operator crawler API surface** (`memexpert/api/routes/v1/crawler.py`)
   exposing `/api/v1/crawler/sessions`, `/channels`, `/pause`, `/resume`,
   `/reassign`, `/replay-post`, and `/freshness`.
4. **Freshness SLO proof harness** (`scripts/verify_s04_runtime.py`) that
   polls `/freshness` against a curated channel fixture and produces a
   pass/fail artifact pair under `.artifacts/s04-runtime-smoke/<run-id>/`.
5. **Curated channel fixture** at
   `memexpert/crawlers/telegram/channels.example.yaml` — the operator
   copies or edits this file to reflect the real `source_channels` rows
   seeded in the database.

The harness evaluates the freshness SLO numbers configured via
`Settings.crawler_freshness_slo_p50_seconds` (default **60s**) and
`Settings.crawler_freshness_slo_p95_seconds` (default **180s**). A run
passes iff the final snapshot proves `slo_p50_pass=True` AND
`slo_p95_pass=True` AND the observed item count reached
`--candidate-limit`.

## Prerequisites

- Docker Compose healthy: `IMGPROXY_PORT=18080 docker compose up -d`.
  Postgres, RabbitMQ, Qdrant, Meilisearch, and MinIO must report healthy
  before the harness runs.
- Alembic head applied: `uv run alembic upgrade head`.
- The native API running on `http://127.0.0.1:8000`: `uv run memexpert-api`.
- The S02/S03 heavy workers running: `uv run memexpert-workers`. These
  process both `media_inspect_requested` events from raw crawler accept and
  the later `transcode → ocr → embed → classify → sync_qdrant → sync_meili`
  chain that materialized crawler content feeds.
- Environment variables (all prefixed with `MEMEXPERT_` in `.env`):
  - `PIPELINE_OPERATOR_TOKEN` — same token the S02/S03 harnesses use.
    The S04 harness reads it from `get_settings()` when
    `--operator-token` is not passed on the CLI.
  - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — the Telethon app
    credentials. These are per-environment secrets; never commit them.
  - `TELEGRAM_SESSION_DIR` — defaults to `.telegram-sessions` in the
    repo root. The directory holds the `*.session` files Telethon
    persists; the `.telegram-sessions/.gitkeep` placeholder is tracked
    but every `*.session` file is excluded via `.gitignore`.
  - `CRAWLER_FRESHNESS_SLO_P50_SECONDS` / `CRAWLER_FRESHNESS_SLO_P95_SECONDS`
    — override the defaults when running against a slower or faster
    stack profile.
  - `CRAWLER_DEFAULT_CATCHUP_MESSAGE_LIMIT` — the default catch-up
    message window the runtime consumes when a channel row does not
    override it. Keep it bounded — the runtime will refuse to walk more
    than the configured limit per sweep.
  - `CRAWLER_MAX_REQUESTS_PER_SECOND` — token-bucket ceiling applied by
    the runtime to all Telethon reads. Lower this before raising
    `CRAWLER_DEFAULT_CATCHUP_MESSAGE_LIMIT`.
  - `CRAWLER_LIVE_MODE_ENABLED` — guards the live listener start-up. Set
    to `false` in environments where catch-up is the only sanctioned
    mode.

### Telethon session auth

Before the harness can prove freshness, the operator must authenticate
each Telethon session the fixture references. Use
`scripts/auth_telegram_session.py`:

```bash
uv run python scripts/auth_telegram_session.py \
  --session-name primary \
  --api-id $MEMEXPERT_TELEGRAM_API_ID \
  --api-hash $MEMEXPERT_TELEGRAM_API_HASH
```

The script performs the interactive Telegram login and writes
`.telegram-sessions/primary.session`. Repeat for every session name
listed in the fixture.

Inspect which sessions are currently known via the operator surface:

```bash
curl -s "http://127.0.0.1:8000/api/v1/crawler/sessions" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN" | jq
```

The response enumerates each `telegram_session_state` row with its
`status`, `last_heartbeat_at`, `flood_wait_until`, and
`owned_channel_count`.

## Seeding curated channels

**Known limitation (T03):** S04 does not ship an HTTP route to insert a
new `source_channels` row. Operators must seed curated channels via a
one-off SQL statement or an Alembic data migration. Wiring a proper seed
route is tracked as a follow-up for T05 / M003.

Copy-pasteable SQL template (run inside `psql` against the same database
the API uses):

```sql
INSERT INTO source_channels (
    id,
    platform,
    platform_id,
    username,
    title,
    is_active,
    is_paused,
    catchup_enabled,
    catchup_message_limit,
    session_id,
    created_at,
    updated_at
)
VALUES (
    gen_random_uuid(),
    'telegram',
    '@example_memes_en',        -- must match channel_fixture.platform_id
    'example_memes_en',
    'Example Memes (EN)',
    TRUE,
    FALSE,
    TRUE,
    500,
    'primary',                  -- must match telegram_session_state.session_name
    NOW(),
    NOW()
);
```

Then copy the example fixture and point the harness at your edited copy:

```bash
cp memexpert/crawlers/telegram/channels.example.yaml .artifacts/channels.yaml
# Edit .artifacts/channels.yaml to match the platform_id/title values you
# just inserted, then pass --channel-fixture-path .artifacts/channels.yaml
# to the harness.
```

The live harness refuses to run if any `platform_id` in the fixture does
not appear in the `GET /api/v1/crawler/channels` response. This is the
guardrail that turns "fixture/database drift" into a loud setup failure
instead of a silently-empty freshness snapshot.

## Starting the crawler runtime

**Known limitation (T04):** the crawler runtime is not wired into the
`memexpert-workers` console script yet. Operators drive it ad-hoc from a
Python shell until a dedicated entrypoint ships in T05 / M003.

Ad-hoc driver template:

```bash
uv run python - <<'PY'
import asyncio

from memexpert.core.config import get_settings
from memexpert.core.database import get_db_session_context
from memexpert.crawlers.telegram.runtime import TelegramCrawlerRuntime
from memexpert.crawlers.telegram.telethon_adapter import PipelineTelethonClient
from memexpert.ingest.crawler_service import PipelineCrawlerIngestService

async def main() -> None:
    settings = get_settings()
    async with get_db_session_context() as session:
        ingest_service = PipelineCrawlerIngestService.from_settings(session, settings=settings)
        telegram_client = PipelineTelethonClient(
            session_name="primary",
            api_id=int(settings.telegram_api_id),
            api_hash=settings.telegram_api_hash.get_secret_value(),
            session_dir=settings.telegram_session_dir,
        )
        runtime = TelegramCrawlerRuntime(
            ingest_service=ingest_service,
            telegram_client=telegram_client,
            session=session,
            settings=settings,
        )
        await runtime.catch_up_channel("primary", "@example_memes_en")
        await runtime.start_live_listener("primary")
        # Leave the listener running until the operator stops the script.
        await asyncio.Event().wait()

asyncio.run(main())
PY
```

Operators are expected to start this driver (or the S05 worker
entrypoint once it ships) **before** running the freshness harness. The
harness only observes freshness — it does not trigger catch-up itself.

## Running `scripts/verify_s04_runtime.py`

### Live mode

```bash
uv run python scripts/verify_s04_runtime.py \
  --api-base-url http://127.0.0.1:8000 \
  --channel-fixture-path .artifacts/channels.yaml \
  --candidate-limit 8 \
  --live-duration-seconds 120 \
  --artifacts-dir .artifacts/s04-runtime-smoke
```

What the harness does:

1. Health-checks the API.
2. Loads the curated channel fixture and refuses to run if any listed
   `platform_id` is missing from `GET /api/v1/crawler/channels`.
3. Calls `POST /api/v1/crawler/channels/{id}/resume` once per fixture
   channel so the operator log trail records "I asked for live mode" —
   `resume` is idempotent and is the closest supported write action T03
   exposes.
4. Polls `GET /api/v1/crawler/freshness` on `--poll-interval-seconds`
   until the combined time budget expires OR a snapshot already proves
   the SLO (both p50 + p95 pass AND observed item count >= candidate
   limit).
5. Aggregates the final snapshot into a
   `CrawlerS04RunSummary` and writes `report.json` + `report.md` under
   `<artifacts-dir>/<run-id>/`.

For staging, keep the same command shape but point `--api-base-url` at
the staging API and pass the staging operator token explicitly when the
local `.env` does not match that environment:

```bash
uv run python scripts/verify_s04_runtime.py \
  --api-base-url https://staging-api.example.invalid \
  --operator-token "$STAGING_PIPELINE_OPERATOR_TOKEN" \
  --channel-fixture-path .artifacts/staging-channels.yaml \
  --candidate-limit 8 \
  --artifacts-dir .artifacts/s04-runtime-smoke-staging
```

Do not use `--dry-run` as staging proof. Dry-run only proves the report
renderer; it does not contact Telegram, Qdrant, Meilisearch, or the API.

### Catch-up-only mode

```bash
uv run python scripts/verify_s04_runtime.py \
  --catch-up-only \
  --channel-fixture-path .artifacts/channels.yaml \
  --artifacts-dir .artifacts/s04-runtime-smoke
```

Catch-up-only mode skips the resume + live-duration polling loop. The
harness only takes one freshness snapshot after healthchecking the API,
then writes the artifact pair. Use this when the operator driver is
still running the catch-up sweep in another shell and you just want a
single observation window.

### Dry-run mode

```bash
uv run python scripts/verify_s04_runtime.py --dry-run \
  --dry-run-slo-scenario pass \
  --artifacts-dir .artifacts/s04-runtime-smoke-dry \
  --run-id operator-dry-run
```

Dry-run mode never touches the network, the database, or Telethon. It
runs the aggregation + rendering pipeline against a canned snapshot
keyed on `--dry-run-slo-scenario`:

| scenario | behavior |
|----------|----------|
| `pass` (default) | Three fixture channels each produce a sub-SLO item. Exit 0. |
| `fail-p50` | Freshness values sit between `slo_p50` and `slo_p95`. Exit 2. |
| `fail-p95` | Freshness values cross `slo_p95`. Exit 2. |
| `empty` | No items observed. Exit 2 (observed < bounded). |

Dry-run is what CI uses to confirm the harness, the schemas, and the
Markdown renderer all agree without standing up the stack.

### Exit codes

- **0** — every condition held: `slo_p50_pass=True` AND `slo_p95_pass=True`
  AND `observed_item_count >= bounded_item_count`.
- **1** — setup failure. The fixture was missing or malformed, the API
  was unreachable, channels were not seeded, or the operator token was
  absent. The stderr output carries an actionable hint.
- **2** — runtime failure. The snapshot was observed successfully but
  the SLO was breached or the corpus was under-sampled.

## Reading the report

Every run writes two files under `<run-id>/`:

- `report.json` — machine-readable `CrawlerS04RunSummary`.
- `report.md` — human-readable companion with the same data plus
  drill-down links into `/api/v1/pipeline/items/<id>/detail`.

Key JSON fields:

| field | meaning |
|-------|---------|
| `mode` | `live`, `catch_up_only`, or `dry_run`. |
| `run_id` | Operator-visible run identifier. |
| `bounded_item_count` | The `--candidate-limit` the operator requested. |
| `observed_item_count` | How many items in the snapshot reached both sync targets. |
| `p50_seconds` / `p95_seconds` | End-to-end freshness percentiles. |
| `slo_p50_seconds` / `slo_p95_seconds` | The configured SLO thresholds. |
| `slo_p50_pass` / `slo_p95_pass` | Whether the observed percentiles are inside the SLO. |
| `per_channel` | Per-channel roll-up with per-channel SLO pass flags. |
| `item_reports` | Per-item breakdown with pre-computed `slo_bucket`, `searchability`, current `pipeline_stage` / `pipeline_status`, and Qdrant/Meili status + failure reason fields. |
| `stalled_channels` | Expected channels that produced **zero** items. |
| `errors` | Harness-captured error strings (dry-run note, HTTP failures, etc.). |

### `slo_bucket` tags

Every item carries one of:

- `pass` — freshness inside `slo_p50`.
- `breached_p50` — between `slo_p50` and `slo_p95`.
- `breached_p95` — at or above `slo_p95`.
- `incomplete` — the sync chain never reached both targets (sample has
  `freshness_seconds=None`). Treat as "no data", not as a breach; read
  the same row's `searchability`, `pipeline_stage`, `qdrant_status`,
  `meili_status`, and reason/error fields to see whether the item is
  blocked, partially searchable, or still in flight.

### Per-item freshness evidence

The freshness endpoint and S04 report now carry enough per-item evidence
to choose the next diagnostic surface without guessing:

| field | meaning |
|-------|---------|
| `searchability` | `ready`, `partially_searchable`, `blocked`, or `in_flight` based on current stage + target truth. |
| `pipeline_stage` / `pipeline_status` | Furthest active or completed stage from `pipeline_stage_journal`. Failed rows include `failure_reason` and `failure_text`. |
| `qdrant_status` / `meili_status` | Per-target status, preferring `meme_file_sync_target_snapshots` and falling back to sync stage-journal rows. |
| `qdrant_reason` / `meili_reason` | Normalized provider or payload failure reason for the target, when known. |

If an item is `partially_searchable`, user search may work through one
target but the product promise is not fully proven. Use
`GET /api/v1/pipeline/items/<meme_file_id>/detail` for the full stage
history and `POST /api/v1/pipeline/search/smoke` to prove the user-facing
search path for that item.

### `stalled_channels`

A channel is stalled when it is listed in the fixture but produced zero
items in the snapshot window. Causes, in priority order:

1. **Session flood-wait or ban.** Check
   `GET /api/v1/crawler/sessions` for the owning session's `status` and
   `flood_wait_until`. If the session is parked, no channel bound to
   it can produce items.
2. **Channel paused.** Check `is_paused` on the channel row via
   `GET /api/v1/crawler/channels`. Resume with `POST /channels/{id}/resume`.
3. **Ad-hoc driver not started.** The T04 harness only observes; it
   does not start the runtime. Re-read the "Starting the crawler
   runtime" section above.
4. **Empty channel.** If the Telegram channel genuinely had no new
   messages in the window, a stalled entry is the honest signal.

## Per-channel replay + repair

All operator actions go through `/api/v1/crawler/*`. Pass the operator
token in the `X-Memexpert-Operator-Token` header.

```bash
export TOKEN="$MEMEXPERT_PIPELINE_OPERATOR_TOKEN"
export BASE="http://127.0.0.1:8000"

# List Telegram sessions with owned channel counts.
curl -s "$BASE/api/v1/crawler/sessions" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq

# List tracked channels (filter by session + paused flag as needed).
curl -s "$BASE/api/v1/crawler/channels?session_name=primary&include_paused=true" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq

# Pause one channel (idempotent).
curl -s -X POST "$BASE/api/v1/crawler/channels/<source_channel_id>/pause" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq

# Resume one channel (idempotent).
curl -s -X POST "$BASE/api/v1/crawler/channels/<source_channel_id>/resume" \
  -H "X-Memexpert-Operator-Token: $TOKEN" | jq

# Reassign one channel to a different Telethon session.
curl -s -X POST "$BASE/api/v1/crawler/channels/<source_channel_id>/reassign" \
  -H "X-Memexpert-Operator-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"new_session_name": "secondary"}' | jq

# Replay one post by id WITHOUT advancing the channel checkpoint.
curl -s -X POST "$BASE/api/v1/crawler/channels/<source_channel_id>/replay-post" \
  -H "X-Memexpert-Operator-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"post_id": "12345"}' | jq

# Read the bounded freshness snapshot the T04 harness consumes.
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

The affected `telegram_session_state` row transitions into
`status=flood_wait` and `flood_wait_until` is set to
`now() + wait_seconds`. The runtime refuses to start a listener or
catch-up sweep for a channel bound to a flood-waited session until the
clock advances past `flood_wait_until`.

Recovery steps:

1. `curl /api/v1/crawler/sessions` and find the parked session.
2. Confirm `flood_wait_until` is in the future.
3. Wait it out. Do not try to bypass — Telegram will escalate the
   cooldown if the client keeps hitting it.
4. Once the window expires, the runtime will reset the session to
   `active` on the next successful call. The T04 harness picks up the
   recovery automatically on the next poll.

If the flood-wait keeps recurring, lower `CRAWLER_MAX_REQUESTS_PER_SECOND`
and restart the ad-hoc driver.

## Session ban recovery

A permanent ban surfaces as `PipelineTelegramSessionBannedError` →
`telegram_session_banned` / HTTP 503. The session row transitions into
`status=banned` and the runtime refuses to use it for any further work.
The only recoveries are:

1. **Reassign affected channels** to another session via
   `POST /channels/{id}/reassign`. The reassign route explicitly
   distinguishes `crawler_invalid_session` (unknown target) from
   `crawler_session_not_runnable` (known target that is parked or
   banned), so operator tooling can pick the next healthy session.
2. **Re-authenticate** the banned session. Delete
   `.telegram-sessions/<session-name>.session`, rerun
   `scripts/auth_telegram_session.py --session-name <name>`, and
   reset the session row via SQL:
   ```sql
   UPDATE telegram_session_state
      SET status = 'active',
          flood_wait_until = NULL,
          last_ban_reason = NULL,
          last_heartbeat_at = NOW()
    WHERE session_name = '<session-name>';
   ```
3. **Re-run the harness** to confirm freshness recovers.

## Common failure modes

- **`telegram_flood_wait` cascade.** Every catch-up tick hits the same
  cooldown. Root cause is usually an oversized
  `catchup_message_limit` combined with a too-high
  `CRAWLER_MAX_REQUESTS_PER_SECOND`. Lower both and restart the driver.
- **`telegram_provider_unavailable`.** Telegram is reachable but refused
  the request. Transient; the runtime reports the stage as retryable and
  the worker will requeue it.
- **Network/provider blocked proof.** If Telegram, Qdrant, Meilisearch,
  or the embedding/OCR provider is unreachable from local or staging, the
  correct S04 result is setup/runtime failure with evidence in
  `errors`, `pipeline_status`, or per-target status fields. Do not mark
  provider calls as successful in fixtures to force a green run; use
  `--dry-run` only to validate report plumbing while waiting on network
  access.
- **`telegram_malformed_message`.** Telethon returned a message shape
  the adapter cannot type-check. Non-retryable — the replay route will
  return 422 until Telegram fixes the underlying message. Do not
  replay; escalate if it reproduces across multiple posts.
- **Fresh session with empty catch-up.** A brand-new session that has
  not seen any messages yet will produce an empty snapshot. Wait for the
  live listener to accumulate at least `--candidate-limit` posts, or
  lower `--candidate-limit` for the first proof run.
- **Channels without `published_at`.** Legacy `MemeSource` rows without
  `published_at` populated are invisible to the freshness query — they
  do not contribute to or block the SLO. This is deliberate; see
  `memexpert/services/crawler_freshness.py`.
- **Dead-letter inspection.** Crawler ingest failures are classified by
  the service layer and land in the same stage-journal surface S02/S03
  already expose. Use `GET /api/v1/pipeline/items/{id}/detail` to drill
  into a stuck item; the T03 smoke-proof route
  (`POST /api/v1/pipeline/search/smoke`) is still valid for items the
  crawler produced.

## `.artifacts/s04-runtime-smoke/` cleanup

```bash
rm -rf .artifacts/s04-runtime-smoke/
```

The harness uses `mkdir(parents=True, exist_ok=True)` so it can safely
write into a pre-existing tree, and each run lives under its own
`<run-id>` subdirectory. Delete the whole tree only when you want to
wipe historical proof runs — individual run directories are the audit
trail for T04 sign-off.

The directory is already listed in `.gitignore` so artifacts never leak
into a commit. Verify with `git status .artifacts/` before running the
harness in a dirty checkout.

## Decision flowchart: "p95 breached the SLO"

When the harness reports `slo_p95_pass=False`, work top-down:

1. **Check per-channel p95 in the report.** If one channel dominates,
   focus there first. Stalled channels show up in
   `stalled_channels` — chase those via the session state surface.
2. **Is Telegram fetch the bottleneck?** Look at the ad-hoc driver logs
   for flood-wait or provider-unavailable errors. If the listener is
   hitting the token-bucket ceiling, lower
   `CRAWLER_DEFAULT_CATCHUP_MESSAGE_LIMIT` or raise
   `CRAWLER_MAX_REQUESTS_PER_SECOND` (but never both in the same run).
3. **Is the pipeline heavy chain slow?** Re-run
   `scripts/verify_s02_runtime.py --dry-run`; if it reports stage
   timings well below the S02 expectations then the bottleneck is
   upstream (Telegram) or downstream (sync targets).
4. **Is a sync target slow?** Re-run
   `scripts/verify_s03_runtime.py` and inspect `qdrant` vs `meili`
   stage timing percentiles. If one target is consistently slower
   than the other, the S03 runbook's replay + repair loop applies
   verbatim.
5. **Is the SLO too tight?** If the stack is healthy at every layer but
   the p95 still sits a few seconds above
   `crawler_freshness_slo_p95_seconds`, evaluate whether the threshold
   reflects the stack's real ceiling. Adjusting the threshold is a
   product decision, not a harness decision — file a ticket before
   relaxing it.

## Known limitations

- **Single Telethon session** — S04 only runs one session at a time per
  ad-hoc driver invocation. Multi-session orchestration is tracked as a
  follow-up for T05 / M003. The operator API surface already lists every
  known session via `GET /api/v1/crawler/sessions`; the limitation is in
  the driver, not in the surface.
- **No runtime channel rebind without restart** — `POST /reassign`
  updates the `source_channels.session_id` column but the in-process
  listener does NOT pick up the change on the fly. Restart the ad-hoc
  driver (or the future worker entrypoint) to pick up a fresh binding.
- **Catch-up is worker-driven** — the operator API surface cannot
  trigger a catch-up sweep on demand. The T04 harness only observes the
  freshness the worker has already produced.
- **Freshness is measured on live items** — `MemeSource.published_at`
  must be set for the freshness query to score the item. Catch-up items
  backfilled from a historical window are NOT scored by the SLO; that
  is by design because their freshness is a function of the backfill
  job, not the live pipeline.
- **No seed API for `source_channels`** — operators insert rows via SQL
  or an Alembic data migration. T05 / M003 is expected to ship the seed
  route.
- **Worker entrypoint not yet wired** — `uv run memexpert-workers` does
  not start the crawler runtime today. Use the ad-hoc driver documented
  above until T05 / M003 closes the gap.
