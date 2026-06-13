# S03 Content Pipeline Search-Sync Runbook

This runbook covers the operator proof loop for milestone **M002 / slice S03**:
the dual-target search-sync chain (`classify → sync_qdrant + sync_meili`) plus
the stricter-than-diagnostic smoke proof that exercises both search engines
end-to-end against the live local stack.

S03 is additive to S02:

- `docs/ops/content-pipeline-smoke.md` — S01 upload/replay/duplicate proof.
- `docs/ops/content-pipeline-heavy-worker.md` — S02 heavy-worker proof
  (`transcode → ocr → embed → classify → meme_ready`).
- **this file** — S03 per-target sync proof plus the
  `POST /api/v1/pipeline/search/smoke` surface.

## Prerequisites

- Docker Compose healthy: `IMGPROXY_PORT=18080 docker compose up -d`.
  Qdrant, Meilisearch, MinIO, Postgres, and RabbitMQ must all report healthy
  before the harness runs.
- Alembic head applied: `uv run alembic upgrade head`.
- The native API running on `http://127.0.0.1:8000`: `uv run memexpert-api`.
- The native workers running: `uv run memexpert-workers`. The workers process
  both `sync_qdrant` and `sync_meili` queues; check the logs for a
  `sync_qdrant consumer started` / `sync_meili consumer started` line.
- Environment variables:
  - `MEMEXPERT_PIPELINE_OPERATOR_TOKEN` — read from
    `memexpert.core.config.get_settings` in the proof harness, so the same
    token the API accepts is used by both the harness and operator curl.
  - `MEMEXPERT_PIPELINE_VOYAGE_API_KEY` — required for the real embed stage
    (sync_qdrant re-uses the cached embedding, so a missing key blocks the
    whole S03 chain at the embed stage upstream).
  - `MEMEXPERT_PIPELINE_CLASSIFICATION_API_URL` / token — required for the
    classify stage (refer to `memexpert/core/classification.py`).
  - `MEMEXPERT_PIPELINE_MEILISEARCH_INDEX_NAME` — defaults to
    `memexpert-memes`. Override when running against a scratch index.
  - `MEMEXPERT_QDRANT_URL` — defaults to the local Qdrant docker port.
  - `MEMEXPERT_MEILISEARCH_URL` / `MEMEXPERT_MEILISEARCH_MASTER_KEY` —
    required for the real Meilisearch client.

If any search engine is unreachable the harness will still run, but items will
stall at the corresponding sync stage and the run summary will tag them as
`blocked_by_qdrant` or `blocked_by_meili`. That is the truthful outcome.

## Indexed metadata contract

Every `sync_qdrant` and `sync_meili` attempt rebuilds its payload/document from
canonical PostgreSQL state at consume time. The broker event does **not** carry
visibility, collection, template, or popularity truth; the worker re-reads it
so replay and full rebuild paths always advertise the latest safe state.

Fields intentionally carried into both indexes:

- Access/ranking hints: `is_public`, `author_user_id`, `collection_ids`,
  `public_collection_ids`, `unlisted_collection_ids`,
  `private_collection_ids`, `shared_collection_ids`,
  `collection_owner_user_ids`, `collection_member_user_ids`.
- Canonical ranking metadata: `search_index_algorithm_version`, `media_type`,
  `language`, `is_nsfw`, `tags`, `template_id`, `template_slug`,
  `popularity_score`, `like_count`, `created_at`, `updated_at`,
  `quality_score`.
- Safe content hints already used by the search sync path: `meme_id`,
  `meme_file_id`/`id`, `seo_page_slug`, and OCR text/snippet.

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

## Running the S03 proof harness

### Live mode

```bash
uv run python scripts/verify_s03_runtime.py \
  --dataset-root /home/alex/Documents/MemeDataset \
  --api-base-url http://127.0.0.1:8000 \
  --artifacts-dir .artifacts/s03-runtime-smoke
```

What the harness does:

1. Walks the dataset root deterministically and picks the first
   `--candidate-limit` supported files (default 8).
2. Uploads each file through the real operator route.
3. Polls `GET /api/v1/pipeline/items/{id}/detail` until **both** sync targets
   reach a terminal `synced` or `failed` status, or `--stage-timeout` seconds
   elapse (default 240).
4. For each item, calls `POST /api/v1/pipeline/search/smoke` with the
   `meme_file_id` and records the returned :class:`SmokeProofResult`.
5. Aggregates per-target sync counters, smoke-pass counters, and a stale
   snapshot list into a :class:`ContentPipelineS03RunSummary`.
6. Writes `.artifacts/s03-runtime-smoke/<run-id>/report.json` plus the
   human-readable Markdown companion at `report.md`.

### Dry-run mode

```bash
uv run python scripts/verify_s03_runtime.py --dry-run \
  --artifacts-dir .artifacts/s03-runtime-smoke-dry \
  --run-id operator-dry-run
```

