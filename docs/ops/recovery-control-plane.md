# Replay & Repair control plane

This runbook covers the cookie-admin **Replay & Repair** workspace, deliberate
stage replay and derivative regeneration, bounded automatic retries, the
durable PostgreSQL dead-letter ledger, and the five isolated worker roles.
Application work can be replayed or repaired; there is no browser control for
restarting services.

## Operator workflow

1. Open `/admin/recovery` (**Replay & Repair**).
2. Use **Needs attention** for `dead_lettered`, `stuck`, `retryable`, or
   `blocked` work. Filter by kind, source, stage, or normalized reason; cursors
   retain the canonical observation snapshot.
3. Use **Regenerate** for deliberate successful/failed stage replay or media
   maintenance. **Outdated web videos** means profile differs from
   `web-h264-aac-1080p30-v2`, verification is missing, or source/output audio
   state is inconsistent. **Successful stage replay** requires exactly one
   non-Ingest stage; create separate jobs when multiple stages need review.
   Its all-matching roots are `succeeded` for Transcode/OCR/Embed/Classify or
   `synced` for the chosen Qdrant/Meilisearch target.
4. Review every backend-declared action and blocked prerequisite. Choose stage
   only or stage and dependents, retry limit 1/3/5 (default 3), warnings/risks,
   audit reason, and required acknowledgements. Never infer safety from an
   error string.
5. For selected rows, create a versioned preview. For **Select all matching**,
   submit the current action/filter/snapshot query once; do not page through or
   synthesize client-side IDs. The server atomically freezes root membership;
   later transitions become sanitized exclusions and newly eligible live rows
   do not enter that preview.
6. Follow the `preparing` job until its exact materialization completes. Review
   selected roots, expanded steps, grouped exclusions, and preview expiry, then
   schedule with one click.
7. Use **Jobs** for active/history reads, failed-first items, manual refresh,
   audited handoff, and **Preview retry of failed items**.

The scheduler executes non-Telegram recovery and resumable materialization every five seconds. The
`telegram` worker role executes source-post replay and resumes backfills. Job
detail shows preparation progress/exclusions and queued, dependency/capacity-
waiting, dispatched, succeeded, failed, stale, dependency-skipped, and cancelled
states. Active browser pages poll about every five seconds only while visible;
refresh failure keeps the last known good state.

## Eligibility, scope, and ownership

- Succeeded, retryable-failed, and terminal-failed stages may be replayed.
  Terminal override requires a reason and acknowledgement; retryable failures
  alone spend the selected budget.
- Pending/processing delivery, active replay reservation, missing original or
  prerequisite, duplicate canonical rows, and unsupported Ingest replay block
  admission. Wait for the current delivery or stuck-work reconciler.
- Original eligibility uses a bounded storage `HEAD`. Only a definitive 404,
  `NoSuchKey`, or `NotFound` response is treated as missing; authorization,
  timeout, endpoint, and unknown errors temporarily block admission as storage
  unavailable. They never exclude a query member or consume a retry budget.
- Stage-only leaves descendants untouched and therefore requires the stale-data
  warning/acknowledgement when declared. Transcode stage-only maps to atomic
  derivative regeneration.
- Cascades are Transcode → OCR → Embed → Classify → Qdrant + Meilisearch. The
  two search targets may run concurrently. Ordinary pipeline fan-out is
  suppressed for recovery-owned events.
- Parent failure, stale version, or cancellation marks undispatched descendants
  `skipped_dependency`. A READY file remains READY/catalog-visible throughout
  maintenance even when the repair fails.
- Scheduling atomically activates a database reservation for every execution
  item. Partial unique indexes fence both file/stage steps and non-stage work
  kind/ID targets across concurrent admin sessions; retryable redispatch keeps
  ownership, while cancellation or terminal reconciliation releases it.
- Cookie-admin `/actions` handles successful and forced replay with CSRF,
  idempotency, versions, and audit. Operator-token pipeline replay remains
  failure-only and must not be used to bypass these controls.

## Exact query preparation and cancellation

Storage `HEAD` requests run outside the materializer's job-row lock. If any
probe in a page is unavailable, the lease is released and the scheduler reports
no progress, leaving the cursor, counts, items, and exclusions unchanged for a
later turn. A definitive missing object is instead recorded as the sanitized
`missing_original` exclusion. A final worker `GET` that races with deletion is
terminal and budget-free; the worker acknowledges after durable failure/dead-
letter recording while derivative maintenance retains the active generation.

