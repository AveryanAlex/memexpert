# Analytics

## Public Meme Analytics

### Per-Meme Analytics

Every public meme page has a source/activity summary plus an expandable
professional view. The public read surface is:

- `GET /api/v1/memes/{meme_id}/sources` — all Telegram
  `public_crawler` posts across every file, stable `snapshot_at` pagination,
  six supported sorts, safe channel/post links, availability, nullable latest
  counters, coverage, views-based rates, and audience-normalized metrics.
- `GET /api/v1/memes/{meme_id}/analytics?window=7d|30d|90d|all` — selected
  UTC-period totals, activity points, seven-day momentum, peak bucket, current
  favorites, an opening absolute source baseline as of the selected range start
  plus server-bucketed absolute source end states, source
  performance/coverage, known audience change, and separate exposure funnels.
- `GET /api/v1/memes/{meme_id}/popularity` remains compatible for the compact
  legacy popularity summary; professional UI must not present its opaque score
  as Recorded activity.
- Meme detail GETs are read-only. `POST /api/v1/memes/{meme_id}/view` records a
  visible detail visit with optional discovery attribution. The web page emits
  it once after hydration for each displayed meme ID, so source sort/page and
  analytics-range navigation cannot manufacture additional MemeExpert views.

Seven-, 30-, and 90-day activity points are explicit UTC days, independent of
the PostgreSQL session timezone. `all` uses adaptive
day/week/month buckets so recent history stays precise without making long
histories unbounded. Absolute source points use the same server-selected bucket
boundaries but emit only for buckets containing a real Telegram observation.
The API returns explicit `history_start_at`, `history_end_at`, `refreshed_at`,
granularity, and `insufficient_history`; the frontend does not re-bucket in
browser-local time or fabricate a line from fewer than two usable points.

**Recorded activity** is the unweighted sum of positive original-source view,
reaction, and repost increases plus MemeExpert views, sends, saves, and favorite
actions. It excludes comments, card impressions, inline results served,
downloads, and subscribers. For each source counter, activity advances only
above that counter's running observed high watermark: `100 → 90 → 100`
contributes zero. The separate observed Telegram series starts with
`opening_baseline`, the latest known absolute aggregate state as of the selected
range's `start_at`, then reports the aggregate end state of each server bucket
that contains one or more real captures. Multiple captures in a bucket collapse
to their final state, and the returned point is stamped at the latest real
`captured_at` represented in that bucket—not at an artificial bucket boundary
or at every raw capture time. Corrections can therefore decrease between
returned points, and a newly discovered post can introduce a baseline jump,
without inventing intermediate samples.

Download counts remain reportable as adjacent metrics, but contribute no
weight to public trend rankings or Meme of the Day scoring.

Telegram counters and subscriber counts are nullable, and all totals/rates
carry measured/eligible post coverage. Views-based reaction/comment/repost
rates use a ratio of sums over posts with that counter and positive views;
combined interaction rate requires all three interaction counters. Per-1,000-
subscriber views/interactions require a successful audience observation no
more than 48 hours before publication. Subscriber snapshots are not
backfilled, summed subscribers are not labeled reach, and no metric claims
unique viewers.

Web and Telegram-inline funnels are separate. `meme_exposures` stores one
privacy-bounded fact per `(meme_id, kind, exposure_key)` and first-observed stage
timestamps, including distinct inline-chosen and inline-sent stages. Web rates
match detail clicks/high-intent actions only to keyed web card exposures; inline
rates match chosen and sent outcomes only to keyed inline results. Unkeyed
legacy events may increase lower-confidence exposure totals but never a funnel
denominator or conversion. No public funnel groups by user, query, request,
chat, or time proximity.

### Template Analytics

Template pages show aggregate analytics: when the template first appeared, peak popularity, number of memes, current activity level, and trend history when enough snapshots exist. "Biography of a meme template."

### Tag/Theme Analytics

Tag pages show trend lines: popularity of cat memes over a year, seasonal patterns (New Year, September 1st).

