# Website

## Technology

SvelteKit SSR application, responsive from a 320px viewport upward. The same public routes serve the website and Telegram Mini App; presentation adapts to the host without changing authentication, access control, API contracts, SEO, or deep links.

## Experience Principles

- The primary loop is **discover → search → save or send → continue discovering**.
- The first mobile Discover viewport prioritizes meme media. Marketing copy, collection administration, and bulk controls must not displace it.
- Consumer-facing copy is concise and uses familiar terms. Backend fields, ranking diagnostics, raw IDs, scores, fallback algorithms, and candidate counts are not consumer UI.
- Favorite means adding a meme to Favorites. The card Save control chooses a writable non-Favorites collection, and its bookmark is active when the meme belongs to any non-Favorites collection the viewer can access. Send opens Telegram sharing.

## Application Shell

### Navigation

The primary navigation is **Discover**, **Search**, **Saved**, and **Account**.

| Surface | Behavior |
| --- | --- |
| Desktop | The header contains the brand, Discover, a query-only global search form, Saved, and one Account or Sign in control. The search placeholder includes the formatted public, non-sensitive catalog count when available and falls back to generic search copy when it is unavailable. Global search submits to `/search`; filtering belongs on the Search route. |
| Mobile | A fixed bottom navigation shows an icon and visible label for Discover, Search, Saved, and Account. Page content reserves space so the navigation does not cover the final controls. |
| Collection routes | `/collection/{id}` is part of the Saved journey, so Saved remains the active navigation destination there. |
| Trends | Trends are a secondary discovery destination from Discover, not a replacement for Search or Saved in primary navigation. |
| Admin | Admin navigation remains conditional on administrator capability and separate from consumer navigation. |

Discover is `/`, Search is `/search`, Saved is `/library`, and Account is `/profile`.

### Discover (`/`)

Discover uses a compact heading, Search and Trends links, Meme of the Day, optional topic shortcuts, and the feed. It does not include collection creation, a collection list, backend/recommendation explanations, or a permanent bulk toolbar.

The feed can be personalized or use a public fallback, but the reason a result was selected is retained for telemetry rather than shown as operational copy. Meme of the Day remains a prominent media tile; selection dates, algorithm versions, and candidate counts are not displayed.

## Media-First Cards and Actions

Meme cards preserve the media aspect ratio and make the media/title area the detail link. A concise caption and, when relevant, a private/shared access marker may appear below media. Dimensions, language, tag rows, scores, and other dense metadata are omitted from the normal card surface.

Every card exposes these direct, labeled actions without opening a menu:

- **Favorite**
- **Download**
- **Save**
- **Send**

The card overflow menu contains only secondary or safety actions that are not already visible below the meme: pinning when the viewer is eligible, copy link, and report. Detail pages use labeled Favorite, Save, and Send controls; Download remains in detail overflow when it is not otherwise visible. Action results and failures are announced without duplicating primary actions.

Save opens a persistent collection chooser. Collections already containing the meme appear first under **Saved in**, with one-step removal when the viewer can edit them; remaining writable collections appear under **Add to**. Favorites is never shown there. The bookmark remains active until the meme is removed from every accessible non-Favorites collection.

Video cards use the poster belonging to their active immutable web-video
generation until playback starts. Playback URLs carry a version token derived
from that active object, so an atomic derivative repair cannot leave a browser
on a stale video/poster pair. A one-column grid autoplays only the sufficiently
visible video, muted and inline; multi-column pointer/hover layouts play on
hover. Clicking the video toggles play/pause, leaving the card or viewport
pauses and re-mutes it, and an explicit control allows unmuting. When the source
was audible, the verified AAC-LC track becomes audible after **Unmute**; silent
sources remain silent. Interactive video cards retain a separate visible Open
meme link. Authenticated media redirects are private and non-cacheable.

## Search (`/search`)

Search is a focused results page: a compact query field and **Filters** trigger, active filter chips, a result summary, then results. An empty search offers suggested intents and categories, not fabricated local search history.

### Filters

The filter disclosure contains tags/categories, media type, language, sensitive-content preference, search destination, and named accessible collections. Search destinations are presented as **Everywhere**, **Public memes**, **My saved memes**, and **Specific collections**. Collection choices use collection titles; raw collection IDs and implementation names are not entered or explained to users.

