# Accounts & Authentication

## Account Model

MemeXpert uses **automatic account creation** with frictionless linking.

| Type | Created When | Capabilities | Identity |
|------|-------------|-------------|----------|
| **Guest** | First website visit | Browse, search, like (Favorites), view recommendations | Anonymous (browser session) |
| **Full** | First TG bot interaction OR guest links a provider | All features: collections, pins, uploads, sharing, Mini App | Telegram ID and/or email/Google |

Key principles:

- **Telegram bot = instant full account.** No registration step.
- **Website = instant guest.** Tracks views, likes, builds recommendation profile. Cannot create collections, upload, or share.
- **Upgrade = linking.** Guest links Telegram or Google/email → becomes full account. Feels like "connecting," not "signing up."

## Account Linking & Merging

When a guest links to an existing full account (e.g., already used the TG bot), accounts are **merged**: guest's view history and favorites transfer to the full account. The guest account is deleted.

**Account splitting is not supported.**

**Linking flows:**

- **Web → Telegram:** "Link Telegram" button → deep link to bot → accounts merge
- **Web → Google/Email:** OAuth / email sign-in → upgrade or merge
- **Telegram → Web:** "Log in with Telegram" widget → full account recognized

## Site-to-Bot Funnel

When a guest user likes or saves a meme on the website, the interface periodically shows a non-intrusive prompt encouraging them to link their Telegram account. The prompt emphasizes the concrete benefit: "Open the bot and this meme will be saved to your Telegram — use it anytime in chats." This drives guest → full account conversion.

## Auth Providers

Custom JWT auth in FastAPI: Telegram Login Widget (primary, HMAC verification), Google OAuth, email + password (fallback).

## User Interface Language

Russian and English. Language auto-detected from browser/Telegram settings, switchable manually.
