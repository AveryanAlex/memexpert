# Policy & Admin

## Content Policy

### NSFW

Auto-classified. Filtered from search and feeds by default. Users enable in settings. No blur — simply filtered.

### Political Content (Deferred)

Planned two-tier sensitivity system (`standard` / `sensitive`) for filtering politically sensitive content on the public website. `sensitive` content would be hidden on the website but shown in the Telegram bot and Mini App. Implementation deferred until a reliable classifier is available — see [Deferred Features](10-deferred.md). Until then, all content is treated as `standard`.

### Copyright

Contact email for takedowns. Source attribution on every meme page.

### Dead Sources

Marked unavailable. Meme stays in database.

---

## Themed Telegram Channel Network

5–10 MemeXpert-owned channels by category (cats, wholesome, science, IT, student life, absurdist). Automated posting 2–4×/day. Selection: tag match + popularity + novelty. Engagement feeds back into channel's content algorithm (not global popularity score). Channel descriptions link to the bot.

---

## Admin Tools

The cookie-authenticated workspace requires both a full account and the durable
admin flag. Routine pages use **source** and **Telegram account**;
implementation-level session names, identifiers, checkpoints, and repair
controls are diagnostic rather than default operator language.

| Route | Purpose |
| --- | --- |
| `/admin` | Actionable overview only; no always-open CRUD forms. |
| `/admin/analytics` | Read-only analytics workspace: Overview, Engagement, Audience, and Content & Sources. |
| `/admin/sources` | Suggestions, public Telegram add flow, health, assignment, ingestion settings, pause/resume, and source removal. |
| `/admin/telegram` | Telegram account connection, validation, account policy, and disconnect. |
| `/admin/recovery` | **Replay & Repair**: attention queues, deliberate regeneration/replay, exact batch previews, active jobs, history, and failures. |
| `/admin/search/synonyms` | English and Russian synonym drafts, validation, publishing, revision restore, and Meilisearch sync status. |
| `/admin/moderation` | Report queue, safe preview, direct review, and recent decisions. |
| `/admin/moderation/patterns` | Specialist blocked perceptual-hash workspace. |
| `/admin/content/seo` | Paginated SEO review queue. `/admin/content` redirects here. |
| `/admin/content/templates` | Searchable template catalog and curator work. |
| `/admin/memes/[id]` | Media-first specialist review for a single meme. |

Desktop navigation is a sidebar; small screens expose the same workspace links
in a horizontally scrollable navigation strip. Every workspace keeps technical
diagnostics, advanced policy controls, and destructive actions behind native
disclosures.

### Analytics workspace

Analytics is intentionally separate from the actionable overview: it answers
“what is changing?” rather than “what needs attention right now?”. Every
analytics view shares configurable inclusive UTC calendar dates (common ranges
or a bounded custom range) and a same-length prior-period comparison. Overview
shows catalog, visits, activity, conversion, source, and discovery-funnel
trends; Engagement adds interaction/search breakdowns and raw-query drill-down;
Audience adds account mix and mature retention cohorts; Content & Sources adds
catalog, processing, source-health, and engagement-delta views.

Charts use adjacent summaries/tables, explicit loading/empty states, and
readable labels. Access remains restricted to full admin accounts. Query
exploration exposes raw query text only in protected admin response bodies and
never exposes a visitor, request identifier, raw URL, IP address, cookie, or
user-agent in a dashboard response. Its browser links use an opaque
`query_key`, plus range/sort/pagination controls, rather than raw query text.

The Content & Sources health breakdown reserves **orphaned** for a Telegram
source whose assigned Telegram account is missing. It is not a generic
"no-session" state: a future non-Telegram source with no Telegram session is
classified by its own freshness/health facts instead. The dashboard's
discovery funnel likewise reports only request-attributed detail clicks and
downloads, so it does not infer a search conversion when the interaction lacks
the matching search request ID.

### Actionable overview

`/admin` answers “What needs attention?” with links to the relevant workspace:
open moderation reports, sources, Telegram accounts, missing SEO, and uncurated
templates. Healthy/ready totals are context, not work queues. Source attention
counts only active, unpaused sources. A source with no successful fetch is
**waiting** for its first 15 minutes; afterward, an orphaned Telegram source or
a stale source needs attention. Paused and removed sources are intentionally
excluded. A Telegram account needs attention when it is disabled, lacks stored authorized material,
is not active, has a current flood-wait, or is quarantined. Orphaned and stale
are overlapping diagnostic subcounts, not buckets to add together: an old
never-fetched Telegram orphan counts in both while it contributes once to source
attention.

### Source management

- A routine add accepts exactly one **public** Telegram reference: `@handle`, a
  bare handle, or a one-path `t.me`/`telegram.me` URL. Invite links, private
  references, and non-Telegram links are rejected.
- The operator must select a specific ready Telegram account unless there is
  exactly one ready account, in which case it is selected by default. “Ready”
  means enabled, active, authorized, and outside flood-wait/quarantine. The API
  rechecks this policy; the browser selection is only a convenience.
