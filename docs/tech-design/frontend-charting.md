# Frontend Charting

## Decision

Use `layerchart` for analytics/trends visualizations in the SvelteKit frontend;
the implementation is pinned to `layerchart@1.0.13`.

Route chart rendering through a shared chart component layer built on LayerChart. Very small chart components can stay compact and non-interactive, but should still use the wrapper when they plot API data.

## Current frontend audit

Current chart-like components found under `frontend/src`:

| Component | Current usage | Current behavior | Recommendation |
| --- | --- | --- | --- |
| `src/lib/features/memes/MemeActivityCharts.svelte` | `src/lib/features/memes/MemeSourcesAndActivity.svelte` on public meme detail | Two LayerChart-backed UTC time-series surfaces. Recorded activity compares original-source and MemeExpert contributions as signals per day so adaptive day/week/month buckets share a meaningful visual scale; the exact table retains raw bucket totals and granularity. The absolute-source chart selects one of views/reactions/comments/reposts, uses the API's opening baseline as of the selected range start plus server-bucketed end states stamped at their last real capture times, and breaks the line across nullable points. Both charts retain tooltips, legends, insufficient-history states, and exact tables. | Keep activity and absolute counters separate. Never re-bucket in browser-local time, coalesce a nullable counter to zero, or connect a line across an unknown point. |
| `src/lib/features/trends/TrendSparkline.svelte` | `src/routes/memes/[id]/+page.svelte` public popularity card | LayerChart-backed compact sparkline over `PublicMemePopularityPointRead.popularity_score`; no axis, legend, tooltip, or point labels; screen-reader chart label and route-level empty state copy | Keep on the shared chart wrapper. It remains compact and non-interactive, but plotting/scales are handled by LayerChart instead of bespoke SVG coordinate math. |
| `src/lib/features/trends/TrendComparisonChart.svelte` | `src/routes/trends/compare/+page.svelte` comparison card | LayerChart-backed multi-series comparison through `$lib/ui/chart`; `ChartFrame` owns shell/empty state/sizing, LayerChart owns axes/grid/line/point/tooltip rendering, and a visible warm-token legend stays below the chart | Keep on the shared chart wrapper. Future work should prefer timestamp-aware x values once every comparison point has a reliable `observed_at`, while preserving the adjacent exact-value data table. |
| `src/lib/features/trends/TrendAggregateHistory.svelte` | Public tag/template landing pages through `TaxonomyLandingPage.svelte` | Timestamp-aware LayerChart line/point history for aggregate Recorded activity, with source/MemeExpert decomposition in tooltips and an adjacent exact-value table | Keep the chart API-bucketed and retain the honest fewer-than-two-point state. |
| `src/lib/features/admin/analytics/AnalyticsTimeSeriesChart.svelte` | Overview, Engagement, Audience, and Content & Sources admin analytics | Reusable bounded UTC multi-series line/point chart with loading/empty states, legend, tooltip, and exact table | Keep admin inputs aggregate-only and preserve the selected server reporting range. |
| `src/lib/features/admin/analytics/AnalyticsBreakdownChart.svelte` and `AnalyticsDonut.svelte` | Admin distribution/ranking and surface-mix cards | Shared-wrapper bar and pie/donut charts with readable summaries/tables | Keep category aggregation bounded and never make the visual the only exact representation. |

No chart feature directly authors bespoke `<svg>`, `<canvas>`, `<path>`,
`<polyline>`, `<line>`, or `<circle>` primitives; rendering stays behind
`$lib/ui/chart` and its LayerChart re-exports.

Non-SVG analytics-like UI that should remain intentionally custom:

- `/trends` ranked meme cards and tag/template summary links: these are lists/cards, not charts.
- `/trends/timeline` period cards and meme grids: chronological browsing UI, not a chart until/unless product adds histogram/calendar/heatmap views.
- `/trends/compare` data table: should remain a table next to the chart for accessibility and exact values.
- `AnalyticsFunnel.svelte`: the bounded admin discovery funnel remains semantic
  HTML because its stages and exact counts are clearer as labelled blocks than
  as a general-purpose plotting surface.

## Project needs

The implemented chart boundary and future analytics/trends work must continue
to support:

- per-meme popularity over time;
- per-meme Recorded activity split between original sources and MemeExpert;
- server-bucketed absolute Telegram views/reactions/comments/reposts with
  corrections;
- template/tag trends over time;
- multi-series trend comparison;
- timeline/top-period views;
- axes/ticks and date/value formatting;
- legends for multiple memes/tags/templates;
- tooltips/crosshair for exact values;
- responsive SSR-friendly SvelteKit rendering;
- Tailwind-compatible styling with MemeExpert tokens (`paper`, `cream`, `ink`, `line`, `soft`);
- accessible fallback text/data tables where visual charts are not enough;
- reasonable bundle cost for SEO/public pages.

