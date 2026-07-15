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
| `/admin/recovery` | Failed/stuck/dead-lettered work, bounded batch preview, audited retry/resume, and job progress. |
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

Source cards show plain health, last fetched time, assigned account, and a link
to message indexing. The source detail page lists every observed Telegram
message, including unsupported and failed fetches, and distinguishes fully
indexed (Qdrant and Meilisearch), partially indexed, processing, failed, and not
indexable states. A fixed observation snapshot keeps pagination stable while
new rows arrive. Operators can queue a bounded older-history catch-up without
moving the live high-water checkpoint; this supports progressively indexing the
rest of a large channel after its initial window. Manual history work requires
the initial window to have completed and catch-up to remain enabled on both the
source and assigned account. Diagnostics exposes technical
identifiers/checkpoints; ingestion, assignment, and removal remain disclosed.
Removing a source stops future crawling while preserving checkpoint and message
inventory history.

### Failed-work recovery

The Recovery workspace is the application-work control plane. It shows
canonical failures from source posts and backfills through ingest, processing,
search sync, outbox publishing, and dead letters. The backend declares the
actions available for each row; the browser never invents retryability from an
error string. Every retry or resume requires an operator reason, an idempotency
request ID, and the version displayed during review. Large selections require a
short-lived preview and explicit scheduling.

Recovery is asynchronous and capacity-aware. Operators can inspect progress or
cancel undispatched batch items, but cannot restart containers or systemd
services from the product. Historical failures become visible after upgrades
without being replayed automatically. Telegram poison posts are isolated after
three attempts so one message cannot permanently block the rest of a channel's
history.

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
  meme route puts preview, current state, and open reports before metadata and
  merge/delete controls.
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
