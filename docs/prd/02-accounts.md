# Accounts & Authentication

## Account Model

MemeXpert uses **automatic account creation** with frictionless linking.

| Type | Created When | Capabilities | Identity |
|------|-------------|-------------|----------|
| **Guest** | First website visit or unauthenticated web API session | Browse, public/private search for the guest's own data, like via Favorites, view recommendations | Anonymous browser session |
| **Full** | First Telegram bot PM interaction, first inline query, first Mini App launch, or guest links a provider | All features: collections, pins, uploads, sharing, Mini App, private/shared collection search | Telegram ID and/or email/Google |

Key principles:

- **Telegram bot = instant full account.** PM interactions and inline queries resolve by `telegram_id`; if no user exists, a full account is created automatically. No registration step.
- **Website = instant guest.** A browser session can be created automatically on first visit/API use. It tracks views, impressions, favorites, downloads, and recommendation signals.
- **Mini App = instant full account.** Telegram `initData` authenticates the user and creates or resolves the same full account used by the bot.
- **Upgrade = linking.** Guest links Telegram or Google/email → becomes full account. Feels like "connecting," not "signing up."

## Account Linking & Merging

When a guest links to an existing full account (e.g., already used the TG bot), accounts are **merged**: guest's interaction history, favorites, and collection state that can be safely transferred move to the full account. The guest account is then retired/deleted as part of the merge transaction.

**Account splitting is not supported.**

**Linking flows:**

- **Web → Telegram:** "Link Telegram" button → deep link to bot → accounts merge
- **Web → Google/Email:** OAuth / email sign-in → upgrade or merge
- **Telegram → Web:** "Log in with Telegram" widget or Mini App `initData` → full account recognized

## Site-to-Bot Funnel

When a guest user likes or saves a meme on the website, the interface periodically shows a non-intrusive prompt encouraging them to link their Telegram account. The prompt emphasizes the concrete benefit: "Open the bot and this meme will be saved to your Telegram — use it anytime in chats." This drives guest → full account conversion.

## Auth Providers and Session Model

Custom JWT auth in FastAPI: Telegram Login Widget / Mini App `initData` (primary, HMAC verification), Google OAuth, email + password (fallback).

The production auth model is intentionally simpler than the original token-pair design: an HttpOnly cookie carries a signed access/session JWT, and `token_nonce` on the user row provides logout-all/session revocation. Opaque refresh-token storage and rotation are not part of the current MVP design.

## User Interface Language

Russian and English. Language auto-detected from browser/Telegram settings, switchable manually.

## Deferred

User-initiated account deletion and data export are deferred from the initial production release. Schema placeholders may exist, but no launch-blocking user flow is required until the deferred privacy/export work is explicitly picked up.