- A successful reference add uses safe defaults: source assignment to that
  account plus catch-up, live collection, and engagement enabled; the first
  catch-up takes the latest 5,000 Telegram messages by default and is bounded in
  Advanced settings. The latest window is processed oldest-to-newest so the live
  high-water checkpoint remains monotonic.
- Public Telegram identity is the canonical lowercase username, not an opaque
  Telegram access hash or display title. Telegram handle renames are not tracked
  automatically; reconcile a rename as an operational exception before relying
  on the old source.
- Adding a Telegram suggestion by reference creates or reuses the canonical
  source and approves the matching pending suggestion atomically. A failed
  lookup leaves the suggestion pending; a retry converges on the same source
  rather than creating a duplicate.
- The Advanced manual fallback is only for a known canonical Telegram platform
  identifier. It creates an unassigned source with ingestion off until a ready
  account is explicitly assigned. Reddit and VK suggestions visibly remain
  unsupported and can be rejected, but cannot create inert crawler rows.
- The generic browser-admin `POST /api/v1/admin/source-channels` endpoint is
  likewise Telegram-only. The read/list model still carries a platform field so
  future crawler support can be added without a read-contract migration.

The source workspace uses one sortable operator table rather than a wall of
cards. It keeps every active, paused, orphaned, stale, unsupported, and removed
source visible, with columns for name/handle, plain health and assigned account,
latest upstream post time, last successful fetch, distinct catalog memes,
observed posts, subscribers, and routine actions. Latest post time is the newest
durable source-post publication timestamp; observed posts count the durable
message inventory; catalog memes count distinct canonical memes attached through
source provenance, so deduplicated reposts do not inflate the total. Missing
timestamps and subscriber counts sort last.

Pause/resume and the source-detail link remain inline. Diagnostics, ingestion,
assignment, validation, and removal live on the source detail page so the table
stays scannable while all operator controls remain available. Source identity
and management load independently from the fetched-message ledger, so a
temporary ledger failure does not hide pause, diagnostics, assignment, or
removal controls. The detail page lists every observed Telegram message,
including unsupported and failed fetches, and distinguishes fully indexed
(Qdrant and Meilisearch), partially indexed,
processing, failed, and not indexable states. A fixed observation snapshot keeps
pagination stable while new rows arrive. Operators can queue a bounded
older-history catch-up without moving the live high-water checkpoint; this
supports progressively indexing the rest of a large channel after its initial
window. Manual history work requires the initial window to have completed and
catch-up to remain enabled on both the source and assigned account.
Removing a source stops future crawling while preserving checkpoint and message
inventory history.

### Replay & Repair

Replay & Repair is the application-work control plane with three sections:

- **Needs attention** shows retryable, stuck, blocked, and dead-lettered work
  from source posts/backfills through ingest, processing, search sync, outbox,
  and dead letters.
- **Regenerate** handles deliberate stage replay and derivative maintenance.
  Its first cohort is every web video not on `web-h264-aac-1080p30-v2`, every
  unverified derivative, and every inconsistent source/output audio state. A
  separate **Successful stage replay** cohort selects exactly one non-Ingest
  stage and can preview every matching successful root: `succeeded` journal
  rows for Transcode/OCR/Embed/Classify and `synced` target rows for Qdrant or
  Meilisearch.
- **Jobs** shows exact preview preparation, active work, history, exclusions,
  and failures. All admins may inspect jobs and perform an audited handoff while
  the immutable original requester remains visible.

The backend declares every available action and every blocked prerequisite; the
browser never infers eligibility from an error string. Succeeded, retryable-
failed, and terminal-failed stages may be replayed. A terminal override requires
an audit reason and acknowledgement checkbox, not typed confirmation. Pending
or processing rows, missing originals/prerequisites, duplicate rows, unsupported
Ingest replay, and an active reservation remain blocked.

Pipeline-stage actions choose **Selected stage only** or **Stage and
dependents**. Stage-only intentionally leaves existing descendants untouched
and displays a stale-data warning; stage-only Transcode means atomic derivative
regeneration. A cascade follows Transcode → OCR → Embed → Classify, then lets
Qdrant and Meilisearch run concurrently. Provider-backed and semantic-merge
risks are shown before scheduling. Every job chooses 1, 3, or 5 retryable
failures, default 3; terminal failures stop immediately and worker-shutdown
redelivery does not spend that budget.

Explicit versioned references remain supported. **Select all matching** instead
stores the current action, filters, and snapshot as an uncapped server query.
One click immediately commits a `preparing` job. The first leased scheduler
turn freezes exact root membership under a server-owned MVCC snapshot; the
client observation timestamp remains context and is not treated as historical
row reconstruction. Later restart-safe keyset turns expand dependency steps,
revalidate captured versions, and record sanitized exclusions. Preview expiry
starts only after expansion finishes. Reviewed outdated-video previews schedule
with one click and no typed phrase. Successful-stage queries also freeze the
chosen stage and stage-only/cascade scope for the whole job; replaying another
stage requires a separate preview. Their review retains the selected 1/3/5
retry limit, audit reason, backend-declared provider/semantic risks, stale-data
or terminal acknowledgements, and exact selected-root versus expanded-step
counts. Roots that changed, lost eligibility/prerequisites, or acquired an
active reservation after capture appear only as grouped sanitized exclusions;
newly successful live rows never enter the reviewed snapshot.

