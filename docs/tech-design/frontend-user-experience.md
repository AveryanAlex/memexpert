# Frontend User Experience

## Purpose and Scope

This document defines the consumer-facing SvelteKit experience for the public website and Telegram Mini App. It implements the product contract in [Website](../prd/06-website.md) while preserving SSR, backend/API contracts, access control, telemetry, SEO, and existing deep links.

The redesign is a presentation and progressive-disclosure change. FastAPI remains the authority for search, recommendations, collections, permissions, actions, analytics, and Mini App authentication. `/admin/*` routes and admin feature components are outside this document's scope.

## UX Architecture Decisions

| Decision | Rationale |
| --- | --- |
| Discover, Search, Saved, and Account are the consumer navigation model. | These destinations map directly to browsing, intent-led retrieval, saved work, and personal settings. Trends remains reachable from Discover without taking the place of Search or Saved. |
| Card media and Favorite/Download/Save/Send actions are always visible. | A meme catalog should make the next meaningful action available where the media is seen, rather than hide primary interaction behind a menu. |
| Selection is an opt-in, local grid state. | Bulk tools are useful only after an intentional transition into management mode; they must not dominate passive discovery. |
| Search uses an ordered grid; Discover can use masonry. | Search order is relevance information. Discover optimizes browsing density across varied media dimensions. See [Grid and feed behavior](#grid-and-feed-behavior). |
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

Desktop navigation presents the brand, Discover, global query search, Saved, and a single Account or Sign in control. Mobile navigation keeps visible text labels with icons; icons alone are not the navigation contract. The shell reserves bottom space for its fixed navigation.

### Meme discovery and action surfaces

| Boundary | Responsibility |
| --- | --- |
| `MemeCard.svelte` | Media-first card. Only media/title is a detail link; direct controls remain sibling buttons so interactive elements are not nested. It carries list/rank semantics and optional access markers. |
| `MemeActionMenu.svelte` | One action implementation with `card`, `detail`, and `overflow` surfaces. Cards expose evenly distributed icon-only Favorite, Download, Save-to-collection, and Send controls; detail exposes labeled Favorite, Save-to-collection, and Send controls. Pin, Copy link, and Report remain contextual overflow actions. |
| `MemeGrid.svelte` | Owns layout choice, result attribution markup, local selection mode, checkboxes, and sticky selection toolbar. It does not decide whether a route enables bulk behavior. |
| `InfiniteMemeFeed.svelte` | Owns initial/next page state, deduplicated append behavior, intersection loading, and accessible Load more/retry/end states. It passes the route's layout and bulk policy to `MemeGrid`. |
| `MemeOfTheDayPanel.svelte` | Presents the daily selection as a compact media-first tile while retaining attribution for telemetry. |

`MemeActionMenu` preserves existing client API calls and status behavior. It receives viewer capability from the root context, so shared cards do not need account-type prop drilling. A Favorite action can trigger the existing guest-to-Telegram connection prompt on detail; it does not change the account model.

Save-to-collection opens a contextual chooser of collections where the viewer can add memes. The API orders those choices by the latest `collection_memes.added_at` value, descending with empty collections last, so the collection used for the most recent addition appears first. The client preserves that order and does not keep cross-user collection state in a module-level cache.

### Search, library, collections, detail, and trends

| Area | Boundaries and ownership |
| --- | --- |
| Search | `SearchFilters.svelte` owns the editable URL-backed query/filter draft, responsive dialog, and sensitive-content confirmation. `ActiveSearchFilters.svelte` renders consumer-language removable chips and empty-search suggestions. The route composes these with the ordered result feed. |
| Saved | `server/libraryPage.ts` centralizes the cookie-forwarded library request. `/library` owns collection creation, active save destination, collection list, Favorites, Pins, pin reorder, and saved-content grids. |
| Account | `/profile` owns Telegram connection, language, sensitive-content preference, and compact statistics only. Its server load fetches stats independently of the library. |
| Collection | The route presents metadata, active-save control, notices, and saved memes first. `CollectionManagement.svelte` contains the disclosure for rename/visibility, invitations, members, revocation, and deletion. |
| Meme/detail landing | `/memes/[id]` composes media, `MemeActionMenu`, concise context, a collapsed About section, and related discovery. Tag/template routes place galleries before their collapsed aggregate activity information. |
| Trends | Trend route components translate server-provided rankings and activity data into consumer labels, accessible charts, and tables. They do not redefine ranking on the client. |

## Route Ownership and Progressive Disclosure

| Route | Primary content | Deferred/disclosed content | Must not reappear here |
| --- | --- | --- | --- |
| `/` | Discover header, Meme of the Day, topics, feed | None required for normal browsing | Collection creation/list, bulk toolbar, recommendation/fallback diagnostics |
| `/search` | Query, Filters trigger, active chips, result count, ordered results | Filter form in a dialog/sheet | A permanently expanded advanced workspace or raw implementation terms |
| `/library` | Favorites, Collections, Pins, active save destination | New collection form | Provider/account diagnostics |
| `/profile` | Telegram connection, language, sensitive-content preference, compact stats | Interaction stats | Saved grids, pin ordering, collection list, active save selector |
| `/collection/{id}` | Collection summary, save destination, saved memes | **Manage collection** native disclosure | Management controls above the meme grid |
| `/memes/{id}` | Media, Favorite/Save/Send, title/description, tags, related memes | **About this meme** | MIME/file/byte rows, raw score, API/fallback diagnostics |
| `/tags/{tag}`, `/templates/{slug}` | Page context and gallery | **About this tag/template** activity disclosure | Aggregate diagnostic panels before media |
| `/trends` | Ranked media and readable weekly story | Compare and timeline links | Raw trend score and materialization/history jargon |

`/library` and `/profile` deliberately use separate server loads. A library failure must not prevent account preferences or stats from rendering, and a stats failure must not prevent Saved content from rendering.

## Responsive Behavior

### Discover and cards

- The first 390px-wide Discover viewport must include meme media. Large hero copy, management panels, and permanent bulk controls are excluded from the route.
- Cards keep aspect-ratio-preserving media, a concise caption when one exists, and visible icon-only Favorite, Download, Save, Send, and overflow controls at touch sizes. The synthetic `Untitled meme` fallback remains available to accessible names and media alt text but does not render as a visible caption row.
- Media/title remains the card's detail link. Direct action controls come after it in the card's tab order.
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

## Telemetry and Attribution Invariants

Presentation must not discard a result's `MemeResultAttributionRead` data just because it is not rendered as consumer copy.

1. `MemeGrid` and Meme of the Day retain discovery data attributes for source algorithm, reason, request ID, impression ID, source meme ID, and score context.
2. `MemeCard` records one impression after at least 25% intersection and records a detail click before navigation. Both send the attribution body with keepalive support.
3. Favorite, Save, Pin, Send/share, Download, Report, and bulk-download paths forward the same attribution through `memeActionAttributionBody` where the existing action supports it.
4. Detail links serialize attribution (`request_id`, `impression_id`, surface, source algorithm, query/filter/scope/collection context, rank, score components, and related-source identity) so the detail action chain remains attributable.
5. Consumer surfaces may say only what helps a user decide. Algorithm names, fallback reasons, request IDs, score components, and candidate diagnostics remain telemetry/data attributes rather than visible copy.

The redesign therefore keeps the analytics contract described in [Analytics](../prd/08-analytics.md) while removing debug-heavy presentation.

## Grid and Feed Behavior

### Ordered Search

`MemeGrid` receives `layout="ordered"` from `/search`. It renders one responsive CSS grid in the exact backend array order: one column on narrow screens, then two, three, and four columns as space permits. DOM order, visual sequence, keyboard traversal, `aria-posinset`, and result rank remain aligned with backend relevance order.

This is intentionally different from an image-gallery algorithm. A person evaluating a search result needs predictable order, readable meme text, stable pagination, and a clear relationship between the result count/rank and the card they encounter.

### Masonry Discover

Discover and gallery-oriented surfaces use the masonry mode when density is more valuable than strict visual relevance ordering. The layout processes backend items sequentially and appends each item to the currently shortest estimated column, breaking ties by earlier column. It is deterministic and does not randomly shuffle input, but multi-column visual scanning is not the ordered-search relevance contract.

Masonry reduces unused space when images, GIFs, videos, and placeholders have different heights. On a one-column mobile layout it naturally preserves array order.

### Infinite pagination and selection

`InfiniteMemeFeed` deduplicates initial results and appends only unseen IDs in later page order. Loading state, retry, result count, explicit Load more fallback, and end state remain available regardless of `IntersectionObserver` support.

Bulk capability is passed per route. `MemeGrid` then owns a local `selectionMode`:

- no checkboxes or sticky toolbar before **Select items** is chosen;
- Select all, Clear, Done, and actionable save/add/remove/download controls appear only in that mode;
- Clear, Done, disabling bulk capability, and successful collection removal leave the mode cleanly;
- Discover explicitly passes bulk disabled.

## Detail, Collection, and Trend Semantics

### Detail and collection disclosures

Meme detail starts with media, then concise context and direct actions. Context text, OCR text, and popularity summary/sparkline are behind **About this meme**. This keeps the source detail, action forms, Telegram connection prompt, tags, related results, and telemetry intact while omitting file-level diagnostics and raw ranking scores from consumer UI.

Collection management uses native `<details>/<summary>` after the content grid. Its forms retain existing hidden inputs, action names, capability checks, clipboard fallback, notices, invitation lifecycle, member-role constraints, and destructive-operation semantics. It is not a reduced permission model; it is a delayed presentation of the same permitted operations.

### Trends

Trend rankings are supplied by the backend. The UI does not expose a raw `trending_score` or describe materialization/fallback internals as user-facing facts.

The shared public measure is **recorded activity**:

```text
recorded activity = original-source views + reactions + reposts
                  + MemeExpert views + sends + saves + favorites
```

It is an unweighted signal count, not a unique-person count and not the ranking/popularity score. Source signals can overlap with platform signals. Trend cards use understandable direction and recent-change language; charts, aggregate histories, comparisons, and timeline cards label the measure consistently and show source versus MemeExpert breakdowns.

Trend visualizations follow these rules:

- Plot a comparison or aggregate line only when there are at least two usable recorded-activity points; do not fabricate a line for a single/current point.
- Provide chart title/description, legend, tooltip context, and an adjacent readable table when exact values matter.
- Keep comparison URL serialization and use labeled item type plus name/identifier fields in enhanced UI; noscript input retains the serialized route format.
- Present the timeline as month/year nostalgia browsing, while preserving its existing granularity and pagination controls.

## Accessibility Requirements

- Use named navigation landmarks, active-page `aria-current`, visible mobile labels, and focus-visible rings on all interactive primitives.
- Preserve semantic card/list structures. Cards expose title-based detail links, `aria-posinset`/`aria-setsize` where a feed provides rank, labeled action groups, and pressed state for Favorite/Save controls.
- Do not nest action buttons inside the detail link. Keyboard focus reaches media/detail, Favorite, Download, Save, Send, then overflow in a predictable card order. Icon-only card controls retain explicit accessible names and pressed state; a favorited heart is filled and uses the danger/red token instead of changing to a visible “Favorited” label.
- Dialogs have title/description/close controls; filter-sheet content scrolls internally; Reset/Show results are reachable at every viewport size.
- Selection checkboxes have meme-specific labels, selection feedback uses live status, and pin ordering retains keyboard Up/Down controls in addition to pointer drag support.
- Native disclosures provide a semantic summary. Status/error messages use appropriate live/alert roles without replacing essential visible text with tooltips.
- Charts use accessible titles/descriptions and non-visual table or summary equivalents. The visual chart is never the only representation of trend data.
- Respect `min-width: 320px`, Telegram safe areas, and bottom-navigation clearance. Verify no horizontal overflow and no covered final interactive content at narrow widths.

## Regression Checklist for Future UX Changes

1. Preserve Discover/Search/Saved/Account paths, active state, and mobile labels.
2. Keep media and Favorite/Download/Save/Send visible on cards; keep secondary actions in overflow.
3. Keep Search filters URL-backed, access-aware, consumer-labeled, and responsively disclosed.
4. Do not move library content back into Account or collection management above saved memes.
5. Preserve detail/tag/template progressive disclosure and related-result attribution.
6. Preserve recorded-activity semantics and accessible trend tables; do not surface raw score/diagnostic copy.
7. Preserve Mini App theme, safe-area, `initData`, and start-parameter behavior.
8. Run focused SSR tests, frontend checks/build, and browser smoke/visual acceptance when modifying these contracts.
