# Analytics

## Public Meme Analytics

### Per-Meme Analytics

Every meme page shows a public popularity chart (sparkline or expandable). Data: popularity score over time, view count, impression count, download count, like count, source count.

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

### Events to Track

Event tracking is a product requirement because recommendations, ranking evaluation, and analytics depend on it. Events must preserve enough attribution to answer: "where did the user see this meme, why was it shown, what did they do next?"

Core events:

- **search_query:** query, source (inline/web/miniapp), user_id, result_count, latency_ms, filters, collection scope
- **meme_impression:** meme shown on screen/web feed or returned in a Telegram inline result; includes rank, surface, request/impression id, algorithm/source, score components
- **meme_view:** detail page opened or PM/detail view shown
- **meme_detail_click:** user clicked from a feed/search/related block to a meme detail page
- **meme_send / inline_chosen / inline_sent**
- **meme_like / meme_save / meme_pin / meme_upload / meme_download / meme_share**
- **collection_action** with `action` in payload for create/invite/join/add/remove/bulk flows
- **meme_report**
- **auth_event / account_merge**
- **miniapp_open**
- **channel_suggest:** user_id, channel_url
- **inline_viral_tracking:** group_id (hashed), unique users from same group over time

Current backend foundation decision: all strict interaction writes stay in the existing `analytics_events` table with a versioned payload envelope (`schema_version`, `actor_type`, `actor_account_type`, `surface`, `refs`, `properties`). Legacy names remain valid for compatibility, and recommendation/trend readers must accept both legacy flat `payload.meme_id` and strict `payload.refs.meme_id` during the transition, but new reusable writes should prefer the canonical event names above and must never store raw `group_id`, `chat_id`, tokens, authorization/cookie headers, request headers, IP addresses, or user agents.

Required attribution fields where applicable:

- `surface`: `web_home`, `web_search`, `web_related`, `web_collection`, `web_profile`, `telegram_inline`, `telegram_pm`, `miniapp`
- `source_algorithm`: `search`, `similarity`, `tag_related`, `personalized`, `trending`, `motd`, `collection`, `fallback`
- `source_meme_id`: source meme when a result appears under related/similar memes
- `query`, `filters`, `collection_id`, `rank`, `score`, `score_components`, `reason`
- `request_id` and/or `impression_id` so later clicks/downloads can be tied back to the result that exposed the meme

### Recommendation Signals

Recommendation service consumes positive interaction history. Download, save/favorite, pin, send/chosen-inline are strong positives; detail view is medium; impression without click is stored for future ranking/evaluation but should be weak or neutral initially.

### Viral Analytics

Track inline bot usage by chat group (hashed for privacy). Measure: when one user uses the bot in a group, how many others from the same group start using it within 7 days? This is the viral coefficient of the inline bot.