### Trend Comparison

Compare multiple memes, templates, or tags on one chart. "Amogus vs Wise Oak vs Skibidi." Shareable — users can link to specific comparisons. Potential for viral sharing of comparison screenshots.

### Meme Timeline

Chronological browsing: "Top memes of January 2026," "Memes of 2025." Nostalgia + SEO + social sharing.

---

## Analytics & Metrics

### North Star Metric

**Memes sent via inline bot per week.**

### KPI Dashboard

| Metric | Target |
|--------|--------|
| Website daily visits | 10,000+ |
| Inline memes sent / week | 50,000+ |
| Bot DAU | 5,000+ |
| Organic traffic / month | 100,000+ sessions |
| Bot Retention D1 / D7 / D30 | >40% / >20% / >10% |
| Collections created | 1,000+ |
| Memes in database | 500,000+ |
| SEO pages generated | 100,000+ |
| Guest → Full conversion rate | >5% |

### Admin Analytics Workspace

`/admin/analytics` is a read-only operator workspace separate from the
actionable `/admin` overview. It provides Overview, Engagement, Audience, and
Content & Sources views with the same inclusive UTC date range and a matching
prior-period comparison. Operators can choose common ranges or a bounded custom
range (up to 366 days, with the last 30 days by default); dashboards resolve
and display the effective range rather than assuming the browser timezone.

- Overview combines catalog growth, first-party visits, active users,
  interactions/downloads, conversion, source activity, and discovery funnel.
- Engagement covers interaction trends/breakdowns, search volume, zero-result
  rate, latency, raw query exploration, and query-attributed meme outcomes.
- Audience covers guest/full account lifecycle, active-account mix, surface
  mix, and mature D1/D7/D30 cohorts.
- Content & Sources covers catalog/processing composition, source health, and
  snapshot-to-snapshot Telegram engagement deltas.

Telegram counters are cumulative but can temporarily move backwards. Admin
engagement deltas retain the last observed high watermark, so a sequence such
as `100 → 90 → 100` contributes zero rather than double-counting ten recovered
views or reactions.

Dashboard reads are bounded on-demand aggregates; they are not an external
traffic analytics integration and do not auto-poll. Raw search queries are
admin-only. Dashboard responses never expose a user ID, request ID, IP,
user-agent, or raw URL. Raw query text is returned only in a protected admin
list/detail response, never in an analytics route URL. Before materializing raw
events, the API rejects a range above its implementation safety ceiling and asks
the operator to choose a shorter period rather than risking API-process memory.

#### Discovery-funnel attribution

The Overview discovery funnel measures observed, request-attributed discovery,
not an inferred site-wide conversion path. Its search stages come from
non-empty initial `search_query` events; a result is classified from the
recorded result count. Detail clicks and downloads enter the funnel only when
their event carries the same `request_id` as a search event in the selected
reporting range. An interaction without that attribution remains available in
general engagement totals, but is not guessed into a search funnel or a
query-to-meme outcome.

#### Audience metric rules

`new_guests` and `new_full_accounts` are lifecycle metrics, while
`guest_to_full_conversions` is a separate conversion metric. A guest creation
is recorded as `auth_event: guest_created`; a lifecycle event with
`full_account_created` records a new full account, while a merge into an
already-existing full account does **not** increase `new_full_accounts`.
`guest_to_full_conversions` counts only transitions from a persistent guest,
whether they upgrade in place or merge, with durable merge records filling
telemetry gaps. For a historical UTC day without lifecycle telemetry, the
account's current derived type at its `User.created_at` date is a compatibility
fallback.

Active guest/full mix uses the strict event's `actor_account_type` snapshot at
the time of the event. Only legacy events missing that snapshot fall back to
the account's current durable type. A person can therefore legitimately appear
in both active-type counts in a range that spans their upgrade.

