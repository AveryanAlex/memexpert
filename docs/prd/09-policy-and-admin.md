# Policy & Admin

## Content Policy

### NSFW

Auto-classified. Filtered from search and feeds by default. Users enable in settings. No blur — simply filtered.

### Political Content (Deferred)

Planned two-tier sensitivity system (`standard` / `sensitive`) for filtering politically sensitive content on the public website. `sensitive` content would be hidden on the website but shown in the Telegram bot and Mini App. Implementation deferred until a reliable classifier is available — see [Deferred Features](10-deferred.md). Until then, all content is treated as `standard`.

### Copyright

Contact email for takedowns. Source attribution on every meme page.

### Dead Sources

Marked unavailable. Meme stays in database.

---

## Themed Telegram Channel Network

5–10 MemeXpert-owned channels by category (cats, wholesome, science, IT, student life, absurdist). Automated posting 2–4×/day. Selection: tag match + popularity + novelty. Engagement feeds back into channel's content algorithm (not global popularity score). Channel descriptions link to the bot.

---

## Admin Tools

### Meme Management

- **Manual meme merge:** admin selects two or more memes → merges into one (combines sources, keeps best quality media, merges popularity data)
- **Manual meme delete:** remove from database entirely
- **Flag review queue:** memes reported by users

### Template Management

- Create, edit, merge, delete templates
- Override AI-assigned template links
- Curate template metadata (name, description)

### Source Management

- **Channel suggestion queue:** review and approve/reject user-submitted channel suggestions
- Add/remove channels from crawler
- View channel health metrics (last crawl, error rate)

### Content Moderation

- Review reported memes
- Override NSFW / political classification
- Ban patterns (e.g., block a specific pHash)
