# Accounts & Auth

## Account Model

| Type | Created When | Capabilities |
|------|-------------|-------------|
| **Guest** | First website visit (automatic) | Browse, search, like (Favorites) |
| **Full** | First TG bot interaction OR guest links a provider | All features: collections, pins, uploads, sharing |

- Guest gets a JWT pair immediately and an auto-created Favorites collection.
- Full account created automatically on first Telegram bot interaction — no registration step.
- Upgrade from guest: link Telegram, Google, or email. Feels like "connecting," not "signing up."

## JWT Architecture

| Token | Lifetime | Storage | Purpose |
|-------|----------|---------|---------|
| Access token | 15 min | Client memory (JS variable) | API authentication |
| Refresh token | 30 days | httpOnly secure cookie | Silent token renewal |

Custom JWT auth in FastAPI. No third-party auth service — Telegram Login Widget requires custom HMAC verification that standard providers don't handle well alongside Google OAuth and email+password.

Access token payload: `sub` (user ID), `type` (guest/full), `exp`, `iat`. Signed with HS256.

Refresh tokens are opaque random strings. Stored in PG as SHA256 hashes. Rotated on each refresh (old token revoked, new token issued). This limits the damage window if a refresh token is intercepted.

## Auth Providers

| Provider | Flow | Verification |
|----------|------|-------------|
| **Telegram Login Widget** | Widget → POST data to API | HMAC verification with bot token |
| **Google OAuth** | OAuth code exchange | Google token endpoint |
| **Email + password** | Registration / login forms | bcrypt hash comparison |
| **Mini App (initData)** | Telegram Mini App launch | HMAC verification with bot token |

## Client Auth Flow

Auth logic lives in the service layer (`services.auth`), used by both FastAPI and the aiogram bot directly.

**HTTP clients** (browser, SvelteKit SSR, Mini App, future mobile) — authenticate via FastAPI:

1. **Browser:** access token in memory (JS variable), refresh token in httpOnly cookie. On 401 → call `/api/v1/auth/refresh` → retry. Guest account created automatically on first visit if no refresh token cookie exists.
2. **SvelteKit SSR:** `hooks.server.ts` reads the refresh token cookie from the incoming request and forwards it to FastAPI calls. No token validation in SvelteKit — FastAPI validates on every request.
3. **Mini App:** auth via Telegram `initData` HMAC, exchanged for a JWT pair via `/api/v1/auth/telegram-miniapp`.
4. **Future mobile:** same JWT flow — authenticate via provider, receive token pair, use access token for API calls.

**Telegram bot** — authenticates via service layer directly. The bot identifies users by `telegram_id` from the Telegram update object (already verified by Telegram). No JWT needed — the bot process is trusted and calls `services.auth` to resolve the user.

## Account Linking & Merging

When a guest links to an existing full account (e.g., already used the TG bot), accounts are **merged**:

1. **PostgreSQL transaction** (atomic): transfer guest's Favorites and view history to the full account, delete guest account, write `AccountMergeLog` entry
2. **Async propagation**: publish `account_merged` event to RabbitMQ → consumers update Qdrant payloads (`author_user_id`), invalidate Redis caches for both old and new user IDs
3. Search indexes are eventually consistent — the user won't notice a brief delay before merged favorites appear in search

Linking flows:
- **Web → Telegram:** "Link Telegram" button → deep link `t.me/memexpertbot?start=link_{code}` → merge
- **Web → Google/Email:** OAuth / email sign-in → upgrade or merge
- **Mini App:** validate `initData` HMAC → lookup by `telegram_id`

Account splitting is not supported.

## Collection Access Control

Three roles enforced at API middleware level:

| Role | Capabilities |
|------|-------------|
| **Owner** | Full control: edit, delete, manage members, invite |
| **Editor** | Add/remove memes, invite viewers |
| **Viewer** | Read-only |

Sharing via invite links. Shared as Mini App deep links: `t.me/memexpertbot/app?startapp=invite_XXXXX`.

## Account Deletion & Data Export

Users can request account deletion and data export from profile settings.

**Data export:** JSON archive of user data (profile, favorites, collections, pins, interaction history). Generated on request, available for download.

**Account deletion:**
1. User requests deletion → `status = deletion_pending`, 30-day grace period (read-only access, can cancel)
2. After 30 days → hard delete (daily APScheduler job):
   - User PII cleared (telegram_id, google_id, email, password_hash)
   - Refresh tokens deleted
   - Favorites collection and memberships deleted
   - Collection ownership transferred to earliest editor, or collection deleted
   - Pinned memes deleted
   - Private memes deleted from storage + Qdrant + Meilisearch

## Security

- **CORS:** allow `memexpert.com`, `*.memexpert.com`, Telegram Mini App origins
- **CSRF:** SameSite=Lax cookies + custom header check (`X-Requested-With`) for state-changing requests
- **Refresh token cookie:** httpOnly, secure, SameSite=Lax, Path=/
- **Input validation:** Pydantic models for all request bodies, Query() constraints for params
- **File uploads:** size limits (10 MB images, 50 MB videos), uploaded through API (not direct-to-S3)
- **Media serving:** CDN with public read, no signed URLs needed

## Rate Limiting

Redis sliding window counters in FastAPI middleware:

| Tier | Endpoints | Limit |
|------|-----------|-------|
| Search | `/api/search`, `/api/feed` | 30 req/min per user |
| Write | POST/PUT/DELETE | 60 req/min per user |
| Upload | `/api/memes/upload` | 10 req/min per user |
| Auth | `/api/auth/*` | 10 req/min per IP |
| Admin | `/api/admin/*` | 120 req/min |
