# Content Pipeline Search-Sync Runbook

This runbook covers the dual-target search-sync chain
(`classify → sync_qdrant + sync_meili`) and the operator replay/repair APIs
for keeping Qdrant and Meilisearch aligned with PostgreSQL truth.

Use the item detail, per-target status, and replay routes below for production
diagnostics.

The repository now defines the `is_primary_file` payload and idempotent Qdrant
payload-index provisioning needed by `personalized_v2`. This runbook does not
assert that the live-beta payload has been backfilled, that its indexes have
been created, or that recommendation traffic has been activated. Treat those as
separate verified rollout steps.

## Prerequisites

- Docker Compose healthy: `IMGPROXY_PORT=18080 docker compose up -d`.
  Qdrant, Meilisearch, MinIO, Postgres, and RabbitMQ must all report healthy
  before runtime diagnostics are meaningful.
- Alembic head applied: `uv run alembic upgrade head`.
- The native API running on `http://127.0.0.1:8000`: `uv run memexpert-api`.
- The native workers running: `uv run memexpert-workers`. The workers process
  both `sync_qdrant` and `sync_meili` queues; check the logs for a
  `sync_qdrant consumer started` / `sync_meili consumer started` line.
- Environment variables:
  - `PIPELINE_OPERATOR_TOKEN` — required by the operator-token pipeline routes
    used in the commands below.
  - `PIPELINE_VOYAGE_API_KEY` — required for the real embed stage
    (sync_qdrant re-uses the cached embedding, so a missing key blocks the
    whole search-sync chain at the embed stage upstream).
  - `PIPELINE_CLASSIFICATION_API_URL` / `PIPELINE_CLASSIFICATION_API_KEY` —
    required for the classify stage (refer to
    `memexpert/core/classification.py`).
  - `PIPELINE_MEILISEARCH_INDEX_NAME` — defaults to
    `memexpert-memes`. Override when running against a scratch index.
  - `QDRANT_URL` — defaults to the local Qdrant docker port.
  - `MEILISEARCH_URL` / `MEILISEARCH_MASTER_KEY` — required for the real
    Meilisearch client.

If any search engine is unreachable, items will stall at the corresponding sync
stage and the per-target status route will show a failed or retryable target.
That is the truthful outcome.

## Indexed metadata contract

Every `sync_qdrant` and `sync_meili` attempt rebuilds its payload/document from
canonical PostgreSQL state at consume time. The broker event does **not** carry
visibility, collection, template, or popularity truth; the worker re-reads it
so replay and full rebuild paths always advertise the latest safe state.

Fields intentionally carried into both indexes:

- Access/ranking hints: `is_public`, `collection_ids`,
  `public_collection_ids`, `unlisted_collection_ids`,
  `private_collection_ids`, `shared_collection_ids`,
  `collection_owner_user_ids`, `collection_member_user_ids`.
- Ranking metadata: `search_index_algorithm_version`, `media_type`,
  `language`, `is_nsfw`, `tags`, `template_id`, `template_slug`,
  derived `popularity_score`, `like_count`, `created_at`, `updated_at`,
  `quality_score`.
- Safe content hints already used by the search sync path: `meme_id`,
  `meme_file_id`/`id`, `seo_page_slug`, and OCR text/snippet.

Qdrant additionally carries `is_primary_file` and internal
`uploader_user_ids`. `is_primary_file` is recomputed as
`Meme.primary_file_id == MemeFile.id` on every sync. Recommendation queries
require it to be true; ingestion deduplication and file-level search may still
inspect every file. `uploader_user_ids` exists only to constrain private
approximate-dedupe candidates to the same sole uploader; it is never an access
grant. Meilisearch carries neither field.

The normal Qdrant collection-ensure path idempotently requests these payload
indexes:

| type | fields |
|---|---|
| keyword | `search_index_algorithm_version`, `uploader_user_ids`, `media_type`, `language`, `tags`, `collection_ids`, `collection_owner_user_ids`, `collection_member_user_ids` |
| boolean | `is_public`, `is_primary_file`, `is_nsfw` |

Provisioning is safe to retry and accepts an already-existing index as the
desired state. It does not populate `is_primary_file` on old points; only a
normal resync/replay rebuilds that payload from PostgreSQL. The bounded
`search-index-sync` scheduler treats a synced Qdrant preview whose
`is_primary_file` value is missing or differs from the current primary file as
stale work, so it can drain this payload-contract backfill without an unrelated
meme update.

A long-lived sync process caches successful collection readiness. If an upsert
later receives `404` because the collection disappeared or was replaced, it
clears that cache, reruns collection and all payload-index provisioning, and
retries the upsert once. A second failure follows the normal durable sync-failure
path; it does not loop indefinitely.