Retention cohorts use `guest_created.refs.source_user_id` as the immutable
cohort member identity. If that guest later merges into another account, the
event row's reassigned `user_id` becomes the activity identity used for
D1/D7/D30 checks while the original cohort date and membership remain stable.
Current `User.created_at` rows fill legacy and direct-full gaps, excluding only
the same source identities already represented by lifecycle telemetry.

### Events to Track

Event tracking is a product requirement because recommendations, ranking evaluation, and analytics depend on it. Events must preserve enough attribution to answer: "where did the user see this meme, why was it shown, what did they do next?"

Core events:

- **page_view:** one first-party consumer route category per browser pathname
  navigation; the payload accepts only an approved surface category, never a
  raw URL, route parameter, query string, referrer, IP, or user agent
- **search_query:** normalized non-empty initial query, source
  (inline/web/miniapp), request ID, result total, returned count, latency_ms,
  filters, and collection scope
- **meme_impression:** a web meme card reached at least 25% visibility; includes
  stable placement-scoped impression ID plus rank, surface, request, and
  algorithm/source attribution
- **inline_served:** one meme returned in a Telegram inline result page with
  its own stable exposure key; it is separate from web impressions
- **meme_view:** detail page opened or PM/detail view shown; web detail reads
  stay side-effect free and record this through the dedicated view action once
  per displayed meme visit
- **meme_detail_click:** user clicked from a feed/search/related block to a meme detail page
- **meme_send / inline_chosen / inline_sent**
- **meme_like / meme_save / meme_pin / meme_upload / meme_download / meme_share**
- **collection_action** with `action` in payload for create/invite/join/add/remove/bulk flows
- **meme_report**
- **auth_event / account_merge:** guest-created and guest-upgraded lifecycle
  milestones, plus durable guest-to-full merge attribution
- **miniapp_open**
- **channel_suggest:** user_id, channel_url
- **inline_viral_tracking:** group_id (hashed), unique users from same group over time

Current backend foundation decision: all strict interaction writes stay in the existing `analytics_events` table with a versioned payload envelope (`schema_version`, `actor_type`, `actor_account_type`, `surface`, `refs`, `properties`). Legacy names remain valid for compatibility, and recommendation/trend readers must accept both legacy flat `payload.meme_id` and strict `payload.refs.meme_id` during the transition, but new reusable writes should prefer the canonical event names above and must never store raw `group_id`, `chat_id`, tokens, authorization/cookie headers, request headers, IP addresses, or user agents.

Web search writes only after a non-empty first page succeeds. Its raw query and
request ID remain inside the durable event stream for admin aggregation and
click/download attribution; public and ordinary-user APIs never return them.

The admin query explorer groups normalized query text and supplies an opaque
64-character hexadecimal `query_key` for drill-down links. The key, reporting
dates, sort, and pagination may appear in the admin URL; the raw query itself
must never appear there. The detail endpoint resolves the key under the same
admin authorization and returns raw query text only in its protected response
body.

Required attribution fields where applicable:

- `surface`: `web_home`, `web_search`, `web_related`, `web_collection`, `web_profile`, `telegram_inline`, `telegram_pm`, `miniapp`
- `source_algorithm`: `search`, `similarity`, `tag_related`, `personalized`, `trending`, `motd`, `collection`, `fallback`
- `source_meme_id`: source meme when a result appears under related/similar memes
- `query`, `filters`, `collection_id`, `rank`, `score`, `score_components`, `reason`
- `request_id` and/or `impression_id` so later clicks/downloads can be tied back to the result that exposed the meme

### Recommendation Signals

Recommendation service consumes positive interaction history. Download, save/favorite, pin, send/chosen-inline are strong positives; detail view is medium; impression without click is stored for future ranking/evaluation but should be weak or neutral initially.

Admin dashboard interaction totals likewise exclude page views, searches,
impressions, inline-query/served exposure, and account lifecycle events; those
facts remain available in their dedicated visit, discovery, and audience
metrics.

### Viral Analytics

Track inline bot usage by chat group (hashed for privacy). Measure: when one user uses the bot in a group, how many others from the same group start using it within 7 days? This is the viral coefficient of the inline bot.