An all-matching job has no product-level item cap. The first leased scheduler
turn captures every matching root into the immutable snapshot-member ledger
under one PostgreSQL repeatable-read snapshot, then atomically records the
server-owned snapshot time. If that transaction does not commit, the snapshot
time remains null and the next lease recaptures cleanly. Later leased turns
keyset-page only those immutable members, revalidate their captured versions
and context, expand dependency steps idempotently, and record changed or
ineligible roots by sanitized reason. A restart resumes expansion from the
durable member cursor. `expires_at` remains null until exact completion.

For **Successful stage replay**, choose stage-only or cascade before creating
the query preview; the stage and scope cannot change after capture. Stage-only
leaves current descendants untouched and requires the declared stale-data
acknowledgement. Cascade expands the exact downstream graph and may add
provider, semantic-merge, or terminal-override risks declared by the backend.
Review the audit reason and 1/3/5 retry limit along with selected-root count,
expanded execution count, preparation progress, and grouped exclusions. A root
that changed version, stopped being `succeeded`/`synced`, lost its original or
prerequisite, or gained an active reservation is excluded; a row that became
successful only after snapshot capture is not added. Do not compensate for an
exclusion by manually appending an unreviewed live row to the job.

Cancellation enters `cancelling`: stop admitting roots/children, cancel queued
or waiting descendants, and continue reconciling dispatched work. Do not report
the job cancelled or use its totals for incident conclusions until it reaches a
terminal status. Never purge the broker to accelerate cancellation.

## Automatic versus admin-scheduled recovery

- RabbitMQ/provider work and transactional-outbox publication get at most five
  automatic attempts. An audited admin recovery can reset an exhausted outbox
  row to `pending` for one new publication attempt.
- Each Replay & Repair job independently selects 1, 3, or 5 retryable failures,
  default 3. A terminal failure stops immediately. Deployment drain/
  `worker_shutdown` redelivery resumes the same logical attempt and consumes no
  job budget.
- A newly encountered Telegram post gets three attempts. The older-history
  cursor does not advance past a retryable failure on attempts one and two. On
  attempt three only that post is quarantined, the cursor advances, and the
  page continues.
- A backfill processes one page of at most 100 messages per lease. Successful
  pages release the lease without starting a new logical attempt. Transient
  page/provider failures use exponential delay and stop after five logical
  attempts in a retryable `failed` state.
- Authentication, banned-account, missing-source, and disabled-workload
  failures stop immediately for operator correction.
- Failures that existed before migration `0034` are visible and retryable, but
  are never replayed merely because the deployment upgraded. An admin must
  schedule them.
- Stale `processing` stage/media-inspection leases, outbox publishing leases,
  and backfill leases are reclaimed automatically. Old `pending` rows are not
  replayed merely because queue age is high: their existing transactional
  outbox or durable RabbitMQ message remains the owner, preventing duplicate
  work during a legitimate backlog.
- Duplicate delivery of an event that is already processing or succeeded is
  acknowledged without running the provider stage again.
- Admin actions are for canonical failed work, not service-process control.

## Admission gates and provider circuits

`pipeline_capacity_states` is refreshed every 15 seconds. A stage closes to
historical/recovery admission when pending work reaches 1,000 or the oldest
pending row reaches one hour. It reopens only after both pending work is at or
below 500 and oldest age is at or below 15 minutes. This hysteresis protects
live ingestion from the backfill/recovery workload.

The durable provider circuits for OCR, Voyage/Qdrant enrichment,
classification, Qdrant sync, and Meilisearch sync open after three matching
transient failures. After the cooldown, one fenced half-open probe is admitted.
A success closes the circuit; a failed probe reopens it. RabbitMQ's five-attempt
budget remains the outer bound.

The production thresholds are explicit in the Whale Nix environment and may
be changed without a migration:

- `PIPELINE_CAPACITY_CLOSE_PENDING_COUNT`
- `PIPELINE_CAPACITY_REOPEN_PENDING_COUNT`
- `PIPELINE_CAPACITY_CLOSE_OLDEST_AGE_SECONDS`
- `PIPELINE_CAPACITY_REOPEN_OLDEST_AGE_SECONDS`
- `PIPELINE_CIRCUIT_FAILURE_THRESHOLD`
- `PIPELINE_CIRCUIT_COOLDOWN_SECONDS`

