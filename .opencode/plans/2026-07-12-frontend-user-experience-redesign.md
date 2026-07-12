# Frontend User Experience Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild MemeExpert's public frontend around the fast discover → search → save/send → continue-discovering loop, while preserving existing API contracts, SSR, SEO, access control, telemetry, and deep links.

**Architecture:** Replace the current dark dashboard shell and debug-heavy route layouts with a media-first responsive application shell. Shared visual primitives and navigation are established first; route work is then split into non-overlapping home/feed, search, library/profile, collections, detail/SEO, and trends workstreams. Existing server loads/actions remain authoritative; the redesign changes presentation and progressive disclosure rather than backend behavior.

**Tech Stack:** SvelteKit 2, Svelte 5 runes, TypeScript, Tailwind CSS v4, Bits UI wrappers, Lucide Svelte, LayerChart, Vitest SSR render tests, Playwright smoke tests.

---

## Product and UX contract

- The first mobile viewport must contain meme media, not marketing copy or management controls.
- Global navigation prioritizes Discover, Search, Saved, and Account. Trends remain discoverable without replacing Search or Saved.
- Every meme card exposes Favorite, Save, and Send without requiring the overflow menu. Pin, Download, and Report stay in overflow.
- Search state remains URL-backed, but implementation terms such as `scope`, `collection_ids`, API state, fallback algorithms, scores, and candidate counts are not user-facing.
- Discovery may use a dense aspect-ratio-preserving masonry layout. Search and mobile layouts must preserve backend order and readable meme text.
- Bulk actions are opt-in selection mode and never appear on Home by default.
- Profile contains account/preferences. Saved content moves to `/library` with Favorites, Collections, and Pins views.
- Collection pages show memes before management. Management is collapsed behind an explicit control.
- Meme details prioritize media, Favorite/Save/Send, concise context, and related memes. Technical information is removed or placed in a collapsed “About this meme” section.
- Telegram Mini App uses Telegram colors and safe areas, hides redundant website chrome, and keeps Send prominent.
- Consumer-facing copy defaults to concise English in this implementation so the repository remains internally consistent; all new labels are centralized or component-local and written so a later Russian locale can replace them without exposing backend terminology.

## Execution graph and file ownership

1. Task 1 is foundational and runs first.
2. After Task 1, Tasks 2–7 may run in parallel because their write sets do not overlap.
3. Task 8 integrates the work, updates docs/smoke fixtures, and is run only after Tasks 2–7 finish.
4. Agents must not modify admin routes or admin feature components.
5. Agents must not commit; repository policy requires an explicit separate commit request.

## Task 1: Design system, application shell, and Telegram host shell

**Files:**
- Modify: `frontend/src/app.css`
- Modify: `frontend/src/lib/ui/styles.ts`
- Modify: `frontend/src/lib/ui/Button.svelte`
- Modify: `frontend/src/lib/ui/ActionLink.svelte`
- Modify: `frontend/src/lib/ui/Card.svelte`
- Modify: `frontend/src/lib/ui/PageShell.svelte`
- Modify: `frontend/src/lib/ui/PageHeader.svelte`
- Modify: `frontend/src/lib/features/app-shell/navigation.ts`
- Modify: `frontend/src/lib/features/app-shell/GlobalSearch.svelte`
- Modify: `frontend/src/lib/features/app-shell/AppShell.svelte`
- Modify: `frontend/src/routes/+layout.svelte`
- Modify: `frontend/src/lib/app-shell-render.test.ts`
- Test: `frontend/tests/smoke/telegram-miniapp.spec.ts`

- [x] **Step 1: Change the shell SSR expectations before implementation**

  Update `app-shell-render.test.ts` to assert desktop Discover/Search/Saved/account navigation, mobile icon-plus-label tabs, one sign-in action, no global filter dropdown, and admin visibility only for admins. The central assertions should be equivalent to:

  ```ts
  expect(body).toContain('Discover');
  expect(body).toContain('href="/search"');
  expect(body).toContain('href="/library"');
  expect(body).toContain('aria-label="Mobile navigation"');
  expect(body).not.toContain('More filters');
  expect(body.match(/>Sign in</g)).toHaveLength(1);
  ```