The browser-admin analytics workspace uses the same boundary for its bounded
UTC series: line/area charts for activity and catalog/source trends,
stacked/horizontal bars for breakdowns and rankings, a small donut for surface
mix, and a labelled funnel for discovery. Each visual must retain an adjacent
summary/table, descriptive caption, responsive loading state, and an explicit
empty state; charts are never the only place an operator can read an exact
metric.

## Library evaluation

| Library | Fit | Strengths | Risks / trade-offs | Verdict |
| --- | --- | --- | --- | --- |
| `layerchart@1.0.13` | Best fit | Native Svelte components; documented Svelte 3-5 peer support; SVG/Canvas/HTML chart primitives; built-in axes, legends, annotations, tooltip/highlight/pan/zoom; docs include pinned component pages for `Axis` and `Tooltip`; active releases including 1.0.x fixes and 2.0 prereleases; MIT | Heavier dependency graph (npm reports 27 dependencies, many D3 packages); stable 1.0 docs still show Tailwind 3-style config snippets, so Tailwind 4 integration should be verified in the migration PR; 2.0 prerelease has better subpath/layer exports but should not be the first production pin | Select for analytics charts. Use stable 1.0.13 first, isolate imports behind MemeExpert chart wrappers, then revisit 2.x once stable. |
| `svelte-chartjs@4.0.1` + `chart.js@4.x` | Good fallback, weaker product fit | Explicit Svelte 5 peer support; Chart.js is mature/popular; axes, legends, tooltips, responsive charts; tree-shakable registration; low wrapper risk | Canvas renderer is less natural for Tailwind/token styling and SVG/SSR accessibility; exact visual customization often goes through Chart.js options/plugins rather than Svelte components; not ideal for SEO/shareable public trend pages where semantic SVG and adjacent markup matter | Do not choose as primary. Keep in mind if LayerChart bundle/compatibility fails or high-density datasets need canvas performance. |
| `@unovis/svelte@1.6.5` | Not acceptable as primary today | Modular, tree-shakable, polished docs/gallery; zero direct dependencies in the Svelte wrapper; active repo | npm peer dependency for `@unovis/svelte@1.6.5` lists Svelte `^3.48.0 || ^4.0.0`, not Svelte 5. Project is Svelte 5, so adopting it now would rely on unsupported compatibility despite current activity. | Reject for now; re-evaluate only after an official Svelte 5-compatible release. |
| `layercake@10.x` | Good low-level foundation, too low-level for this task | Svelte 5 support, SSR-friendly, headless responsive graphics framework, full control | It explicitly requires project-local chart components; that keeps much of the bespoke axis/tooltip/legend burden this task is trying to avoid. LayerChart is built on this ecosystem and gives higher-level chart pieces. | Do not choose directly unless LayerChart is too opinionated for a specific custom visualization. |

## Why LayerChart over bespoke SVG

The pre-wrapper comparison implementation demonstrated the failure mode:
coordinate transforms, axis lines, multi-series path generation, point hit
labels, and legend rendering all became local bespoke code. Current meme,
taxonomy, comparison, and admin analytics now reuse the wrapper for date axes,
tick formatting, tooltip behavior, line/point/bar/donut variants, and consistent
responsive sizing. Returning those concerns to feature-local SVG would recreate
inconsistent charts and accessibility gaps.

LayerChart gives us reusable primitives for the solved parts:

- `Axis` for real x/y axes, ticks, rules, grids, labels, time/log scales, and formatting.
- `Tooltip` with chart-aware modes such as `bisect-x`, `band`, `bounds`, `voronoi`, and `quadtree`.
- Legend/highlight support for multi-series charts.
- SVG-first composition that remains compatible with Tailwind class styling and semantic surrounding markup.
- A Svelte-native component model that fits the existing `src/lib/features/trends` + `src/lib/ui` architecture.

## Implementation status

1. `layerchart@1.0.13` is pinned in the frontend dependency and lock files.
2. `$lib/ui/chart` is the integration boundary for `ChartFrame`, selected
   LayerChart primitives, palette tokens, and shared loading/empty behavior.
3. Comparison, tag/template aggregate history, admin analytics, the compact
   popularity sparkline, and expanded per-meme analytics all use that boundary.
4. Per-meme activity and absolute-source charts use real UTC time axes and
   retain their exact API values in adjacent tables. The activity plot derives
   signals per day only as a visual normalization across adaptive buckets; it
   does not replace exact Recorded activity totals.
5. The absolute-source selector changes only the displayed counter. Nullable
   points remain unknown in the table and split the plotted line, while the
   opening baseline—the latest known aggregate state as of the selected range's
   `start_at`—stays visibly distinct from later server-bucketed end states.
6. Run `pnpm check`, `pnpm test`, and `pnpm build` after changing the wrapper or
   any chart contract, plus focused component/SSR coverage for null gaps,
   adaptive granularity, selector state, and exact tables.

