# Website

## Technology

SvelteKit SSR. Responsive. Serves as the Mini App too.

## Pages

### Home Page

Search bar + "Meme of the Day" featured section + trending memes in masonry grid. Category/tag chips. Personalized feed for users with history.

### Meme Page (`/meme/{slug}` or `/meme/{id}`)

- Meme (image / GIF / video)
- Caption and body text (if SEO content exists)
- **Like count** (number of users who favorited this meme) — displayed publicly
- Tags (clickable → tag pages)
- Template link (if identified) → template page
- Source info, popularity chart (sparkline)
- Action buttons: Like (Favorites), Save to Collection, Pin, Share (including TG), Download
- Similar memes grid
- Personalized / trending feed below

### Search Results (`/search?q=...`)

Masonry grid, infinite scroll. Filter sidebar (tags, NSFW, media type, language). Ad banners appear periodically in the grid as meme-sized slots.

### Tag Pages (`/tag/{slug}`)

SEO landing pages. Description + meme grid sorted by popularity. Seasonal tags auto-populated (e.g., `/tag/new-year` fills with holiday memes when tagged by AI).

### Template Pages (`/template/{slug}`)

Template name, description, meme gallery. Always by slug. Popularity analytics.

### Collection Page (`/collection/{id}`)

Members-only. Grid with **bulk management** (multi-select → add to another collection / remove). Member management. Invite link.

### Profile Page (`/profile`)

Favorites, collections, pins. Account linking. Settings (NSFW default, language). **User stats**: memes sent, saved, days active, top tags/templates.

### Trends Page (`/trends`)

Public analytics:

- Trending memes this week
- Fastest-rising memes
- Most liked/shared memes
- **Template popularity over time** — how templates rise and fall
- **Tag/theme trends** — seasonal patterns, category dynamics
- **Trend comparison** — overlay multiple memes/templates/tags on one chart ("Google Trends for memes")
- **Meme timeline** — top memes by month/year, nostalgia browsing

## Web Collection & Bulk Management

- **Multi-select mode** in any grid (collection, favorites, search results)
- **Bulk actions:** add to collection, remove, download
- **Drag-and-drop reorder** for pins
- **Collection settings panel**

## Advertising

Banner ads (Yandex Direct / AdSense) integrated into meme grids as meme-sized slots, appearing periodically (e.g., every ~15 items).
