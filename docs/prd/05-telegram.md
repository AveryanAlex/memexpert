# Telegram Bot

## Inline Mode

`@memexpertbot <query>` in any chat.

- First inline query resolves the Telegram user by `telegram_id`; if no user exists, a full account is created automatically.
- Searches public memes plus the user's accessible private/shared collections.
- Scrollable grid of thumbnails, pagination on scroll.
- Tap → sends as photo (images) or animation (GIFs). Sent as a plain image, no buttons, no branding.
- Videos excluded from inline.
- Empty query: pins → personalized/recent sends → trending (full accounts); trending only until enough personal history exists.
- Inline results served and chosen/sent outcomes carry stable exposure keys for
  analytics and recommendations. Public funnel rates use only keyed results;
  unkeyed legacy events remain lower-confidence totals and are never matched by
  user identity or time proximity.

MVP implementation note: inline answers can reuse cached Telegram Bot API `file_id`s and public HTTPS media URLs. First-send upload from private object storage is deferred until the bot has a presigned/public media URL or a proactive upload/cache warmup path.

## Direct Messages (Bot PM)

Auto-creates full account on first interaction.

**Features:**

- **Favorites:** browse, remove
- **Pins:** add, remove, reorder (up to 20)
- **Active save collection:** set which collection receives forwarded memes (default: Favorites)
- **Collections:** create, browse, delete, manage members via invite links
- **Collection invites:** create invite links, accept invite links, list membership role where useful
- **Upload:** send image → saved to active collection
- **Search:** public + accessible private/shared collections
- **Settings:** NSFW default, language
- **Suggest channel:** form to submit a channel for crawling review
- **Account linking:** link web account, view linked providers
- **User stats:** fun statistics (memes sent, saved, downloaded, days active, favorite tag/template)

## Quick Save

Forward any meme to the bot → bot recognizes it → immediately saves to the **active save collection** (no questions). Reply: "✅ Saved to {collection name}" with a button to change collection. If not in the database → saves as user upload.

## Telegram Mini App

Website registered as TG Mini App, accessible via:

- Button in bot PM
- Direct link: `t.me/memexpertbot/app`
- Collection invite links: `t.me/memexpertbot/app?startapp=invite_XXXXX`
- Meme share links: `t.me/memexpertbot/app?startapp=meme_XXXXX`

Provides:

- Same SvelteKit frontend shell as the website, adapted to Telegram viewport/theme where needed
- Seamless auth via Telegram `initData`, creating/resolving a full account
- Full collection management with bulk actions and invites
- Meme browsing with personalized feed
- Search with filters, including collection scope
- All content visible according to user settings (including content that may later be hidden on the public web by sensitivity policy)
- Meme editor (V2)
- User stats

## Share from Website

Meme pages include a "Share to Telegram" button. Opens a Telegram share dialog — user picks a chat, the meme is shared. Implementation: standard `https://t.me/share/url?url=...` with meme page link, or a deep link to the Mini App for richer experience.

## Public Telegram Attribution

Public meme pages attribute every observed Telegram post attached to any file
of the meme, but only when its provenance is `source_kind=public_crawler`.
Uploader/operator sources, crawler account/session identity, source text, raw
platform IDs, and forwarded-original identity never enter the public response.
When a tracked channel still has a valid public username, the page links to the
channel and exact post. A deleted or inaccessible post remains in historical
attribution and is marked unavailable rather than silently disappearing.

Telegram views, reactions, comments, and reposts are independently nullable.
Missing means Telegram did not expose the counter; it is not rendered or
aggregated as a known zero. Subscriber counts are forward-only observations
from `channels.getFullChannel` during source creation, crawler refresh, and
daily capture. They support coverage-qualified per-post and
per-1,000-subscriber comparisons, not claims of unique reach or reconstructed
historical audience.