## Wrapper conventions

`frontend/src/lib/ui/chart` is the LayerChart integration boundary. Future chart components should import `ChartFrame` plus selected `LayerChart*` re-exports from `$lib/ui/chart`; direct `layerchart` imports should stay centralized in that wrapper barrel unless a new primitive needs to be added there first.

`ChartFrame` owns the shared shell behavior for analytics charts:

- responsive plot sizing with `compact`, `default`, and `tall` sizes;
- loading and empty rendering that keeps chart surfaces stable;
- warm-token Tailwind hooks using `paper`, `soft`, `line`, `ink`, and `muted`, with `class`, `captionClass`, and `plotClass` overrides for composed chart components;
- visible or screen-reader-only caption text through `label`, `description`, and `showCaption`.

Keep exact values in adjacent tables or summaries when a visual chart is not sufficient for accessibility. Compact sparklines should use the shared wrapper when they render API data, while staying visually minimal unless they need axes, legends, tooltips, or multi-series behavior.

Admin analytics chart inputs are aggregate-only API DTOs. The frontend keeps
the selected UTC date range in the route URL and passes it unchanged to server
loads; chart components receive already-bucketed data and must not derive a
browser-local reporting window. Raw search query drill-down remains a table-led
admin disclosure, not a chart label that could leak visitor/request metadata.
Its route may carry only the opaque 64-character hexadecimal `query_key` (with
date, sort, and pagination controls), never raw query text. The selected raw
query is rendered only after the server-side admin load receives the protected
list/detail response; chart props and route URLs must not retain it.

## Per-meme backend/API chart contract

Public per-meme chart input is now available from
`GET /api/v1/memes/{meme_id}/analytics`. `activity_points` contains already
bucketed source/MemeExpert counts for 7/30/90/All. The chart derives
`recorded_activity / bucket_duration_days` for its adaptive signals-per-day
plot, using API `bucket_start`, `bucket_end`, and per-point granularity; its
exact table displays the unmodified counts.

`observed_source` contains `opening_baseline`, the latest known absolute
aggregate state as of the selected range's `start_at`, plus one aggregate end
state for every server-selected bucket containing at least one real Telegram
capture. Multiple captures in one bucket collapse to their final aggregate
state, and the returned point uses the latest real `captured_at` represented in
that bucket rather than an artificial bucket-end timestamp. The
views/reactions/comments/reposts selector plots one counter at a time on a UTC
time axis. A null selected counter stays unknown, retains its `Unknown` label in
the exact table, and breaks the line between surrounding known values.
Per-point coverage remains available in the DTO for sparse-data explanations.
The frontend must not re-bucket either series in browser-local time, turn
missing counters into zero, interpolate across unknown data, or hide a real
decrease after an upstream correction.

`insufficient_history` supports the page-level sparse-history notice, while
explicit period/history bounds and coverage remain available as context and
must not alter the plotted exact values. A line needs at least two usable points
for the selected projection. The legacy per-meme popularity sparkline remains a
compact compatibility view; professional activity and absolute cumulative
counters stay on separate chart surfaces rather than sharing an axis.

## Sources checked

- Project files: `frontend/src/lib/features/memes/MemeActivityCharts.svelte`,
  `frontend/src/lib/features/memes/meme-activity-chart.ts`,
  `frontend/src/lib/features/trends/{TrendSparkline,TrendComparisonChart,TrendAggregateHistory}.svelte`,
  `frontend/src/lib/features/admin/analytics/Analytics{TimeSeriesChart,BreakdownChart,Donut,Funnel}.svelte`,
  `frontend/src/lib/ui/chart`, public/admin routes using them,
  `frontend/src/lib/api/types.ts`, `frontend/AGENTS.md`,
  `docs/prd/08-analytics.md`, and `docs/prd/06-website.md`.
- LayerChart docs/repo/npm: `https://www.layerchart.com/`, `https://www.layerchart.com/getting-started`, `https://www.layerchart.com/docs/components/Axis`, `https://www.layerchart.com/docs/components/Tooltip`, `https://www.layerchart.com/changelog`, `https://github.com/techniq/layerchart`, `https://www.npmjs.com/package/layerchart`.
- Chart.js/svelte-chartjs docs/repo/npm: `https://www.chartjs.org/docs/latest/`, `https://saurav.tech/svelte-chartjs/`, `https://github.com/SauravKanchan/svelte-chartjs/releases`.
- Unovis docs/repo/npm: `https://unovis.dev/docs/quick-start`, `https://github.com/f5/unovis`, `https://www.npmjs.com/package/@unovis/svelte`.
- Layer Cake docs/repo: `https://layercake.graphics/`, `https://layercake.graphics/guide`, `https://github.com/mhkeller/layercake`.