- [x] **Step 2: Run the focused test and confirm the old shell fails it**

  Run: `pnpm test -- src/lib/app-shell-render.test.ts`

  Expected: FAIL because the old shell renders For You/Trends/Profile, the filter dropdown, and duplicate sign-in text.

- [x] **Step 3: Replace the visual tokens and fix link color cascading**

  Define a neutral canvas, white surface, near-black text, muted gray, restrained blue accent, subtle border, and overlay shadows in `app.css`. Move the global anchor reset into a Tailwind base layer so component utility colors win:

  ```css
  @layer base {
    a { color: inherit; }
  }
  ```

  Remove the navy radial-gradient dependency from the public shell. Preserve Telegram CSS variables, add `env(safe-area-inset-*)`, and provide complete light/dark surface tokens rather than white panels on a navy canvas.

- [x] **Step 4: Normalize shared primitives**

  Reduce default radii and padding in Button, ActionLink, Card, PageShell, and PageHeader. Keep prop forwarding and focus rings. Use a 12px card radius, 14–18px overlay radius, restrained borders, and shadows only for elevated elements.

- [x] **Step 5: Implement the new desktop and mobile navigation**

  `PRIMARY_NAV_ITEMS` becomes Discover (`/`), Search (`/search`), Saved (`/library`), and Account (`/profile`). Render Lucide icons and visible labels on mobile. Desktop uses brand, Discover, a central `GlobalSearch`, Saved, and a single account/sign-in control. Keep Admin conditional and accessible.

- [x] **Step 6: Simplify global search**

  Keep one GET form targeting `/search` with query input and submit control. Remove the filters dropdown and raw collection/tag fields. On narrow screens, allow the field to collapse into a clear Search entry point without hiding the dedicated Search tab.

- [x] **Step 7: Add Telegram host-shell behavior**

  Under `html.telegram-miniapp`, hide redundant brand/sign-in chrome, apply Telegram theme variables to canvas/surfaces/text, preserve a compact route shell, and reserve Telegram safe areas. Do not change Mini App authentication behavior.

- [x] **Step 8: Run focused checks**

  Run: `pnpm test -- src/lib/app-shell-render.test.ts src/lib/telegram-miniapp.test.ts`

  Expected: PASS.

## Task 2: Media-first cards, contextual bulk mode, and Discover home

**Files:**
- Modify: `frontend/src/lib/features/memes/MemeActionMenu.svelte`
- Modify: `frontend/src/lib/features/memes/MemeCard.svelte`
- Modify: `frontend/src/lib/features/memes/MemeGrid.svelte`
- Modify: `frontend/src/lib/features/memes/InfiniteMemeFeed.svelte`
- Modify: `frontend/src/lib/features/memes/MemeOfTheDayPanel.svelte`
- Modify: `frontend/src/routes/+page.svelte`
- Modify: `frontend/src/lib/features/memes/meme-card-render.test.ts`
- Modify: `frontend/src/lib/features/memes/meme-action-menu-context.test.ts`
- Modify: `frontend/src/lib/home-page-render.test.ts`
- Modify: `frontend/tests/smoke/catalog.spec.ts`

- [x] **Step 1: Write card and Home rendering expectations**

  Assert that cards render accessible Favorite, Save, and Send controls; dimensions/language/tags are not always visible; Home does not render collection creation or a permanent bulk toolbar; and the feed/Meme of the Day appears before secondary navigation copy.

  ```ts
  expect(body).toContain('Favorite');
  expect(body).toContain('Save');
  expect(body).toContain('Send');
  expect(body).not.toContain('640x360');
  expect(body).not.toContain('Algorithm');
  expect(body).not.toContain('Bulk actions');
  ```

