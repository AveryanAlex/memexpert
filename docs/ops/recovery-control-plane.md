# Failed-work recovery control plane

This runbook covers the browser-admin recovery workspace, bounded automatic
retries, the durable PostgreSQL dead-letter ledger, and the five isolated
worker roles. Recovery actions retry or resume application work; there is no
browser control for restarting services.

## Operator workflow

1. Open `/admin/recovery` and select one of the mutually exclusive buckets:
   `dead_lettered`, `stuck`, `retryable`, or `blocked`.
2. Filter by work kind, source, stage, or normalized reason. The result cursor
   is tied to a fixed observation timestamp so newly failing work does not move
   rows between pages.
3. Open the work detail and review the safe error, attempts, source, target,
   and backend-declared capabilities.
4. Enter an operational reason and schedule the offered action. Every mutation
   is request-idempotent, version-fenced, and written to
   `operational_audit_logs`.
5. For a large selection, create a bounded preview first. Preview rows expire
   after five minutes and must be explicitly scheduled. Queued, undispatched
   items may be cancelled.

The scheduler executes non-Telegram recovery every five seconds. The
`telegram` worker role executes source-post replay and resumes backfills. Job
detail shows queued, capacity-waiting, dispatched, succeeded, stale-skipped,
failed, and cancelled item states.

## Automatic versus admin-scheduled recovery

- RabbitMQ/provider work and transactional-outbox publication get at most five
  automatic attempts. An audited admin recovery can reset an exhausted outbox
  row to `pending` for one new publication attempt.
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

1. Deploy migration `0034`.
2. Start the API and scheduler.
3. Start the five worker roles and crawler; verify all runtime health checks.
4. Confirm one correctly owned RabbitMQ consumer for each pipeline queue.
5. Run the legacy DLQ importer.
6. Open `/admin/recovery`, verify historical counts, and schedule small recovery
   batches before larger ones.

If a role split must be rolled back, stop the five role units and run one
`memexpert-workers --role all` process. Do not run `all` concurrently with the
role units because that creates competing consumers and defeats isolation.