## Durable dead letters

Before acknowledging a final delivery, a worker upserts a sanitized record in
`pipeline_dead_letters`. Payload and header secrets are redacted, a stable
payload hash deduplicates redelivery, and a link to canonical ingest, stage, or
sync work is retained when it can be resolved. Publishing to the legacy
RabbitMQ DLQ remains best-effort during the transition; PostgreSQL is the
recovery source of truth.

A dead letter offers replay only while its linked canonical row is still in
the same failed event generation. Superseded or unresolvable rows remain
archive-only. Outbox recovery remains `dispatched` until the outbox publisher
records `published`; broker failure leaves the recovery and dead letter
unresolved instead of reporting a false success.

Import the pre-existing RabbitMQ DLQ once after migration `0034` and before
operators start resolving its historical rows:

```bash
memexpert-import-pipeline-dlq --limit 10000
```

The command passively opens the configured `pipeline.dlq`, writes each message
to PostgreSQL, and acknowledges it only after the ledger commit. It is safe to
run again: deduplication reconciles an already imported delivery.

## Web-video rollout and regeneration

Deploy migration, API, workers, and frontend together through normal CI/Reploy;
follow the workflow through the final deploy job. CI's real-stack proof first
generates and ingests an audible 24 FPS WebM/Opus source and a silent portrait
60 FPS WebM source through the real worker. It downloads each generated MP4
and poster, checks the active generation ledger and object pointer, and
independently FFprobes container, H.264/yuv420p profile/level, dimensions, FPS,
bitrate, and AAC-LC presence/absence. The audible result must remain 24 FPS with
audio; the silent result must become 30 FPS without audio. Repeat an audible
ingest in beta and confirm playback after **Unmute** before regeneration.

Then use **Outdated web videos**:

1. Materialize and schedule a 10-root canary. Confirm every old active pointer
   remained usable until its replacement verified and activated.
2. Materialize and schedule 100 roots. Measure end-to-end throughput, CPU,
   storage growth, failure distribution, live-ingest backlog, and worker health.
3. Replace the initial estimate with that measurement. Beta currently has about
   7.4k derivatives and 32 GB of originals; at one regeneration in flight the
   initial planning range is 24–36 hours because the profile can encode roughly
   twice as many frames at higher quality.
4. Only then create the uncapped all-matching job. Do not split it into browser
   pages or raise worker concurrency without the canary evidence.

Completion requires zero outdated matches, a sibling poster for every active
video, verified FPS/dimensions/bitrate/codecs/audio invariants, no unresolved
regeneration failures, successful audible playback after Unmute, and normal
live-ingest backlog/worker health. Superseded generations remain seven days;
run GC only through its recognized/unreferenced safety checks.

## Worker and health inspection on Whale

The deployable roles are `media`, `ocr`, `enrichment`, `sync`, and `telegram`.
The `all` role remains available for local development and rollback only. Each
long-running worker, scheduler, and crawler writes an atomic local heartbeat;
the container health command verifies readiness, process liveness, heartbeat
freshness, and operation deadlines:

```bash
memexpert-runtime-health
```

The crawler becomes ready after its live listeners are established. Initial
catch-up continues in the running process and does not hold deployment
activation open while thousands of historical media posts are downloaded.

Useful service checks on Whale:

```bash
systemctl status memexpert-worker-media memexpert-worker-ocr \
  memexpert-worker-enrichment memexpert-worker-sync memexpert-worker-telegram \
  memexpert-telegram-crawler memexpert-scheduler

journalctl -u memexpert-worker-ocr -u memexpert-telegram-crawler --since -30m
```

The role-specific PostgreSQL `application_name` and RabbitMQ
`x-memexpert-worker-role` consumer argument make ownership visible without
inspecting process internals.

## Graceful worker shutdown

Workers treat the first `SIGTERM` or `SIGINT` as a drain request. They set the
runtime health `lifecycle_state` to `draining`, stop RabbitMQ consumer intake,
wait for in-flight handlers under one application-wide deadline, and then close
the broker and shared dependencies. A second termination signal or the
application deadline cancels remaining handlers. Because consumers acknowledge
manually, a cancelled delivery still owned by its journal generation is
negatively acknowledged for immediate requeue. A delivery whose generation is
already complete or superseded is acknowledged instead, and closing the broker
channel is the fallback if the database-backed disposition fails.
Terminal and retry-exhausted failures still obey the record-before-ack rule:
cancellation cleanup persists/reconciles their PostgreSQL dead-letter row before
acknowledging the source delivery.