Jobs expose selected roots, expanded execution steps, preparation progress,
grouped exclusions, and queued/waiting/dispatched/succeeded/failed/stale/
skipped/cancelled totals. Cancellation first enters `cancelling`, stops new
admission, cancels queued descendants, lets dispatched work reconcile, and only
then finalizes accurate totals. Failed items sort first and can seed a new
**Preview retry of failed items**. Recovery remains asynchronous and capacity-
aware and never restarts containers or systemd services from the product.
Historical failures remain visible without automatic replay; Telegram poison
posts remain isolated so one message cannot block later history.

### Search synonym management

`/admin/search/synonyms` manages separate English and Russian same-language
catalogs in a bulk newline/comma format. PostgreSQL is the source of truth.
Every locale has one mutable draft plus immutable published and archived
revisions; restoring history copies an old revision into the draft rather than
mutating history. Translation aliases remain a separate future catalog and are
not mixed into either locale.

Saving a draft normalizes and validates it without changing live search.
Publishing requires an effective Meilisearch map, rejects conflicting keys
across the two locale snapshots, and requires explicit confirmation when it
would remove a large share of active keys. Meilisearch's source-key token limit
is surfaced in validation: long phrases may remain useful targets, while a
group with no eligible key is inactive. All mutations require an operator
reason and idempotency request ID. Draft, publish, and restore mutations also
require the version displayed during review. Sync retry is intentionally
idempotent and accepts a stale monitoring-row version because scheduler health
checks advance that row independently of desired state.

Publishing only records durable desired state. The scheduler is the sole
Meilisearch settings writer and asynchronously replaces the complete combined
published map. The page exposes desired, applied, and observed hashes, the
provider task, a safe error, and an audited retry action. An empty effective map
is never submitted, so a missing or invalid publication cannot accidentally
clear live synonyms.

### Telegram accounts

QR sign-in is the primary connection flow, with phone sign-in as a disclosed
fallback. Account cards expose the account identity, readiness, source count,
and heartbeat before diagnostics. Raw encrypted credentials, StringSession
material, full phone numbers, passwords, and login attempt IDs are never shown
in UI or API reads. Advanced policy/repair and the destructive disconnect
control are separate disclosures. Disconnecting an account unassigns its
sources and disables their ingestion; reconnect or reassign them deliberately.

A connection starts as a provisional login attempt, not as a Telegram account.
Closing the dialog cancels the attempt best-effort, while server-side expiry is
authoritative if the browser disappears. Failed, cancelled, and expired
attempts must not leave an account card or a runnable crawler session. If
Telegram authorized a temporary credential before the attempt was abandoned,
the service revokes that credential with Telegram before discarding it; cleanup
that cannot finish immediately remains retryable by the scheduler.

Only a successful identity check promotes the encrypted credential into a
durable Telegram account. Promotion upserts by canonical Telegram account
identity: a first connection creates the account, while login for an identity
already in the catalog rotates that account's credential instead of creating a
duplicate. An explicit reconnect target may be updated only when the authorized
identity matches it. Promotion clears temporary login secrets and disconnects
the temporary client without logging it out, because logout would revoke the
credential just stored for the crawler.

### Moderation and content work

- The moderation queue renders an authorized preview even for hidden/private
  memes, links directly to the meme review route, and records decisions. The
  meme route puts preview and current state first, then a **Processing** panel
  for every attached file before reports and controls. The panel identifies the
  primary file and shows file/stage state, active profile, original/output
  dimensions and FPS, audio state, attempts, active job, and every backend-
  declared action without exposing storage object keys.
- Blocked patterns are pHash policy fingerprints. The specialist page lists
  active and inactive patterns first; raw hash/algorithm/tolerance editing and
  lifecycle/deletion controls are disclosed.
- SEO is a list-first queue of public safe memes. It uses `?page=` pagination
  with 25 rows per page; editing and regenerate-and-overwrite controls are
  disclosed per row. Templates are searchable/list-first, with create, edit,
  merge, and delete controls disclosed per template.

Private admin previews use authenticated media render URLs, never storage object
keys. A full account with the durable admin flag may render a private meme file
through that proxy; unrelated non-admin users remain unable to discover it. The
admin request remains a control-plane write and does not synchronously call
Telegram. An idle crawler polls for committed source/account policy changes at
the configured reconciliation cadence, rebuilds and confirms live subscriptions,
then performs bounded catch-up for the new durable state. An in-flight reconciliation
can delay the next poll.