Dry-run mode never touches the dataset, the API, Qdrant, or Meilisearch. It
produces a report with zero items so operators can validate that their local
copy of the harness, the schemas, and the markdown renderer all agree before
running the live path against the corpus.

### Exit codes

- **0** — every condition held: all bounded items dual-synced, all smoke proofs
  returned `both_targets_searchable=true`, and `stale_snapshot_ids` is empty.
- **1** — setup failure (dataset root missing, API unreachable, or another
  condition that blocked the harness before it could begin real work).
  The stderr output carries an actionable hint.
- **2** — runtime failure: at least one bounded item did not dual-sync OR
  failed its smoke proof OR the snapshot row claimed "both synced" while the
  smoke proof rejected it (the stale-snapshot signal).

## Reading `report.json` + `report.md`

Every run writes two files under the `<run-id>` directory:

- `report.json` — machine-readable :class:`ContentPipelineS03RunSummary`.
- `report.md` — table-formatted companion that renders the same data plus
  per-target replay drill-down links.

Key fields in the JSON:

| field | meaning |
|-------|---------|
| `bounded_item_count` | How many dataset files the harness pushed through. |
| `qdrant_synced_count` | Items whose Qdrant snapshot row is `synced`. |
| `meilisearch_synced_count` | Items whose Meilisearch snapshot row is `synced`. |
| `both_synced_count` | Items whose outcome resolved to `ready` (both targets OK). |
| `partial_count` | Items with exactly one target synced. |
| `blocked_by_qdrant_count` | Items blocked because Qdrant sync failed non-retryably. |
| `blocked_by_meili_count` | Items blocked because Meilisearch sync failed non-retryably. |
| `smoke_pass_count` | Items whose smoke proof returned `both_targets_searchable=true`. |
| `stale_snapshot_ids` | Items whose snapshot says "both synced" but whose smoke proof said otherwise — **operator triage required**. |

`stale_snapshot_ids` is the most important field on a passing run. When it is
empty, the snapshot row and the real search engines agree. When it is
non-empty, the pipeline persisted a "synced" status but the engine itself
cannot answer a lookup — either the document was deleted out-of-band, a
TTL expired, or a race between the sync worker and a replay left the snapshot
row stale.

### When `smoke_pass_count < both_synced_count`

This is the exact "stale snapshot" symptom. The fix sequence is:

1. Open `report.md` and scroll to the "Blocked items (per target)" section.
   Each row carries the failing target plus a replay drill-down link.
2. For each stale item, call the matching `/sync/<target>/replay` route (see
   below) to requeue the sync.
3. Wait for the worker to re-upsert the document, then re-run
   `verify_s03_runtime.py` — the new run should show the item in
   `smoke_pass_count` and drop it from `stale_snapshot_ids`.

If the stale set does not shrink after one replay round, check the worker
logs for the stage reason (`sync_qdrant_provider_blocked`,
`sync_meili_timeout`, etc.) and consult the "Common failure modes" section
below before replaying again.

## Replaying one target independently

Every per-target sync stage can be replayed without touching the other
target. The snapshot rows are independent; replaying Qdrant never rewrites
the Meilisearch snapshot and vice versa.

### Single-item replay

```bash
# Replay only Qdrant for one item:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/qdrant/replay" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN"

# Replay only Meilisearch for one item:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/items/<meme_file_id>/sync/meili/replay" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN"
```

### Bounded batch replay

```bash
# Qdrant batch:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/sync/qdrant/replay-batch" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"meme_file_ids": ["<id1>", "<id2>", "<id3>"]}'

# Meilisearch batch:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/sync/meili/replay-batch" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"meme_file_ids": ["<id1>", "<id2>", "<id3>"]}'
```

The batch endpoints enforce the service-layer `SYNC_REPLAY_BATCH_MAX` cap.
Operators who need to replay more than that must split the work into
successive calls. The cap is deliberate — it prevents accidental
"requeue the entire corpus" operator errors.

## When to replay or rebuild

Use per-target replay when canonical PostgreSQL truth changed after the last
successful sync and the search indexes need to catch up. Typical triggers:

- `is_public` flipped between public/private.
- A meme was added to or removed from a collection.
- Collection visibility changed between `private`, `unlisted`, and `public`.
- Tags, template assignment, template slug, or SEO slug changed.
- `like_count`, `popularity_score`, or `quality_score` changed enough to matter
  for ranking.

Operational guidance:

1. Replay **only the affected target** when one engine is stale and the other
   is already correct.
2. Use the batch replay endpoints for many affected ids after a moderation,
   collection, template, or popularity backfill.
3. For a full rebuild, enumerate every ready `meme_file_id` and feed the same
   per-target batch endpoints in bounded chunks until the whole corpus has been
   re-queued. The payload/document is rebuilt from PostgreSQL on every consume,
   so the rebuild does not depend on any stale snapshot JSON.
4. Re-run the smoke proof after the replay/rebuild wave. A `synced` snapshot is
   only trustworthy when the engine itself can still find the item.