Whale deliberately nests the shutdown budgets:

| Layer | Timeout | Purpose |
| --- | ---: | --- |
| Worker application | 210 seconds | Stop intake and drain or cancel in-flight handlers |
| Quadlet/Podman `StopTimeout` | 240 seconds | Allow application cleanup before the container is killed |
| systemd `TimeoutStopSec` | 270 seconds | Allow Podman to finish container teardown |

Keep the invariant `210 < 240 < 270` when changing any of these values. Do not
raise only the application timeout past an outer supervisor deadline. The
application value is configured with
`PIPELINE_WORKER_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS` in Whale's Nix deployment.
Keep the RabbitMQ connection timeout below the application deadline as well.
Whale uses 10 seconds; an initial robust-connect attempt is allowed to settle
within that finite timeout rather than being cancelled before FastStream owns
the connection.

The runtime health file progresses through `lifecycle_state` values `starting`,
`running`, `draining`, and terminal `stopped`. `memexpert-runtime-health` must
remain successful while the process is alive and within its `draining`
deadline. Production configures `HealthOnFailure=kill`, so turning an
intentional drain into an unhealthy result would cause premature termination
and defeat manual-ack requeue guarantees.

During a deploy or manual `systemctl stop`, follow all worker roles and verify
that intake stops before the drain wait, the process exits before Podman's
240-second boundary, and no unit records exit status 137:

```bash
journalctl -fu 'memexpert-worker-*.service' -o short-iso

systemctl list-units --all 'memexpert-worker-*' --no-pager
journalctl -u 'memexpert-worker-*.service' --since '-15 minutes' \
  --no-pager -o short-iso
```

A clean stop logs this structured event sequence:

1. `worker_shutdown_requested`
2. `worker_shutdown_started`
3. `worker_consumers_quiesced`
4. `worker_shutdown_drain_completed`
5. `worker_shutdown_completed`

Workers configure INFO-level JSON logging to stdout, so these event names and
fields are directly searchable in journald.

`worker_shutdown_force_requested` means a second signal requested immediate
cancellation. `worker_shutdown_drain_timed_out` means the 210-second application
deadline cancelled remaining handlers. Either event requires checking that the
cancelled delivery was redelivered; its matching stage journal should briefly
show retryable reason `worker_shutdown` rather than remain stuck in
`processing`. The event should still be followed by
`worker_shutdown_completed` before the outer timeout. Filter a rollout directly
with:

```bash
journalctl -u 'memexpert-worker-*.service' --since '-15 minutes' \
  --no-pager -o cat | grep -E \
  'worker_shutdown_|worker_consumers_quiesced'
```

After the replacement workers become healthy, confirm RabbitMQ consumers are
owned by the expected roles and watch recovery/admin state for redelivered
work. A redelivery after a forced drain is expected; a delivery accepted after
the worker logged that intake stopped, a 137 exit, or a worker still stopping
after 240 seconds indicates a shutdown regression. Do not acknowledge or purge
the affected queue manually: allow the idempotent consumer and recovery
control plane to reconcile it.

## Deployment order

Reploy still submits one combined systemd restart transaction, but explicit
unit ordering keeps the old web tier available while workers drain. For a full
image rollout, systemd applies the dependency chain in these directions:

```text
stop:  workers -> frontend -> API -> migration
start: migration -> API -> frontend -> workers
```

Worker units have ordering-only `After=memexpert-frontend.service`; they do not
require the frontend to remain healthy. Do not replace the combined release
with separate worker and web Reploy calls: a new worker generation or migration
must not run against an arbitrarily old API/schema contract.

1. Deploy the current recovery/media-generation migration (`0042` after the
   existing `0034` control-plane base).
2. Start the API and scheduler.
3. Start the five worker roles and crawler; verify all runtime health checks.
4. Confirm one correctly owned RabbitMQ consumer for each pipeline queue.
5. Run the legacy DLQ importer.
6. Open Replay & Repair, verify historical/outdated counts and job pagination,
   then run the 10- and 100-root canaries before any all-matching job.

If a role split must be rolled back, stop the five role units and run one
`memexpert-workers --role all` process. Do not run `all` concurrently with the
role units because that creates competing consumers and defeats isolation.