Indexes remain **candidate sources only**. PostgreSQL is still the final access
authority in `MemeSearchService`; stale Qdrant or Meilisearch payloads must not
be treated as authorization.

User-facing search also applies a small conservative query-time prefilter before
candidate collection. The prefilter uses only safe index hints already stored in
Qdrant/Meilisearch: algorithm version, public/private/access hints,
collection ids, media type, language, NSFW flag, and tags. It is intentionally
best-effort:

- if the hint is clearly safe, the adapters pass it through;
- if the hint would risk hiding a legitimate result, the prefilter stays broad;
- PostgreSQL still performs the final visibility and access check after index
  candidate collection.

Meilisearch-specific requirement: the prefilter only works after the index has
its filterable attributes configured. `PipelineMeilisearchSyncClient.ensure_index`
now applies the required filterable fields before search traffic relies on them.
If operators recreate the index out-of-band, run the normal startup/index-ensure
path before expecting query-time filters to work.

Fields intentionally omitted from indexes:

- Collection invite tokens, hashes, recipient emails, and any other invite
  secret material.
- Collection membership roles and permission state; indexes only carry coarse
  owner/member id hints, and PostgreSQL resolves actual access.
- Raw auth/session material and any non-UUID user PII.
- SEO prose/modeling fields other than `seo_page_slug` (`page_title`,
  `meta_description`, `alt_text`, `caption`, `body_text`, SEO tag copies,
  `model_id`, `prompt_version`), moderation notes, and other
  operator/internal audit fields.

## Qdrant recommendation rollout and verification

The live beta's Nix/Quadlet source of truth pins validated Qdrant 1.18.3 by
immutable digest, never `latest`:

```text
docker.io/qdrant/qdrant@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286
```

Local and E2E Compose use the explicit `v1.18.3` tag. The production example and
beta Nix/Quadlet source use the immutable 1.18.3 digest above; beta omits
automatic image updates so upgrades require a deliberate deployment-source
change. The digest pin is present in source, but this document does not claim a
Reploy run or any application/index rollout has completed.

Before enabling `personalized_v2`, an authorized rollout must:

1. Confirm the running Qdrant reports 1.18.3 and the immutable digest expected
   by Nix.
2. Let the application collection-ensure path create the eleven payload indexes
   and verify their field names/types from Qdrant collection metadata.
3. Let the bounded `search-index-sync` scheduler drain stale/missing
   `is_primary_file` previews. Enumerate READY files from PostgreSQL and use
   authorized bounded Qdrant replay only for a verified residual/failure cohort.
4. Compare READY-primary PostgreSQL count with points matching
   `is_primary_file=true`, verify there is exactly one primary point per
   eligible meme, and sample public/NSFW/language/source-exclusion filters.
5. Verify recommendation reads return primary files while approximate dedupe can
   still find alternate files, then record counts and sampled parity without
   dumping vectors or sensitive payloads.

Steps 2–4 mutate live state. Do not run them, replay beta sync, or manually
create indexes without explicit authorization and normal deployment/migration
ordering. Read-only inspection comes first, and logs/chat must never contain
environment files, credentials, full container inspections, vectors, or tokens.

The Phase-3 stable-alias/named-vector migration is future-only. If offline and
memory gates justify it, the sequence is create a versioned collection and
indexes, dual-write, bounded PostgreSQL backfill, verify READY-primary coverage,
counts/dimensions/filters/ranking parity, atomically switch the alias, and retain
the old collection for rollback. Today's metadata repair remains bounded replay
into the configured single collection; do not infer an existing alias from
future design notes.

## Inspecting one item

Use the enriched item-detail route as the first diagnostic surface:

```bash
curl -s "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/detail" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN" | jq '.sync_targets'
```

The `sync_targets` object shows independent Qdrant and Meilisearch truth:
status, attempt count, last success/attempt timestamps, normalized reason,
error text, and the bounded preview of the last payload/document advertised to
that engine.

For a target-focused view, call the per-target status routes:

```bash
curl -s "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/qdrant" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN" | jq

curl -s "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/meili" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN" | jq
```

Healthy search-sync state means both targets are `synced`, have recent
`last_success_at` values for the current item, and their preview fields match
current PostgreSQL visibility/search truth.

## Replaying one target independently

Every per-target sync stage can be replayed without touching the other
target. The snapshot rows are independent; replaying Qdrant never rewrites
the Meilisearch snapshot and vice versa.

### Single-item replay

```bash
# Replay only Qdrant for one item:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/qdrant/replay" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN"

# Replay only Meilisearch for one item:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/meili/replay" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN"
```

### Bounded batch replay

```bash
# Qdrant batch:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/sync/qdrant/replay-batch" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"meme_file_ids": ["<id1>", "<id2>", "<id3>"]}'

# Meilisearch batch:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/sync/meili/replay-batch" \
  -H "X-Memexpert-Operator-Token: $PIPELINE_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"meme_file_ids": ["<id1>", "<id2>", "<id3>"]}'
```

