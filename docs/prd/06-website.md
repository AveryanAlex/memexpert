# Website

## Technology

SvelteKit SSR. Responsive. Serves as the Mini App too.

## Pages

### App Shell Navigation

The website uses a responsive application shell: desktop top navigation with global search, and mobile bottom tabs for For You, Trends, and Profile. The global search is available from primary browsing surfaces and exposes URL-backed filters for tags, NSFW, media type, language, search scope, and collection ids.

### Home Page

"Meme of the Day" featured section + personalized For You feed for users with history + trending/cold-start memes in a masonry grid. Category/tag chips remain available for quick exploration through global search and Search workspace filters.

### Meme Page (`/memes/{slug}` or `/memes/{id}`)

- Meme (image / GIF / video)
- Caption and body text (if SEO content exists)
- **Like count** (number of users who favorited this meme) — displayed publicly
- Tags (clickable → tag pages)
- Template link (if identified) → template page
- Source info, popularity chart (sparkline)
- Action buttons: Like (Favorites), Save to Collection, Pin, Share (including TG), Download
- Similar memes grid powered by embedding similarity, with fallback attribution if the system falls back to tags/trending
- Personalized / trending feed below

### Search Results (`/search?q=...`)

Masonry grid, infinite scroll. Filter sidebar includes tags, NSFW, media type, language, search scope (public/common, private/shared, all), and specific collection multi-select for private/shared collections the user can access.

#### Public Feed Ordering Policy

Public meme feeds consume the backend-ranked array sequentially. On desktop/tablet masonry, each result is placed into the current shortest estimated column, with ties going to the earlier column, so assignment is deterministic and does not shuffle the backend response. Top-down visual scanning under this policy should encounter earlier/higher-ranked results before later/lower-ranked results, while still reducing mixed image/GIF/video height gaps.

On mobile one-column layouts, the masonry algorithm preserves the exact backend order. Infinite loading appends only unseen meme IDs in backend page order, so duplicate results from overlapping pages do not move already-rendered cards. Tag, template, and collection-specific scope policies are deferred unless those pages explicitly document a different ranking contract.

### Tag Pages (`/tags/{slug}`)

SEO landing pages. Description + meme grid sorted by popularity. Seasonal tags auto-populated (e.g., `/tags/new-year` fills with holiday memes when tagged by AI).

### Template Pages (`/templates/{slug}`)

Template name, description, meme gallery. Always by slug. Popularity analytics.

### Collection Page (`/collection/{id}`)

Members-only. Grid with **bulk management** (multi-select → add to another collection / remove). Member management. Invite link.

### Profile Page (`/profile`)

Favorites, collections, pins. Account linking. Settings (NSFW default, language). **User stats**: memes sent, saved, viewed, downloaded, days active, top tags/templates. User-initiated account deletion and data export are deferred.

### Trends Page (`/trends`)

Public analytics:

- Trending memes this week
- Fastest-rising memes
- Most liked/shared/downloaded memes
- **Template popularity over time** — how templates rise and fall
- **Tag/theme trends** — seasonal patterns, category dynamics
- **Trend comparison** — overlay multiple memes/templates/tags on one chart ("Google Trends for memes")
- **Meme timeline** — top memes by month/year, nostalgia browsing

## Web Collection & Bulk Management

- **Multi-select mode** in any grid (collection, favorites, search results)
- **Bulk actions:** add to collection, remove, download
- **Drag-and-drop reorder** for pins
- **Collection settings panel**
- **Invite management:** create/copy invite links and show member roles

## Advertising

Ad slots in meme grids are deferred from the initial production release. The layout should not block future insertion of meme-sized ad cards, but no Yandex Direct / AdSense integration is required for MVP.
