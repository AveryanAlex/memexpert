# Accounts & Auth

## Account Model

| Type | Created When | Capabilities |
|------|-------------|-------------|
| **Guest** | First website visit or unauthenticated web API/session request | Browse, search public content, favorite, build recommendation history |
| **Full** | First Telegram PM interaction, first inline query, first Mini App launch, or guest links a provider | All features: collections, pins, uploads, sharing, private/shared collection search |

- Guest gets a cookie-backed JWT session immediately and an auto-created Favorites collection.
- Full account is created automatically on first Telegram bot interaction or inline query — no registration step.
- Mini App validates Telegram `initData` and creates/resolves the same full account as the bot.
- Upgrade from guest: link Telegram, Google, or email. Feels like "connecting," not "signing up."

## Session Architecture

The current design intentionally uses a simplified cookie-backed access/session JWT model instead of the original access-token + refresh-token pair.

| Token/State | Storage | Purpose |
|-------------|---------|---------|
| Access/session JWT | HttpOnly cookie | API authentication for browser, SvelteKit SSR, and Mini App |
| `token_nonce` | PostgreSQL `users` row | Revocation primitive; bumping it invalidates all outstanding JWTs for that user |
| `LoginEvent` | PostgreSQL audit row | Immutable login/session issuance audit |

Custom JWT auth in FastAPI. No third-party auth service — Telegram Login Widget and Mini App require custom HMAC verification that standard providers don't handle well alongside Google OAuth and email+password.

Access token payload includes `sub` (user ID), account type/state claims, `exp`, `iat`, and `nonce`. Signed with HS256 or configured algorithm.

Auth-aware FastAPI dependencies resolve access/session cookies through a shared auth resolver. A valid, unexpired cookie whose `sub` points at a guest account retired by an `AccountMergeLog` entry is automatically replaced with a cookie for the canonical merged account on that same response. Invalid, expired, revoked, or unrelated deleted-user tokens remain rejected. There is no manual session-refresh endpoint and no `RefreshToken` table in the current MVP design.

## Auth Providers

| Provider | Flow | Verification |
|----------|------|-------------|
| **Telegram Login Widget** | Widget → POST data to API | HMAC verification with bot token |
| **Google OAuth** | OAuth code exchange | Google token endpoint |
| **Email + password** | Registration / login forms | bcrypt hash comparison |
| **Mini App (initData)** | Telegram Mini App launch | HMAC verification with bot token |

## Client Auth Flow

Auth logic lives in the service layer, used by both FastAPI and the aiogram bot directly.

**HTTP clients** (browser, SvelteKit SSR, Mini App, future mobile) — authenticate via FastAPI:

1. **Browser:** HttpOnly access/session cookie. If no valid cookie exists, `/api/v1/auth/session/current` can bootstrap a guest session.
2. **SvelteKit SSR:** server load/hooks forward the cookie to FastAPI calls. The root layout seeds a context-scoped Svelte auth store from the current-session load so account-aware UI has one reactive projection; each SSR request/layout instance is isolated, the store contains no token, and it is resynchronized from server load data after navigation or invalidation. No durable auth state is owned by SvelteKit.
3. **Mini App:** frontend conditionally loads Telegram's `telegram-web-app.js` only when Telegram launch params are present, reads `window.Telegram.WebApp.initData` (falling back to the signed `tgWebAppData` launch parameter when the host script is unavailable), posts it to `/api/v1/auth/telegram-miniapp`, receives the same cookie-backed session, publishes the refreshed current-session projection to the root auth store, then invalidates normal route data.
4. **Future mobile:** may reuse the same provider exchange endpoints, but token transport can be revisited for non-cookie clients.

**Telegram bot** — authenticates via service layer directly. The bot identifies users by `telegram_id` from the Telegram update object (already verified by Telegram). No JWT needed — the bot process is trusted and calls auth/user services to resolve or create the full account.

## Account Linking & Merging