## Running the smoke proof directly

```bash
# Prove one item by id:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/search/smoke" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"meme_file_id": "<meme_file_id>"}'

# Prove whichever item surfaces for a natural-language query:
curl -X POST "http://127.0.0.1:8000/api/v1/pipeline/search/smoke" \
  -H "X-Memexpert-Operator-Token: $MEMEXPERT_PIPELINE_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "crying cat drinking water"}'
```

Exactly one of `meme_file_id` and `query` must be present. When only `query`
is passed the route resolves a `meme_file_id` from the top Meilisearch hit
before running the per-item dual proof — so the response still carries a
single `SmokeProofResult` against a concrete item. A blank query, both
fields, or neither field all produce HTTP 422.

HTTP 200 is returned even when `both_targets_searchable=false`. Per-target
failure `reason` strings are DATA, not errors. The only non-200 responses are:

- **401** — operator token missing or wrong.
- **404** — `query`-only lookup returned no Meilisearch hit.
- **422** — request body validation failure.

## Interpreting `both_targets_searchable=false`

The response body carries a per-target breakdown under `targets`:

```json
{
  "meme_file_id": "...",
  "both_targets_searchable": false,
  "targets": [
    {
      "target": "qdrant",
      "searchable": false,
      "reason": "point_not_found",
      "matched_by": null,
      "latency_ms": 12.4
    },
    {
      "target": "meilisearch",
      "searchable": true,
      "reason": null,
      "matched_by": "both",
      "latency_ms": 8.1
    }
  ],
  "evaluated_at": "2026-04-11T10:20:30Z"
}
```

Decision flow:

1. **Find the failing target.** Scan `targets` for `searchable=false`.
2. **Read the `reason`.** The reason dictates the next action:
   - `point_not_found` / `document_not_found` — the sync snapshot row is
     stale. Call the per-target replay route for that target.
   - `query_miss` — the id-lookup succeeded but the re-query did not find
     the target in the top results. Double-check that the index analyzer is
     configured correctly and that the document is not buried by noisy hits;
     then replay.
   - `timeout` — transient. Re-run the smoke proof once; if it still
     times out, check the provider health and raise `--stage-timeout`.
   - `provider_blocked` — the adapter raised an unhandled SDK error. Check
     the worker logs plus provider health; replay once the provider is up.
   - `malformed_response` — the search engine returned a payload the
     pipeline cannot trust (typically a schema mismatch). File a ticket
     and do NOT replay until the engine is fixed — the stale row cannot
     be repaired by retrying a known-bad engine.
3. **Drill down.** Call the per-target GET status route:
   - `GET /api/v1/pipeline/items/<id>/sync/qdrant`
   - `GET /api/v1/pipeline/items/<id>/sync/meili`
   The response carries the full snapshot row plus the last payload preview.
4. **Replay the target** using the curl commands above.
5. **Re-run the smoke proof** against the same `meme_file_id`. Success
   drops the item from `stale_snapshot_ids` on the next full harness run.

## Wiping artifacts between runs

```bash
rm -rf .artifacts/s03-runtime-smoke/
```

The harness uses `mkdir(parents=True, exist_ok=True)` so it can write into a
pre-existing directory, but successive runs live under their own `<run-id>`
subdirectory. Delete the whole tree only when you want to clear historical
proof runs — do not delete individual run directories if you want to preserve
audit trail.

## Common failure modes

- **Provider outage.** Both the harness and the smoke proof route emit
  `reason=provider_blocked` when Qdrant or Meilisearch is unreachable. The
  snapshot row stays truthful (FAILED with a replayable reason) until the
  provider recovers; a single replay call will bring it back to SYNCED.

- **Stale snapshots.** The exact trigger for a non-empty `stale_snapshot_ids`
  is a successful snapshot row whose underlying document was deleted or
  never indexed (e.g. the Meilisearch index was wiped, a Qdrant collection
  was recreated, or a TTL policy removed the document). Replay the affected
  target to rewrite the document; re-run the smoke proof to confirm.

- **Malformed payloads.** `reason=malformed_response` is non-retryable. The
  stage row is marked non-retryable, the DLQ picks up the message, and the
  snapshot row stays in FAILED. Do NOT replay — the engine will reject the
  replay with the same malformed response. Fix the engine or the payload
  before replaying.

- **Query-only smoke proof with no hits.** When operators pass only `query`,
  the route returns HTTP 404 if Meilisearch has no hits. This is not a
  failure of the smoke proof — it means the query genuinely matches nothing
  in the index right now. Either widen the query or pass an explicit
  `meme_file_id` instead.

- **Harness polling timeout.** The default `--stage-timeout 240` is longer
  than the S02 heavy chain timeout because sync adds two stages. If a real
  run blows through it, check whether the worker is processing the
  `sync_qdrant` / `sync_meili` queues at all (`rabbitmqctl list_queues`) and
  whether Voyage embedding latency has pushed the upstream embed stage
  itself into timeout territory.
