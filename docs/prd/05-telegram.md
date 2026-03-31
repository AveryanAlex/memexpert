# Telegram Bot

## Inline Mode

`@memexpertbot <query>` in any chat.

- Scrollable grid of thumbnails, pagination on scroll
- Tap → sends as photo (images) or animation (GIFs). Sent as a plain image, no buttons, no branding.
- Videos excluded from inline
- Empty query: pins → recent sends → trending (full accounts); trending only (guests)

## Direct Messages (Bot PM)

Auto-creates full account on first interaction.

**Features:**

- **Favorites:** browse, remove
- **Pins:** add, remove, reorder (up to 20)
- **Active save collection:** set which collection receives forwarded memes (default: Favorites)
- **Collections:** create, browse, delete, manage members via invite links
- **Upload:** send image → saved to active collection
- **Settings:** NSFW default, language
- **Suggest channel:** form to submit a channel for crawling review
- **Account linking:** link web account, view linked providers
- **User stats:** fun statistics (memes sent, saved, days active, favorite tag/template)

## Quick Save

Forward any meme to the bot → bot recognizes it → immediately saves to the **active save collection** (no questions). Reply: "✅ Saved to {collection name}" with a button to change collection. If not in the database → saves as user upload.

## Telegram Mini App

Website registered as TG Mini App, accessible via:

- Button in bot PM
- Direct link: `t.me/memexpertbot/app`
- Collection invite links: `t.me/memexpertbot/app?startapp=invite_XXXXX`
- Meme share links: `t.me/memexpertbot/app?startapp=meme_XXXXX`

Provides:

- Full collection management with bulk actions
- Meme browsing with personalized feed
- Search with filters
- All content visible (including sensitive political — no web restrictions apply)
- Meme editor (V2)
- User stats

Auth via Telegram `initData` — seamless.

## Share from Website

Meme pages include a "Share to Telegram" button. Opens a Telegram share dialog — user picks a chat, the meme is shared. Implementation: standard `https://t.me/share/url?url=...` with meme page link, or a deep link to the Mini App for richer experience.