- [x] **Step 2: Run focused tests and confirm failure**

  Run: `pnpm test -- src/lib/features/memes/meme-card-render.test.ts src/lib/features/memes/meme-action-menu-context.test.ts src/lib/home-page-render.test.ts`

  Expected: FAIL against the current metadata-heavy cards and Home.

- [x] **Step 3: Add explicit action surfaces to `MemeActionMenu`**

  Add a typed surface prop:

  ```ts
  type MemeActionSurface = 'card' | 'detail' | 'overflow';
  ```

  For `card`, render compact Favorite, Save, and Send controls plus overflow. For `detail`, render labeled primary actions plus overflow. Keep existing API calls, telemetry, viewer capabilities, report form, keyboard behavior, and live status messaging. Use “Favorite” consistently rather than mixing Like and Favorite.

- [x] **Step 4: Recompose `MemeCard` around media**

  Make only the media/title area navigate to detail. Remove the large bordered metadata footer. Preserve full media aspect ratio and access markers. Show a concise optional caption and the card action row. Keep impression/detail telemetry and rank-order ARIA attributes.

- [x] **Step 5: Make bulk selection contextual**

  Add local `selectionMode` state to `MemeGrid`. When bulk capability exists, render a quiet “Select items” entry. Render checkboxes and the sticky contextual toolbar only after selection mode starts. Exit selection mode after Clear or successful destructive collection removal. Home passes bulk disabled.

- [x] **Step 6: Quiet infinite-feed chrome**

  Keep loading, retry, accessible Load more fallback, counts, and end state, but reduce panel styling and operational explanations. Do not expose attribution/fallback text visually; retain data attributes and telemetry.

- [x] **Step 7: Rebuild Home as Discover**

  Remove the giant hero, collection list/form, and cold-start/backend explanations. Render a compact heading/tabs row, a simplified Meme of the Day media tile, optional topic shortcuts, then the feed. Ensure a 390×844 viewport contains meme media.

- [x] **Step 8: Update smoke behavior**

  Update catalog smoke expectations so card actions are visible, selection controls appear only after entering Select mode, keyboard order remains deterministic, and Load more remains accessible.

- [x] **Step 9: Run focused tests**

  Run: `pnpm test -- src/lib/features/memes src/lib/home-page-render.test.ts`

  Expected: PASS.

## Task 3: Search workspace and responsive filter sheet

**Files:**
- Create: `frontend/src/lib/features/search/SearchFilters.svelte`
- Create: `frontend/src/lib/features/search/ActiveSearchFilters.svelte`
- Modify: `frontend/src/routes/search/+page.svelte`
- Modify: `frontend/src/lib/search-page-render.test.ts`
- Modify: `frontend/src/lib/searchParams.test.ts`

- [x] **Step 1: Define consumer-language SSR expectations**

  Assert one query field, a Filters trigger, active filter chips, “Where to search,” named collection choices, and absence of raw parameter explanations.

  ```ts
  expect(body).toContain('Filters');
  expect(body).toContain('Where to search');
  expect(body).not.toContain('collection_ids');
  expect(body).not.toContain('URL-backed filter workspace');
  ```

- [x] **Step 2: Run the focused tests and confirm failure**

  Run: `pnpm test -- src/lib/search-page-render.test.ts src/lib/searchParams.test.ts`

  Expected: FAIL because the current route permanently displays the advanced form and parameter names.

- [x] **Step 3: Extract `SearchFilters`**

  Move the existing URL-backed form behavior, NSFW preference gate inputs, media/language controls, search scope, and named collection checkboxes into a feature component. Present scope labels as Everywhere, Public memes, My saved memes, and Specific collections. Never accept raw collection IDs through a visible text field.

- [x] **Step 4: Implement responsive disclosure**

  Desktop uses a right-side dialog/drawer no wider than 380px. Mobile uses a bottom sheet capped near `85dvh`, internally scrollable, with sticky Reset and Show results actions. Forward form submit and preserve the existing NSFW confirmation behavior.

