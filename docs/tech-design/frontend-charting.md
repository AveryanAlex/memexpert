# Frontend Charting Recommendation

## Decision

Use `layerchart` for analytics/trends visualizations in the SvelteKit frontend, pinned initially to `layerchart@1.0.13`.

Route chart rendering through a shared chart component layer built on LayerChart. Very small chart components can stay compact and non-interactive, but should still use the wrapper when they plot API data.

## Current frontend audit

Current chart-like components found under `frontend/src`:

| Component | Current usage | Current behavior | Recommendation |
| --- | --- | --- | --- |
| `src/lib/features/trends/TrendSparkline.svelte` | `src/routes/memes/[id]/+page.svelte` public popularity card | LayerChart-backed compact sparkline over `PublicMemePopularityPointRead.popularity_score`; no axis, legend, tooltip, or point labels; screen-reader chart label and route-level empty state copy | Keep on the shared chart wrapper. It remains compact and non-interactive, but plotting/scales are handled by LayerChart instead of bespoke SVG coordinate math. |
| `src/lib/features/trends/TrendComparisonChart.svelte` | `src/routes/trends/compare/+page.svelte` comparison card | LayerChart-backed multi-series comparison through `$lib/ui/chart`; `ChartFrame` owns shell/empty state/sizing, LayerChart owns axes/grid/line/point/tooltip rendering, and a visible warm-token legend stays below the chart | Keep on the shared chart wrapper. Future work should prefer timestamp-aware x values once every comparison point has a reliable `observed_at`, while preserving the adjacent exact-value data table. |

No other `<svg>`, `<canvas>`, `<path>`, `<polyline>`, `<line>`, or `<circle>` chart-like Svelte components were found in `frontend/src`.

Non-SVG analytics-like UI that should remain intentionally custom:

- `/trends` ranked meme cards and tag/template summary links: these are lists/cards, not charts.
- `/trends/timeline` period cards and meme grids: chronological browsing UI, not a chart until/unless product adds histogram/calendar/heatmap views.
- `/trends/compare` data table: should remain a table next to the chart for accessibility and exact values.

## Project needs

From the PRD and current routes, planned analytics/trends UI needs:

- per-meme popularity over time;
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

The current `TrendComparisonChart.svelte` already contains the failure mode: coordinate transforms, axis lines, multi-series path generation, point hit labels, and legend rendering are all local bespoke code. Planned analytics will add date axes, tick formatting, tooltip behavior, crosshair/highlight behavior, stacked/area/bar variants, and consistent responsive sizing. Repeating that by hand will produce inconsistent charts and accessibility gaps.

LayerChart gives us reusable primitives for the solved parts:

- `Axis` for real x/y axes, ticks, rules, grids, labels, time/log scales, and formatting.
- `Tooltip` with chart-aware modes such as `bisect-x`, `band`, `bounds`, `voronoi`, and `quadtree`.
- Legend/highlight support for multi-series charts.
- SVG-first composition that remains compatible with Tailwind class styling and semantic surrounding markup.
- A Svelte-native component model that fits the existing `src/lib/features/trends` + `src/lib/ui` architecture.

## Migration plan

1. Add `layerchart@1.0.13` as a frontend dependency in a focused implementation task.
2. Create a small MemeExpert chart wrapper under `frontend/src/lib/ui/chart` or `frontend/src/lib/features/trends/charts` rather than importing LayerChart directly in routes.
3. Keep `TrendComparisonChart.svelte` on LayerChart:
   - map comparison points to chart data in the feature component;
   - render line/point series with real axes;
   - use a visible legend and chart tooltip;
   - keep the existing data table as the exact-value accessible fallback.
4. Keep `TrendSparkline.svelte` on the shared wrapper; only add interactions or an expanded chart mode if product needs hover values, multiple metrics, or axes.
5. Add future LayerChart-based components for tag/template time-series and per-meme expanded analytics once backend data exists.
6. Run `pnpm check`, `pnpm test`, and `pnpm build` from `frontend` after the dependency and first migration.

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

## Backend/API follow-up scope discovered

Do not block the library choice on backend work, but analytics UI beyond the current comparison page will need API additions:

- Tag and template comparison series currently expose `insufficient_history` and only current aggregate points in the frontend copy; proper trend lines need historical tag/template aggregate snapshots.
- Template and tag pages do not currently expose full analytics series suitable for per-page charts.
- The comparison point model has `observed_at: string | null`; charting works best if every time-series point has a timestamp or the API explicitly declares categorical/current-only series.
- Per-meme sparkline exposes raw captured points and metrics, but future expanded charts should define which metrics are selectable and how mixed metrics share axes.

## Sources checked

- Project files: `frontend/src/lib/features/trends/TrendSparkline.svelte`, `frontend/src/lib/features/trends/TrendComparisonChart.svelte`, `frontend/src/routes/memes/[id]/+page.svelte`, `frontend/src/routes/trends/compare/+page.svelte`, `frontend/src/routes/trends/timeline/+page.svelte`, `frontend/src/lib/api/types.ts`, `frontend/AGENTS.md`, `docs/prd/08-analytics.md`, `docs/prd/06-website.md`.
- LayerChart docs/repo/npm: `https://www.layerchart.com/`, `https://www.layerchart.com/getting-started`, `https://www.layerchart.com/docs/components/Axis`, `https://www.layerchart.com/docs/components/Tooltip`, `https://www.layerchart.com/changelog`, `https://github.com/techniq/layerchart`, `https://www.npmjs.com/package/layerchart`.
- Chart.js/svelte-chartjs docs/repo/npm: `https://www.chartjs.org/docs/latest/`, `https://saurav.tech/svelte-chartjs/`, `https://github.com/SauravKanchan/svelte-chartjs/releases`.
- Unovis docs/repo/npm: `https://unovis.dev/docs/quick-start`, `https://github.com/f5/unovis`, `https://www.npmjs.com/package/@unovis/svelte`.
- Layer Cake docs/repo: `https://layercake.graphics/`, `https://layercake.graphics/guide`, `https://github.com/mhkeller/layercake`.
