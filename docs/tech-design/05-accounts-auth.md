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

## SvelteKit Integration

SvelteKit `hooks.server.ts` handles the auth lifecycle:

1. Reads refresh token cookie on each request
2. Validates token, attaches user to `event.locals`
3. SSR load functions access `event.locals.user` for personalization
4. Client-side: access token in memory, refreshed via `/api/auth/refresh` on 401
5. Guest account created automatically on first visit if no refresh token cookie exists

## Account Linking & Merging

When a guest links to an existing full account (e.g., already used the TG bot), accounts are **merged**: guest's Favorites and view history transfer to the full account. Guest account is deleted. Audit logged in `AccountMergeLog`.

Linking flows:
- **Web → Telegram:** "Link Telegram" button → deep link `t.me/MemeXpertBot?start=link_{code}` → merge
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

Sharing via invite links. Shared as Mini App deep links: `t.me/MemeXpertBot/app?startapp=invite_XXXXX`.

## 152-FZ Compliance (Account Deletion)

Russian Federal Law 152-FZ requires users can request deletion of personal data.

1. User requests deletion → `status = deletion_pending`, 30-day grace period (read-only access)
2. User can cancel within grace period → `status = active`
3. After 30 days → hard delete (daily TaskIQ scheduled job):
   - User row cleared (telegram_id, google_id, email, password_hash)
   - Refresh tokens deleted
   - Favorites collection and membership deleted
   - Collection memberships removed (ownership transferred to earliest editor, or collection deleted)
   - Pinned memes deleted
   - Analytics events re-attributed to anonymous
   - Private memes deleted from storage + Qdrant + Meilisearch
4. Audit trail in `DataDestructionLog`

## Security

- **CORS:** allow `memexpert.com`, `*.memexpert.com`, Telegram Mini App origins
- **CSRF:** SameSite=Lax cookies + custom header check (`X-Requested-With`) for state-changing requests
- **Refresh token cookie:** httpOnly, secure, SameSite=Lax, Path=/api/auth
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