- [x] **Step 5: Implement active chips**

  `ActiveSearchFilters` renders removable URL links for media, language, tags, content sensitivity, scope, and selected collection titles. Empty search shows recent-style suggested intents and quick categories without pretending local history exists.

- [x] **Step 6: Recompose `/search`**

  Remove the giant Search Workspace hero. Keep a compact page title/query row, chips, results count, and results immediately afterward. Bulk capability remains available only through `MemeGrid` selection mode.

- [x] **Step 7: Run focused tests**

  Run: `pnpm test -- src/lib/search-page-render.test.ts src/lib/searchParams.test.ts`

  Expected: PASS.

## Task 4: Saved library route and focused account profile

**Files:**
- Create: `frontend/src/lib/server/libraryPage.ts`
- Create: `frontend/src/routes/library/+page.server.ts`
- Create: `frontend/src/routes/library/+page.svelte`
- Create: `frontend/src/lib/library-page-render.test.ts`
- Modify: `frontend/src/routes/profile/+page.server.ts`
- Modify: `frontend/src/routes/profile/+page.svelte`
- Modify: `frontend/src/lib/profile/profile-page-render.test.ts`
- Reuse without modification: `frontend/src/lib/features/profile/LibrarySection.svelte`

- [x] **Step 1: Add rendering tests for route separation**

  The library test asserts Favorites, Collections, Pins, active save destination, and meme grids. The profile test asserts account connection, language, sensitive-content preference, optional compact stats, and absence of favorites/pins grids.

- [x] **Step 2: Run focused tests and confirm missing route/failing expectations**

  Run: `pnpm test -- src/lib/library-page-render.test.ts src/lib/profile/profile-page-render.test.ts`

  Expected: FAIL because `/library` and its test do not exist and Profile still contains all library content.

- [x] **Step 3: Extract the shared library server loader**

  Implement:

  ```ts
  export async function loadLibraryPage(request: BackendPageRequest): Promise<{
    library: MemeLibraryRead | null;
    libraryError: string | null;
  }>;
  ```

  Reuse current cookie forwarding and `fetchMemeLibrary`. Keep errors independent from profile stats.

- [x] **Step 4: Create `/library`**

  Render compact tabs/anchors for Favorites, Collections, and Pins, collection cards with title/count/access state, active-save selector, contextual bulk selection, and the existing accessible pin reorder controls. Do not place account provider diagnostics on this route.

- [x] **Step 5: Simplify `/profile`**

  Load profile stats only. Render a compact account header, one Telegram connection row, language preference, sensitive-content preference, and collapsed/compact stats. Remove library, active save, collection list, favorites, and pin ordering.

- [x] **Step 6: Run focused tests**

  Run: `pnpm test -- src/lib/library-page-render.test.ts src/lib/profile/profile-page-render.test.ts src/lib/profile/view-model.test.ts`

  Expected: PASS.

## Task 5: Collection-first detail with collapsed management

**Files:**
- Create: `frontend/src/lib/features/collections/CollectionManagement.svelte`
- Modify: `frontend/src/routes/collection/[id]/+page.svelte`
- Modify: `frontend/src/lib/collection-page-render.test.ts`

- [x] **Step 1: Change the collection render test**

  Assert that the title/save-destination action and saved memes appear before a “Manage collection” disclosure, while rename, invitations, members, and deletion remain present for capable roles inside that disclosure.

- [x] **Step 2: Run the focused test and confirm failure**

  Run: `pnpm test -- src/lib/collection-page-render.test.ts`

  Expected: FAIL because management currently precedes the meme grid.

- [x] **Step 3: Extract management UI**

  Move rename/visibility, invite creation, invite rows, member roles/removal, and danger-zone forms into `CollectionManagement.svelte`. Preserve every server action name, hidden input, capability condition, clipboard fallback, and success/error surface.

