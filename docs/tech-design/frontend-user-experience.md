# Frontend User Experience

## Purpose and Scope

This document defines the consumer-facing SvelteKit experience for the public website and Telegram Mini App. It implements the product contract in [Website](../prd/06-website.md) while preserving SSR, backend/API contracts, access control, telemetry, SEO, and existing deep links.

The redesign is a presentation and progressive-disclosure change. FastAPI remains the authority for search, recommendations, collections, permissions, actions, analytics, and Mini App authentication. `/admin/*` routes and admin feature components are outside this document's scope.

## UX Architecture Decisions

| Decision | Rationale |
| --- | --- |
| Discover, Search, Saved, and Account are the consumer navigation model. | These destinations map directly to browsing, intent-led retrieval, saved work, and personal settings. Trends remains reachable from Discover without taking the place of Search or Saved. |
| Card media, image enlargement, and Favorite/Download/Save/Send actions are always visible. | A meme catalog should make the next meaningful action available where the media is seen, rather than hide primary interaction behind a menu. |
| Selection is an opt-in, local grid state. | Bulk tools are useful only after an intentional transition into management mode; they must not dominate passive discovery. |
| Multi-card surfaces use rank-aware measured masonry. | One placement contract preserves backend relevance in DOM and visual order while packing varied media dimensions densely. See [Grid and feed behavior](#grid-and-feed-behavior). |
| Similar memes use an SSR-first infinite feed. | The detail route renders 12 results immediately and appends bounded pages through the shared rank-aware masonry without exposing bulk tools; backend similarity/fallback order and attribution remain intact. |
| Saved content and account settings have separate routes. | Library operations are content management; connection and preferences are account management. Keeping them separate makes each route smaller and more legible. |
| Advanced or technical information lives behind explicit disclosure. | Collections, meme details, tag/template analytics, and trends should lead with media and understandable context, not control panels or diagnostics. |
| Telegram adapts the host shell instead of forking routes. | Website and Mini App retain one route and action contract while adopting Telegram viewport, theme, safe-area, and authentication behavior. |

## Component Boundaries

### Application shell

| Boundary | Owner | Responsibility |
| --- | --- | --- |
| `features/app-shell/navigation.ts` | Navigation model | Defines Discover (`/`), Search (`/search`), Saved (`/library`), Account (`/profile`), and conditional Admin items. Collection routes resolve as Saved for active-state purposes. |
| `features/app-shell/AppShell.svelte` | Shared route shell | Renders the desktop header, mobile bottom navigation, account/sign-in control, page-safe bottom padding, and active navigation state. |
| `features/app-shell/GlobalSearch.svelte` | Global search entry | Provides a single query-only GET form on desktop and an explicit Search route entry on narrow screens. It does not own filter controls. |
| `auth-state.ts` | Reactive session projection | Provides the root-layout, context-scoped Svelte store used by the shell, viewer capability context, and account-aware routes. SSR session data seeds an isolated instance; successful browser auth and preference mutations publish to it before route invalidation. It never stores the HttpOnly token. |
| `TelegramMiniAppBootstrap.svelte` and `telegram-miniapp.ts` | Host adaptation | Detects Telegram data, applies host state, runs readiness/expand hooks, authenticates `initData`, and handles supported start parameters. |
| `lib/ui/*` | Reusable primitives | Provides token-based controls, focus treatment, dialogs, disclosures, menus, charts, and layout primitives without owning meme- or account-specific behavior. |
| `lib/ui/PillLink.svelte` | Reusable pill navigation | Provides compact topic/chip links and larger active tabs with one focus treatment and `aria-current` policy. Product surfaces provide destinations and active state. |

Desktop navigation presents the brand, Discover, global query search, Saved, and a single Account or Sign in control. Mobile navigation keeps visible text labels with icons; icons alone are not the navigation contract. The shell reserves bottom space for its fixed navigation.

### Meme discovery and action surfaces

| Boundary | Responsibility |
| --- | --- |
| `MemeCard.svelte` | Media-first card. Only media/title is a detail link; direct controls remain sibling buttons so interactive elements are not nested. It carries list/rank semantics and optional access markers. |
| `MemeZoomDialog.svelte` | Top-right image overlay action that opens the highest-resolution available image at its intrinsic size, capped to the viewport without cropping or upscaling. |
| `MemeActionMenu.svelte` | One action implementation with `card`, `detail`, and `overflow` surfaces. Cards expose evenly distributed icon-only Favorite, Download, Save-to-collection, and Send controls; detail exposes labeled Favorite, Save-to-collection, and Send controls. Pin, Copy link, and Report remain contextual overflow actions. |
| `SaveCollectionChooser.svelte` | Persistent meme-scoped chooser. It groups existing memberships before writable destinations, preserves backend recency order, supports in-place add/remove, and publishes aggregate bookmark state across duplicate cards. |
| `MemeVideoPlayer.svelte` | Shared grid video preview. It coordinates one active preview/audio source, uses viewport autoplay in one-column grids, hover playback in wider fine-pointer grids, click/keyboard pause, muted defaults, and visibility/reduced-motion safeguards. |
| `lib/ui/Masonry.svelte` | Generic measured layout primitive. It retains one keyed flat DOM list, calculates rank-aware coordinates from rendered item heights, and owns hydration/no-JavaScript fallback behavior without meme-domain policy. |
| `MemeGrid.svelte` | Composes the shared masonry primitive with result attribution markup, local selection mode, checkboxes, and the sticky selection toolbar. It does not decide whether a route enables bulk behavior. |
| `discovery-attribution.ts` | Maps the shared discovery-attribution fields to DOM data attributes for grids and Meme of the Day without diverging field sets. |
| `InfiniteMemeFeed.svelte` | Owns initial/next page state, deduplicated append behavior, intersection loading, and accessible Load more/retry/end states for Search and Similar sources. It resets and aborts stale pagination when its source key changes, passes route bulk policy to `MemeGrid`, and shares its layout across surfaces. |
| `MemeOfTheDayPanel.svelte` | Presents the daily selection as a compact media-first tile while retaining attribution for telemetry. |
| `features/taxonomy/TaxonomyLandingPage.svelte` and `server/taxonomyLanding.ts` | Share gallery-first tag/template presentation and loading while route files remain thin kind/slug adapters. |

`MemeActionMenu` preserves existing client API calls while keeping routine action success feedback visual: Favorite and Save update their pressed/filled icon states without adding a status row, and action failures still render an error. It receives viewer capability from the root context, so shared cards do not need account-type prop drilling. A Favorite action can trigger the existing guest-to-Telegram connection prompt on detail; it does not change the account model.

Save-to-collection opens a borderless contextual popover backed by a meme-scoped collection-choice API. The API includes existing accessible non-Favorites memberships, including read-only shared collections, plus unsaved writable collections. Existing memberships render under **Saved in** before **Add to** destinations. The API orders choices by the latest `collection_memes.added_at` value, descending with empty collections last; the client preserves that order and keeps membership patches only in the layout-scoped meme action state.

### Search, library, collections, detail, and trends

| Area | Boundaries and ownership |
| --- | --- |
| Search | `SearchFilters.svelte` owns the editable URL-backed query/filter draft, responsive dialog, and sensitive-content confirmation. `ActiveSearchFilters.svelte` renders consumer-language removable chips and empty-search suggestions. The route composes these with the shared rank-aware result feed. |
| Saved | `server/libraryPage.ts` centralizes the cookie-forwarded library request. `/library` owns collection creation, active save destination, collection list, Favorites, Pins, pin reorder, and saved-content grids. |
| Account | `/profile` owns Telegram connection, language, sensitive-content preference, and compact statistics only. Its server load fetches stats independently of the library. |
| Collection | The route presents metadata, active-save control, notices, and saved memes first. `CollectionManagement.svelte` contains the disclosure for rename/visibility, invitations, members, revocation, and deletion. |
| Meme/detail landing | `/memes/[id]` composes media, `MemeActionMenu`, de-duplicated concise context, **About this meme**, full-width **Sources & activity**, nested **Professional analytics**, and an SSR-first Similar feed. Detail, sources, analytics, and Similar loads fail independently. Tag/template routes place galleries before their collapsed aggregate activity information. |
| Trends | Trend route components translate server-provided rankings and activity data into consumer labels, accessible charts, and tables. They do not redefine ranking on the client. |

## Route Ownership and Progressive Disclosure

| Route | Primary content | Deferred/disclosed content | Must not reappear here |
| --- | --- | --- | --- |
| `/` | Discover header, Meme of the Day, topics, feed | None required for normal browsing | Collection creation/list, bulk toolbar, recommendation/fallback diagnostics |
| `/search` | Query, Filters trigger, active chips, result count, rank-aware results | Filter form in a dialog/sheet | A permanently expanded advanced workspace or raw implementation terms |
| `/library` | Favorites, Collections, Pins, active save destination | New collection form | Provider/account diagnostics |
| `/profile` | Telegram connection, language, sensitive-content preference, compact stats | Interaction stats | Saved grids, pin ordering, collection list, active save selector |
| `/collection/{id}` | Collection summary, save destination, saved memes | **Manage collection** native disclosure | Management controls above the meme grid |
| `/memes/{id}` | Media, Favorite/Save/Send, one non-duplicated title/description sequence, tags, related memes | **About this meme**, then **Sources & activity** with nested **Professional analytics** | MIME/file/byte rows, raw score, API/fallback diagnostics, crawler/session/source identities |
| `/tags/{tag}`, `/templates/{slug}` | Page context and gallery | **About this tag/template** activity disclosure | Aggregate diagnostic panels before media |
| `/trends` | Ranked media and readable weekly story | Compare and timeline links | Raw trend score and materialization/history jargon |

`/library` and `/profile` deliberately use separate server loads. A library failure must not prevent account preferences or stats from rendering, and a stats failure must not prevent Saved content from rendering.

## Responsive Behavior

### Discover and cards

- The first 390px-wide Discover viewport must include meme media. Large hero copy, management panels, and permanent bulk controls are excluded from the route.
- Cards keep aspect-ratio-preserving media, a concise caption when one exists, and visible icon-only Favorite, Download, Save, Send, and overflow controls at touch sizes. Multi-column grids add a top-right image-enlargement overlay; one-column feeds omit that redundant control because media already uses the available width. The synthetic `Untitled meme` fallback remains available to accessible names and media alt text but does not render as a visible caption row. Interactive videos use a separate Open meme overlay so play/mute controls are never nested inside a link.
- Video preview policy follows the measured rendered grid: sufficiently visible one-column previews autoplay muted unless reduced motion is requested; multi-column fine-pointer previews play on hover; coarse-pointer multi-column previews remain click-to-play. Leaving hover/viewport, hiding the page, or activating another preview pauses and re-mutes the previous video.
- Image media/title remains the card's detail link. Interactive videos instead expose a separate Open meme link before Play/Pause and Mute/Unmute controls in tab order.
- At 320px and above, controls must remain within the viewport without horizontal page overflow.

### Search filters

The route always renders one query field and one Filters trigger. The full filter interface is mounted only while open.

| Viewport | Filter container | Interaction requirements |
| --- | --- | --- |
| Desktop/tablet (`md` and wider) | Right-side dialog/drawer, maximum width 380px and viewport height | Labeled dialog, close control, internally scrollable content, Reset and Show results actions. |
| Mobile | Bottom sheet, maximum height `85dvh` | Scrollable inner content, sticky Reset and Show results actions, safe-area-aware bottom padding, no result control obstruction. |

The visible labels are Tags or categories, Media type, Language, Sensitive content, Where to search, and Collections. Scope choices are Everywhere, Public memes, My saved memes, and Specific collections. Collection checkboxes use readable collection titles, filtered to collections the viewer can open.

### Telegram host shell

When Telegram bootstrap data is available, the application adds `telegram-miniapp` state to the document and supplies Telegram theme and viewport values as CSS variables. The host shell:

- uses Telegram colors for canvas, paper, text, borders, and primary action tokens;
- uses Telegram stable/regular viewport height and all safe-area insets;
- hides redundant website brand, Account, and Sign in chrome;
- retains the compact route shell, bottom-navigation safe placement, and direct Send action;
- calls `ready()` and `expand()` best-effort so host API failures do not block browsing.

Mini App authentication keeps the existing backend contract: `initData` is POSTed to `/telegram-miniapp/auth`. On success the browser reads the repaired current session into the root auth store before invalidating route data, so shared account UI changes without a document reload. Supported `invite_…` and `meme_…` start parameters route once to their collection-invite or meme destinations. If authentication fails or is unavailable, the current web session remains usable.

## State and URL Invariants

### Search

`SearchRouteState` is the single route-state model for Search. It normalizes and serializes these existing parameters:

| State | URL parameter | Invariant |
| --- | --- | --- |
| Query | `q` | Trimmed before navigation. |
| Tags | repeated `tags` | Comma-separated input is normalized, de-duplicated, lowercased, and serialized as repeated values. Legacy category aliases remain parse-compatible. |
| Sensitive-content choice | `include_nsfw` | Always serialized as a boolean string; UI confirmation may update the account preference before navigating. |
| Media and language | `media_type`, `language` | Only supported values are retained. |
| Search destination | `scope` | Defaults to `public` when invalid or absent. |
| Specific collections | repeated `collection_ids` | Retained only for `scope=collections`, normalized and de-duplicated. The server/API still performs final access control. |
| Pagination | `offset` | Positive offset only; changing query or filters resets it to zero. |

The SSR load parses this state, forwards it unchanged to the existing meme page API request, and keeps canonical/noindex behavior. Filter chips are links built from the same serializer, so removing one filter does not discard the rest of the state. The user never types collection IDs, although collection-scoped URLs remain shareable and permission-checked.

### Route, form, and deep-link contracts

- Keep existing public paths, server action names, form fields, query parameters, cookie forwarding, and API payloads. A visual refactor must not change the backend contract.
- `memeHref` continues to generate `/memes/{seo_page_slug|id}` and preserves available attribution query parameters on detail links.
- Collection pages, library creation, active-save selection, invite/member forms, pin reorder, comparison serialization, trend ranking/timeline controls, and Mini App start-parameter routes retain their established URL/action behavior.
- The search filter drawer is a presentation wrapper around a real GET form. Native form submission remains meaningful without JavaScript; the trends comparison route retains its noscript serialized-item inputs.
- Meme source/analytics controls preserve discovery-attribution parameters and
  stable source/professional disclosure anchors. Their local open state survives
  same-route navigation. `source_sort`, `source_offset`, and the API-issued
  `source_snapshot` keep source pages stable; `activity_window` selects
  `7d`, `30d`, `90d`, or `all`. Changing source sort clears offset/snapshot;
  paging reuses the response snapshot. Clean share/canonical meme URL builders
  omit these presentation parameters; this does not require the page to emit a
  dedicated `rel=canonical` element.
- Detail GETs are read-only. After hydration the route posts one `meme_view`
  for the displayed meme ID and keeps that client guard across insight query
  navigation, so pagination, sorting, and range selection do not inflate the
  page's own activity chart.

## Telemetry and Attribution Invariants

Presentation must not discard a result's `MemeResultAttributionRead` data just because it is not rendered as consumer copy.

1. `MemeGrid` and Meme of the Day retain discovery data attributes for source algorithm, reason, request ID, impression ID, source meme ID, and score context.
2. `MemeCard` records one impression after at least 25% intersection and records a detail click before navigation. Exposure IDs are stable for one layout placement/page, survive component remounts, and remain distinct for genuinely different placements. Both sends use keepalive support; the idempotent backend exposure fact prevents a repeated token from increasing the keyed denominator.
3. Favorite, Save, Pin, Send/share, Download, Report, and bulk-download paths forward the same attribution through `memeActionAttributionBody` where the existing action supports it.
4. Detail links serialize attribution (`request_id`, `impression_id`, surface, source algorithm, query/filter/scope/collection context, rank, score components, and related-source identity) so the detail action chain remains attributable.
5. Consumer surfaces may say only what helps a user decide. Algorithm names, fallback reasons, request IDs, score components, and candidate diagnostics remain telemetry/data attributes rather than visible copy.

The redesign therefore keeps the analytics contract described in [Analytics](../prd/08-analytics.md) while removing debug-heavy presentation.

## Grid and Feed Behavior

### Rank-aware measured masonry

All multi-card meme surfaces share one measured masonry primitive. `MemeGrid` uses it for Search, Discover, Saved, collections, related memes, and taxonomy galleries; Trends and each Timeline period use the same primitive with a three-column maximum. The primitive renders items once as a keyed flat DOM list in backend order and never regroups or moves DOM nodes into visual column containers.

After measuring the container and naturally rendered items at their final column width, placement processes items strictly in array order. Rank 1 is top-left; for any ranks `i < j`, `top(i) <= top(j)`; items with the same top coordinate appear left-to-right in rank order. Selection of the shortest eligible column and left-biased tie-breaking makes placement deterministic. Coordinates use actual rendered heights rather than media or caption estimates, reducing unused space without weakening search relevance, keyboard traversal, `aria-posinset`, or `aria-setsize` semantics.

SSR emits a complete responsive grid in semantic order. On a scripting-capable client, the initial cards remain measurable but hidden while the first coordinates and container height are calculated; those styles are committed atomically before all cards are revealed, with no transition. A user therefore never sees the fallback grid reorder during hydration. `@media (scripting: none)` exposes the fallback immediately when JavaScript is disabled, and missing measurement support also reveals the ordered fallback. One-column layouts remain ordinary flow.

Container resizing and post-reveal media-height changes may trigger a measured relayout without changing DOM order. Infinite append preserves every existing coordinate and withholds only the new batch until it is measured, so loading more results cannot visibly reorder cards already shown.

### Infinite pagination and selection

`InfiniteMemeFeed` deduplicates initial results and appends only unseen IDs in later page order. Loading state, retry, result count, explicit Load more fallback, and end state remain available regardless of `IntersectionObserver` support.

For `similar`, the meme-scoped SSR load requests the canonical first 12 results. Browser pagination uses the same-origin SvelteKit proxy with `limit=12` and offsets `12`, `24`, and so on; the proxy validates pagination, forwards cookies, propagates upstream error status and cancellation, and marks every response `private, no-store`. Same-detail source/analytics query navigation retains that SSR result instead of repeating Similar retrieval. The feed appends in API order until `has_more` is false or its stable bounded `total` (at most 200) is reached. Navigation, component teardown, or a changed source meme ID aborts in-flight work; a source change also clears the prior source's results, error, count, and pagination state.

The Similar feed is always rank-preserving and bulk-disabled. It relies on the API to exclude the source and own Qdrant/tag/template/popular degradation, while the client only deduplicates repeated IDs caused by an eventually consistent page boundary. The public popularity materialized view refreshes on a five-minute cadence, so pages are best-effort across refreshes and do not claim snapshot isolation. A page failure leaves the meme detail usable and exposes an inline retry; an empty first page and a completed feed have distinct accessible states.

All initial and appended Similar cards retain API-provided attribution, including global rank and related-source identity. Their detail links override the application's hover preload policy with `data-sveltekit-preload-data="tap"`; taps, clicks, and keyboard activation retain normal navigation without hover-triggered detail-data waterfalls.

Bulk capability is passed per route. `MemeGrid` then owns a local `selectionMode`:

- no checkboxes or sticky toolbar before **Select items** is chosen;
- Select all, Clear, Done, and actionable save/add/remove/download controls appear only in that mode;
- Clear, Done, disabling bulk capability, and successful collection removal leave the mode cleanly;
- Discover and Similar explicitly pass bulk disabled.

## Detail, Collection, and Trend Semantics

### Detail and collection disclosures

Meme detail starts with media, then concise context and direct actions. Visible
title, lead description, body/context, and OCR candidates are considered in
that priority order. Before rendering each lower-priority candidate, normalize
with Unicode NFKC, trim, collapse whitespace, and lowercase without locale
dependence; suppress only an
exact normalized duplicate already displayed. SEO meta description and image
alt text stay independent. Long context, OCR, and the compact legacy popularity
summary remain behind **About this meme**.

After the article, **Sources & activity** summarizes posts, channels, and known
views and lists all eligible Telegram source posts. It defaults to most viewed,
puts unknown metrics last, marks missing values as not captured, retains
unavailable historical posts, uses only API-provided safe links, and supports
stable paginated sorting. The disclosure is full-width at 320px and desktop.

Nested **Professional analytics** owns 7/30/90/All controls, headline Recorded
activity/average/momentum/peak/favorites, a source-versus-MemeExpert activity
chart, an absolute Telegram end-state chart with a selector for views,
reactions, comments, or reposts, source performance/audience coverage, separate
web/inline funnels, and exact-value tables for both chart inputs. The activity
chart uses signals per day so daily, weekly, and monthly adaptive buckets remain
visually comparable; exact bucket totals and granularity remain in the table.
Both charts use UTC time axes; an absolute point is positioned at the last real
capture time represented by its server bucket. Absolute source lines may
decrease after Telegram corrections, and a nullable selected counter creates a
visible gap rather than a zero or a line across unknown data; activity never
decreases. Missing/one-point history uses an honest insufficient-history state
instead of a fabricated line. Each projection has its own loading/error state,
so an analytics or related-content failure does not remove source attribution
or the meme itself.

The SSR-first Similar feed follows those independent projections. It keeps the
source detail, action forms, Telegram connection prompt, tags, and telemetry
intact while omitting file-level diagnostics, raw ranking scores, and fallback
implementation details from consumer UI.

Collection management uses native `<details>/<summary>` after the content grid. Its forms retain existing hidden inputs, action names, capability checks, clipboard fallback, notices, invitation lifecycle, member-role constraints, and destructive-operation semantics. It is not a reduced permission model; it is a delayed presentation of the same permitted operations.

### Trends

Trend rankings are supplied by the backend. The UI does not expose a raw `trending_score` or describe materialization/fallback internals as user-facing facts.

The shared public measure is **recorded activity**:

```text
recorded activity = original-source views + reactions + reposts
                  + MemeExpert views + sends + saves + favorites
```

It is an unweighted signal count, not a unique-person count and not the ranking/popularity score. Source signals can overlap with platform signals. Trend cards use understandable direction and recent-change language; charts, aggregate histories, comparisons, and timeline cards label the measure consistently and show source versus MemeExpert breakdowns.

Comments, impressions/results served, downloads, and subscriber counts are
adjacent metrics, not Recorded activity. Source activity uses per-counter
running high-watermark increases; the server-bucketed absolute end-state chart
preserves corrections between returned points. Subscriber-normalized rates
always show eligible/total coverage and never call summed channel subscribers
reach or unique audience.

Trend visualizations follow these rules:

- Plot a comparison or aggregate line only when there are at least two usable recorded-activity points; do not fabricate a line for a single/current point.
- Provide chart title/description, legend, tooltip context, and an adjacent readable table when exact values matter.
- Keep comparison URL serialization and use labeled item type plus name/identifier fields in enhanced UI; noscript input retains the serialized route format.
- Present the timeline as month/year nostalgia browsing, while preserving its existing granularity and pagination controls.

## Accessibility Requirements

- Use named navigation landmarks, active-page `aria-current`, visible mobile labels, and focus-visible rings on all interactive primitives.
- Preserve semantic card/list structures. The masonry root and its cards remain a flat DOM list in backend rank order. Cards expose title-based detail links, `aria-posinset`/`aria-setsize` where a feed provides rank, labeled action groups, and pressed state for Favorite/Save controls.
- Do not nest action buttons inside the detail link. Image-card focus reaches media/detail, then Enlarge when the grid offers it, Favorite, Download, Save, Send, and overflow. Video-card focus reaches Open meme, Play/Pause, Mute/Unmute, then the same direct actions. The enlargement dialog uses the best available image variant, preserves its full aspect ratio within the viewport, traps focus, closes with Escape, and restores focus to its trigger. Icon-only card controls retain explicit accessible names and pressed state; a favorited heart is filled and uses the danger/red token instead of changing to a visible “Favorited” label.
- Viewer action state is layout-scoped and keyed by meme ID, so repeated presentations of the same meme (for example Meme of the Day plus the home feed) update Favorite, Save, Pin, and the visible like count together. Save choosers omit Favorites, and the bookmark is pressed when the meme belongs to any accessible non-Favorites collection. The state is discarded when the active viewer identity changes.
- Dialogs have title/description/close controls; filter-sheet content scrolls internally; Reset/Show results are reachable at every viewport size.
- Selection checkboxes have meme-specific labels, selection feedback uses live status, and pin ordering retains keyboard Up/Down controls in addition to pointer drag support.
- Native disclosures provide a semantic summary. Status/error messages use appropriate live/alert roles without replacing essential visible text with tooltips.
- Charts use accessible titles/descriptions and non-visual table or summary equivalents. The visual chart is never the only representation of trend data.
- Respect `min-width: 320px`, Telegram safe areas, and bottom-navigation clearance. Verify no horizontal overflow and no covered final interactive content at narrow widths.

## Regression Checklist for Future UX Changes

1. Preserve Discover/Search/Saved/Account paths, active state, and mobile labels.
2. Keep media, image enlargement, and Favorite/Download/Save/Send visible on cards; keep secondary actions in overflow.
3. Keep Search filters URL-backed, access-aware, consumer-labeled, and responsively disclosed.
4. Do not move library content back into Account or collection management above saved memes.
5. Preserve detail/tag/template progressive disclosure, visible-text deduplication, source/analytics partial states, and related-result attribution.
6. Preserve recorded-activity semantics and accessible trend tables; do not surface raw score/diagnostic copy.
7. Preserve Mini App theme, safe-area, `initData`, and start-parameter behavior.
8. Run focused SSR tests, frontend checks/build, and browser smoke/visual acceptance when modifying these contracts.