- On desktop, Filters opens a right-side dialog/drawer no wider than 380px.
- On mobile, Filters opens a bottom sheet capped near 85dvh. Its body scrolls independently and Reset/Show results stay available at the bottom.
- Selecting sensitive content honors the account preference and requests confirmation before enabling it for an account that has not opted in.
- Active chips remove one filter at a time and preserve the remaining search state.

Search state remains URL-backed and shareable. Query, tags, sensitive-content choice, media type, language, scope, selected collections, and offset retain their established URL semantics; an incoming collection-scoped link is still subject to the recipient's access permissions.

### Rank-aware Masonry

Search, Discover, Saved, taxonomy galleries, related memes, Trends, and Timeline use the same aspect-ratio-preserving measured masonry behavior. Cards stay in one flat DOM list in backend order. Their visual placement is also rank-aware: rank 1 is top-left, a later card's top edge never appears above an earlier card's top edge, and cards sharing a top edge follow rank from left to right. This retains relevance and keyboard order while avoiding the large row gaps produced by mixed image, GIF, video, and text heights.

Server rendering emits the complete semantic list in backend order. With JavaScript enabled, cards are not shown first in a fallback grid and then visibly rearranged during hydration; initial measurement and coordinates are applied before the cards are revealed together. Without JavaScript or measurement support, the ordered responsive grid remains usable. Infinite loading appends only unseen meme IDs, keeps existing cards in place, and reveals each appended batch only after it has been measured.

## Saved Library and Account

### Saved (`/library`)

Saved content lives at `/library`, with in-page navigation for **Favorites**, **Collections**, and **Pins**. It owns:

- the active save destination selector;
- collection cards with access state and saved-meme counts;
- favorite and pinned-meme grids;
- pin ordering with drag support plus keyboard-safe Up/Down controls; and
- the collapsed New collection form for eligible full accounts.

The library is the place to create and manage saved content. Guests retain Favorites and receive an appropriate connection path when a full-account capability is needed.

### Account (`/profile`)

`/profile` is deliberately limited to account connection and preferences: Telegram connection status/entry point, language preference, sensitive-content preference, and an optional collapsed interaction-statistics summary. It links to Saved but does not duplicate favorite grids, pins, collection management, active save selection, or provider diagnostics.

## Collections (`/collection/{id}`)

Collection pages are content first. They show collection context, active-save status/control when permitted, feedback notices, and saved memes before administrative controls.

Rename/visibility changes, invitations, member roles, revocation, and deletion are contained in an accessible **Manage collection** disclosure after the grid. The disclosure is capability- and role-aware; existing invitations, membership checks, and server actions remain authoritative.

### Contextual Selection

Bulk behavior is available only on capable Saved, Search, and collection grids. A quiet **Select items** entry starts a local selection mode. Only then do checkboxes and the sticky selection toolbar appear. The toolbar can save selected memes, add them to a permitted collection, remove them from the current collection when allowed, or download selected media.

Selection mode is never shown by default on Discover. Clear, Done, and successful destructive removal exit the mode so management controls do not persist while browsing.

## Meme, Tag, and Template Pages

### Meme Detail (`/memes/{slug}` or `/memes/{id}`)

Meme detail is media first. Desktop uses a media area with a compact sticky context/action area; mobile places Favorite, Save, and Send immediately below the media. The visible context is limited to title, concise description, tags, and related memes. Title, lead description, long-form body, and OCR text are compared after Unicode compatibility normalization, trimming, whitespace collapse, and locale-independent lowercasing; an exact normalized lower-priority duplicate is suppressed. SEO description and media alt text remain independent metadata.

OCR text, long-form context, and popularity information are progressively disclosed in **About this meme**. MIME types, byte sizes, file rows, public API explanations, raw popularity scores, and internal similarity/fallback diagnostics are not consumer-facing. Related memes preserve their actual discovery attribution while presenting a simple continuation path.

A separate full-width **Sources & activity** disclosure follows the meme
article and precedes Related memes. Its summary reports the number of observed
Telegram posts and channels plus known views. Inside, every public-crawler post
is available through stable pagination and can be sorted by views, reactions,
reposts, interaction rate, newest, or oldest. Missing counters render as not
captured, unavailable posts remain labeled historical rows, and only validated
public `t.me` channel/post links are clickable.