- [x] **Step 4: Recompose collection detail**

  Render compact back/title/count/access metadata, active-save control, feedback notices, then saved memes. Add an accessible `<details>` management disclosure after the grid. Keep bulk removal available through contextual selection mode.

- [x] **Step 5: Run focused tests**

  Run: `pnpm test -- src/lib/collection-page-render.test.ts src/lib/features/collections/view-model.test.ts`

  Expected: PASS.

## Task 6: Consumer meme detail and SEO discovery surfaces

**Files:**
- Modify: `frontend/src/routes/memes/[id]/+page.svelte`
- Modify: `frontend/src/routes/tags/[tag]/+page.svelte`
- Modify: `frontend/src/routes/templates/[slug]/+page.svelte`
- Modify: `frontend/src/lib/meme-detail-page-render.test.ts`

- [x] **Step 1: Rewrite detail rendering expectations**

  Assert media, Favorite/Save/Send, concise title/description, tags, collapsed “About this meme,” and related discovery. Assert absence of MIME byte lists, public API explanations, and visible internal score.

  ```ts
  expect(body).toContain('About this meme');
  expect(body).toContain('Related memes');
  expect(body).not.toContain('Media and file info');
  expect(body).not.toContain('Only fields exposed by the public meme detail API');
  expect(body).not.toContain('score 42.5');
  ```

- [x] **Step 2: Run the focused test and confirm failure**

  Run: `pnpm test -- src/lib/meme-detail-page-render.test.ts`

  Expected: FAIL against the current technical detail page.

- [x] **Step 3: Recompose meme detail**

  Desktop uses a media column and compact sticky action/context column. Mobile places primary actions directly below media. Keep telemetry, action forms, Telegram connection prompt, tags, popularity data, and related results, but place OCR/source/popularity in a collapsed About section. Remove file rows, MIME types, bytes, and score from consumer presentation.

- [x] **Step 4: Make tag/template pages gallery-first**

  Keep canonical metadata and analytics data, but render concise title/context followed by meme media. Put aggregate analytics after the gallery or inside disclosure. Remove diagnostic empty-state language.

- [x] **Step 5: Run focused tests**

  Run: `pnpm test -- src/lib/meme-detail-page-render.test.ts`

  Expected: PASS.

## Task 7: Story-led trends, timeline, and comparison

**Files:**
- Modify: `frontend/src/lib/features/trends/TrendSummary.svelte`
- Modify: `frontend/src/lib/features/trends/TrendAggregateHistory.svelte`
- Modify: `frontend/src/lib/features/trends/TrendComparisonChart.svelte`
- Modify: `frontend/src/routes/trends/+page.svelte`
- Modify: `frontend/src/routes/trends/timeline/+page.svelte`
- Modify: `frontend/src/routes/trends/compare/+page.svelte`
- Modify: `frontend/src/lib/trend-timeline-page-render.test.ts`
- Modify: `frontend/src/lib/trend-compare-page-render.test.ts`
- Test: existing trend component tests under `frontend/src/lib/features/trends/`

- [x] **Step 1: Add consumer-copy assertions**

  Assert understandable periods and deltas such as “this week,” visual ranking labels, accessible charts/tables, and absence of “history points,” “current window only,” raw scores, and typed-spec instructions.

- [x] **Step 2: Run focused tests and confirm failure**

  Run: `pnpm test -- src/lib/trend-timeline-page-render.test.ts src/lib/trend-compare-page-render.test.ts src/lib/features/trends`

  Expected: FAIL where current copy exposes aggregate diagnostics and comparison syntax.

- [x] **Step 3: Rebuild the trends landing hierarchy**

  Use tabs Trending, Rising, and Most favorited. Each ranked row/card shows media, title, direction, and a comprehensible recent change derived from available recent/previous counts. Keep comparison and timeline as secondary actions. Hide raw trend scores and history sufficiency diagnostics.

