# Deferred Features & Open Questions

## Deferred Features

Features planned but deferred from the initial production release:

- **Political content classification** — two-tier sensitivity system (`standard` / `sensitive`) for filtering politically sensitive content on the public website. Requires classifier model selection and threshold tuning. Until implemented, all content is treated as `standard`.

## Open Questions

- **Search ranking weights** — semantic (0.4) + text (0.3) + popularity (0.3) are initial values; to be A/B tested post-launch
- **Popularity snapshot granularity** — 6h vs daily vs adaptive, depending on observed traffic patterns
- **Guest account TTL** — cleanup unused guests after N days; optimal N to be determined
- **"Share to Telegram" implementation** — standard `https://t.me/share/url` vs Mini App deep link for richer experience
- **Trend comparison UI/UX** — design for the "Google Trends for memes" comparison tool
- **Reddit/VK crawler priority** — which platform to add next based on user demand