When a guest links to an existing full account (e.g., already used the TG bot), accounts are **merged**:

1. **PostgreSQL transaction** (atomic): transfer guest's Favorites, safe collection state, and interaction history to the full account; write `AccountMergeLog` entry; retire/delete the guest row as appropriate.
2. **Async propagation:** publish `account_merged` event to update search payloads and invalidate caches for both old and new user IDs.
3. Search indexes are eventually consistent — the user should not notice a brief delay before merged favorites/history affect recommendations.

Linking flows:

- **Web → Telegram:** "Link Telegram" button → deep link `t.me/memexpertbot?start=link_{code}` → merge → return to `/account/telegram/complete`, where the normal session load self-heals the cookie and redirects linked sessions onward
- **Web → Google/Email:** OAuth / email sign-in → upgrade or merge
- **Mini App:** validate `initData` HMAC → lookup/create by `telegram_id`

Browser auth and account-preference mutations publish their returned current-session or user projection to the context-scoped auth store before route invalidation. A short reconciliation window prevents an immediately lagging layout response from overwriting that browser-confirmed projection; a matching or newer server snapshot resumes normal server authority. This keeps the application shell, viewer capabilities, and account-aware controls consistent without waiting for a full document reload; FastAPI plus the HttpOnly cookie remain authoritative.

Account splitting is not supported.

## Collection Access Control

Three roles enforced in the service layer and API routes:

| Role | Capabilities |
|------|-------------|
| **Owner** | Full control: edit, delete, manage members, invite |
| **Editor** | Add/remove memes, invite viewers |
| **Viewer** | Read-only |

Sharing via invite links. Shared as Mini App deep links: `t.me/memexpertbot/app?startapp=invite_XXXXX`.

Search must enforce collection access through PostgreSQL before returning DTOs, even if Qdrant/Meilisearch prefilter candidates.

## Deferred: Account Deletion & Data Export

User-initiated account deletion and data export are deferred from the initial production release.

Do not implement the earlier daily hard-delete scheduler job or expose profile deletion/export UI as part of MVP. If deletion-related columns/log tables exist, treat them as reserved schema for future privacy/export work.

## Security

- **CORS:** allow `memexpert.net`, `*.memexpert.net`, localhost web dev origins, and Telegram web origins
- **CSRF:** SameSite cookies + custom header check (`X-Requested-With`) for any unsafe versioned API request with an `Origin` header; safe methods and non-browser clients without `Origin` stay exempt
- **Auth cookie:** HttpOnly, secure in production, SameSite policy configured per deployment, scoped Path=/
- **Revocation:** `token_nonce` bump invalidates outstanding JWTs for a user
- **Input validation:** Pydantic models for all request bodies, Query() constraints for params
- **File uploads:** size limits (10 MB images, 50 MB videos), uploaded through API (not direct-to-S3)
- **Media serving:** CDN with public read for public media; private-media sendability handled by bot/public URL/cache constraints

## Rate Limiting

Redis sliding-window counters in shared FastAPI middleware. User-scoped tiers derive their subject directly from the signed access-cookie JWT `sub` claim when present and valid; otherwise they fall back to client IP. Middleware never hits PostgreSQL for subject resolution.

| Tier | Endpoints | Limit |
|------|-----------|-------|
| `search_feed` | Safe reads on `/api/v1/memes/search`, `/browse`, `/trending`, `/trends`, and `/trends/*` | 30 req/min per signed user, else IP |
| `write` | Remaining unsafe `/api/v1/*` requests | 60 req/min per signed user, else IP |
| `upload` | Unsafe `/api/v1/pipeline/uploads` | 10 req/min per operator identity when a safe non-secret one exists, else IP |
| `auth_write` | Unsafe `/api/v1/auth/*` | 10 req/min per IP |
| `admin` | All `/api/v1/admin/*` requests, including reads | 120 req/min per signed user, else IP |

Other safe reads remain unlimited.