**Professional analytics** is nested within that disclosure. It provides
URL-backed 7/30/90-day and All controls, an original-source versus MemeExpert
activity chart, a selectable server-bucketed absolute Telegram counter chart,
exact-value tables, source rates with eligibility coverage, separate web and
Telegram inline funnels, and forward-only channel-audience comparisons. The
activity chart compares adaptive buckets as signals per day while its table
retains exact bucket totals. Both charts use UTC time axes; each absolute-source
point uses the last real capture time represented by its server bucket. A
missing selected Telegram counter breaks the line instead of becoming zero.
Detail, sources, analytics, and related memes load independently; one
unavailable projection must not erase the others. Clean share/canonical meme
URLs omit disclosure, sorting, pagination, snapshot, and analytics-range state.

Related memes use the same rank-aware masonry presentation as Search in an
SSR-first infinite feed. The server renders the first 12 results and the
browser appends 12-result pages without reordering cards already shown, up to
the API's stable bounded pool of 200 candidates. Automatic intersection
loading retains an accessible **Load more** control plus loading, retry, empty,
and end states. The feed does not expose bulk selection, and moving to another
meme resets it to that source.

The API owns semantic, tag-related, same-template, and public-popular
degradation; the frontend never invents or re-ranks fallback results. Every
appended card preserves the attribution and global rank returned by the API.
Detail links in this grid use tap-only data preloading so pointer hover cannot
launch several expensive detail-page loads before the user chooses a meme.

Tag and template pages likewise put the gallery before aggregate popularity information. Any available recent activity and history appear in an **About this tag** or **About this template** disclosure after the gallery.

## Trends (`/trends`)

Trends are public, story-led discovery surfaces with **Trending**, **Rising**, and **Most favorited** rankings. Each ranked meme shows media, rank, direction, understandable recent activity, and a recent change rather than raw ranking scores or history-sufficiency diagnostics. Comparison and timeline are secondary actions.

**Recorded activity** is the public activity label used in meme analytics,
trend summaries, tag/template history, comparisons, and timeline cards. It is
an unweighted sum of available original-source view, reaction, and repost
increases plus MemeExpert views, sends, saves, and favorite actions. It counts
signals, not unique people, and is not the backend ranking/popularity score.
Comments, impressions/results served, downloads, and subscriber counts are
reported separately and never enter Recorded activity. A counter correction
does not create negative activity, and recovery to a previously observed high
does not count the same activity twice. Downloads remain visible as adjacent
metrics but contribute no weight to public trend rankings or Meme of the Day
scoring.

- Timeline supports month/year nostalgia browsing while retaining URL controls and pagination.
- Comparison accepts labeled meme, tag, and template rows and remains shareable through its existing URL serialization. Charts include an adjacent readable table of the same values.
- Trend charts and tables distinguish original-source and MemeExpert contributions and use consumer-friendly empty states; they do not invent a line when there are not enough recorded moments.

## Telegram Mini App Host Shell

The Mini App keeps the same routes and consumer actions while adapting the shell to Telegram:

- Telegram theme colors, viewport values, dark/light scheme, and safe-area insets drive the canvas, surfaces, text, actions, and bottom navigation placement.
- Redundant website brand, account, and sign-in controls are hidden; the compact route shell and prominent Send action remain.
- Startup calls Telegram readiness/expand hooks, authenticates `initData` through the existing Mini App flow, and routes supported invite/meme start parameters once. If host authentication is unavailable, current web-session browsing remains available.

## Accessibility and Interaction Requirements

- Navigation has named landmarks, visible mobile labels, active-page state, and keyboard-visible focus treatment.
- Card media/detail links, direct actions, overflow menus, selection controls, dialogs, disclosures, and live action status are keyboard reachable and labeled.
- Grids retain list semantics and rank/position metadata where available. Flat DOM order keeps keyboard and assistive-technology traversal aligned with backend ranking on every masonry surface.
- Infinite feeds retain loading, retry, count, end-state, and accessible **Load more** behavior even when automatic intersection loading is available.
- Charts provide titles/descriptions and adjacent tables or summaries for exact values. Dialogs and mobile filter sheets must remain operable with keyboard and assistive technology.

## Advertising

Ad slots in meme grids are deferred from the initial production release. The layout should not block future insertion of meme-sized ad cards, but no Yandex Direct / AdSense integration is required for MVP.
