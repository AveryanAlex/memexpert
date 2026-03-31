# Deferred Features & Open Questions

## Deferred Features

Features planned but deferred from the initial production release:

- **Political content classification** — two-tier sensitivity system (`standard` / `sensitive`) for filtering politically sensitive content on the public website. Requires classifier model selection and threshold tuning. Until implemented, all content is treated as `standard`.
- **Search ranking weight tuning** — semantic (0.4) + text (0.3) + popularity (0.3) are initial hardcoded values. Proper tuning requires A/B testing infrastructure (experiment framework, metric collection, statistical significance tooling) and meaningful traffic volume. Deferred until post-launch traffic reaches sufficient scale.
- **Popularity & trending weight tuning** — baseline formulas for static popularity and trending score ship with initial hardcoded weights. Tuning deferred alongside search ranking weights.

## Open Questions

- **Popularity snapshot granularity** — 6h vs daily vs adaptive, depending on observed traffic patterns
- **Guest account TTL** — 90-day TTL, cleanup guests with no interactions in that period
- **"Share to Telegram" implementation** — standard `https://t.me/share/url` vs Mini App deep link for richer experience
- **Trend comparison UI/UX** — design for the "Google Trends for memes" comparison tool
- **Reddit/VK crawler priority** — which platform to add next based on user demand