The batch endpoints enforce the service-layer `SYNC_REPLAY_BATCH_MAX` cap.
Operators who need to replay more than that must split the work into
successive calls. The cap is deliberate — it prevents accidental
"requeue the entire corpus" operator errors.

These operator-token routes remain deliberately **failure-only** and bounded;
they are not the universal admin control plane. Cookie-authenticated admins use
`/admin/recovery` (**Replay & Repair**) for successful/forced replay,
stage-only versus cascading scope, version/audit/CSRF fencing, and an uncapped
all-matching selector. “Uncapped” there means one durable query job whose
scheduler materializes exact versioned rows in keyset pages under capacity
control—not one unbounded API request or broker publish burst.

## When to replay or rebuild

Use per-target replay when canonical PostgreSQL truth changed after the last
successful sync and the search indexes need to catch up. Typical triggers:

- `is_public` flipped between public/private.
- `visibility_mode` changed, or crawler provenance promoted an `auto` meme.
- The private meme's distinct uploader set changed, which makes Qdrant's
  internal approximate-dedupe payload stale.
- A meme was added to or removed from a collection.
- Collection visibility changed between `private`, `unlisted`, and `public`.
- Tags, template assignment, template slug, or SEO slug changed.
- `like_count`, derived source-engagement popularity, or `quality_score` changed
  enough to matter for ranking.
- `Meme.primary_file_id` changed, so both the new and former primary Qdrant
  points need rebuilt `is_primary_file` values.

Operational guidance:

1. Replay **only the affected target** when one engine is stale and the other
   is already correct.
2. Use the batch replay endpoints for many affected ids after a moderation,
   collection, template, or source-engagement/read-model repair.
3. For a full rebuild, enumerate every ready `meme_file_id` and feed the same
   per-target batch endpoints in bounded chunks until the whole corpus has been
   re-queued. The payload/document is rebuilt from PostgreSQL on every consume,
   so the rebuild does not depend on any stale snapshot JSON.
4. After the replay/rebuild wave, re-read the item detail and per-target status
   routes. A `synced` snapshot is trustworthy only when the target preview and
   timestamps line up with current PostgreSQL truth.

## Staged SHA reconciliation rollout

The provenance migration cannot restore global SHA uniqueness until historical
duplicates are merged. Use this deployment order:

1. Stop API, crawler, bot-upload, and worker ingestion writers.
2. Apply the additive revision only: `uv run alembic upgrade 0031`.
3. Run `uv run memexpert-reconcile-sha-duplicates`. It commits one SHA group per
   transaction and is safe to restart. Use `--limit <n>` for bounded batches.
4. Require `uv run memexpert-reconcile-sha-duplicates --verify-only` to print
   `no duplicate non-null SHA groups remain` and exit zero.
5. Apply `uv run alembic upgrade 0032`. The migration itself aborts if a
   duplicate remains; do not bypass that guard.
6. Read `meme_merge_logs.details` for the obsolete file IDs, S3 keys, Qdrant
   point IDs, and Meilisearch document IDs. Delete an S3 object only after a
   PostgreSQL reference check proves no surviving `MemeFile` uses it.
7. Delete the logged stale Qdrant points/Meilisearch documents, then perform a
   full rebuild from ready PostgreSQL files. The algorithm version changed to
   `collection-provenance-v2`, so a partial legacy payload population is not a
   valid end state.
8. Verify representative public, private, shared, and crawler-promoted memes
   through user-facing search/detail/media authorization before resuming
   ingestion.

The reconciliation merge log is the cleanup ledger. Do not infer obsolete S3
or index identifiers after deletion from current tables; those rows are already
gone by design.

## Common failure modes

- **Provider outage.** Qdrant or Meilisearch unavailability appears as a failed
  per-target snapshot with a replayable provider/timeout reason. The snapshot
  row stays truthful until the provider recovers; a single replay call should
  bring it back to `synced`.

- **Stale snapshots.** If the search engine was wiped out-of-band after a
  successful sync, the DB snapshot can still say `synced`. Replay the affected
  target to rewrite the document, then inspect the target directly and verify
  user-facing search behavior.

- **Malformed payloads.** `reason=malformed_response` is non-retryable. The
  stage row is marked non-retryable, the DLQ picks up the message, and the
  snapshot row stays in FAILED. Do NOT replay — the engine will reject the
  replay with the same malformed response. Fix the engine or the payload
  before replaying.

- **Sync queue backlog.** If items remain pending for both targets, check
  whether the worker is processing the `sync_qdrant` / `sync_meili` queues at
  all (`rabbitmqctl list_queues`) and whether Voyage embedding latency has
  pushed the upstream embed stage itself into timeout territory.
