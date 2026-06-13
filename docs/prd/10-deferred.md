# Deferred Features & Open Questions

## Deferred Features

Features planned but deferred from the initial production release:

- **Political content classification** — two-tier sensitivity system (`standard` / `sensitive`) for filtering politically sensitive content on the public website. Requires classifier model selection and threshold tuning. Until implemented, all content is treated as `standard`.
- **Advertising slots** — Yandex Direct / AdSense or other meme-grid ad cards. Layout can leave room for future insertion, but no ad provider integration is required for MVP.
- **User-initiated account deletion and data export** — profile UI/API for deletion grace periods and JSON archive export. Schema placeholders may exist, but launch does not require the end-user flow.
- **Formal ranking/recommendation experimentation** — search/recommendation weights must be configurable and logged in MVP, but A/B testing infrastructure, statistical tuning, and learned ranking are deferred until meaningful traffic exists.
- **Meme editor** — browser-based template editor remains V2.
- **Additional crawlers** — Reddit/VK/Twitter/X ingestion after Telegram-first launch unless demand changes the priority.
- **Prometheus-compatible metrics endpoint** — deferred in favor of a later OpenTelemetry-based observability pass.

## Open Questions

- **Popularity snapshot granularity** — 6h vs daily vs adaptive, depending on observed traffic patterns and materialized-view refresh cost.
- **"Share to Telegram" implementation** — standard `https://t.me/share/url` vs Mini App deep link for richer experience.
- **Trend comparison UI/UX** — design for the "Google Trends for memes" comparison tool.
- **Reddit/VK crawler priority** — which platform to add next based on user demand.

## Explicit Non-Goal

Guest accounts are intentionally retained for personalization and conversion. Do not add guest TTL/deletion jobs unless this product decision changes.