- [x] **Step 4: Simplify timeline**

  Present it as nostalgia browsing by month/year with visual top memes. Keep URL controls and pagination, but remove materialization/snapshot language.

- [x] **Step 5: Simplify comparison**

  Preserve existing URL serialization and chart data contracts. Replace typed-spec jargon with labeled item type + identifier/name rows and selected chips. Keep the accessible fallback table adjacent to the chart.

- [x] **Step 6: Run focused tests**

  Run: `pnpm test -- src/lib/trend-timeline-page-render.test.ts src/lib/trend-compare-page-render.test.ts src/lib/features/trends`

  Expected: PASS.

## Task 8: Integration, product documentation, and browser acceptance

**Files:**
- Modify: `docs/prd/06-website.md`
- Create: `docs/tech-design/frontend-user-experience.md`
- Modify: `frontend/tests/smoke/catalog.spec.ts` if integration changes require final selector alignment
- Modify: `frontend/tests/smoke/telegram-miniapp.spec.ts` if integration changes require final selector alignment
- Modify: only conflicting public frontend files discovered during integration

- [x] **Step 1: Review the complete diff for ownership violations and route contract drift**

  Run: `git status --short && git diff --stat && git diff -- frontend/src frontend/tests docs/prd/06-website.md docs/tech-design`

  Expected: only the planned public frontend, tests, plan, and documentation files are changed; admin files are untouched.

- [x] **Step 2: Update product documentation**

  Document the new navigation, Discover-first hierarchy, Saved/Profile separation, contextual selection, responsive filter disclosure, collection management disclosure, detail progressive disclosure, and Telegram host shell in `06-website.md`.

- [x] **Step 3: Add the frontend UX technical design**

  Record component boundaries, route ownership, responsive behavior, state/URL invariants, telemetry preservation, accessibility rules, and the reason discovery and search use different grid treatments.

- [x] **Step 4: Run static checks**

  Run: `pnpm check`

  Expected: 0 errors and 0 warnings.

- [x] **Step 5: Run the complete frontend unit suite**

  Run: `pnpm test`

  Expected: all Vitest tests pass.

- [x] **Step 6: Build the production frontend**

  Run: `pnpm build`

  Expected: SvelteKit adapter-node build completes successfully.

- [x] **Step 7: Run Playwright smoke coverage**

  Run: `pnpm test:smoke`

  Expected: catalog, search/detail, collection-scoped search, and Telegram Mini App smoke tests pass.

- [x] **Step 8: Perform browser visual acceptance**

  Capture and inspect `/`, `/search?q=cat+reaction`, and `/memes/smoke-test-cat-reaction` at 1440×1000 and 390×844 using the smoke mock API. Verify:

  - meme media appears in the initial mobile Discover viewport;
  - mobile navigation never obscures the last interactive content;
  - filter sheet is scrollable and has sticky actions;
  - active navigation and CTAs have visible text;
  - Favorite, Save, and Send are visible on touch cards;
  - no horizontal overflow exists at 320px;
  - keyboard focus reaches media and card actions in deterministic order;
  - Telegram Mini App hides redundant website chrome and respects theme/safe-area variables.

- [x] **Step 9: Inspect final working tree**

  Run: `git status --short --branch && git diff --check`

  Expected: branch is `feat/frontend-redesign`, only intended files are modified, and `git diff --check` reports no whitespace errors.

## Self-review record

- Spec coverage: shell, navigation, Home, Search, cards/actions, contextual bulk mode, Library/Profile split, collection hierarchy, detail, tag/template discovery, trends, Mini App, accessibility, docs, and browser acceptance are each assigned to a task.
- Placeholder scan: the plan contains no deferred implementation markers; each task names exact files, expected behavior, tests, and commands.
- Type consistency: `MemeActionSurface` is defined once in Task 2; `/library` is introduced in Task 4 and referenced by Task 1 navigation; existing server action names and API contracts remain unchanged.
- Scope boundary: admin UI is explicitly excluded, and backend/database changes are not required.
