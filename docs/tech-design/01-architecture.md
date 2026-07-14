# System Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     INGESTION LAYER                         │
│                                                             │
│   Telegram Crawler ─┐                                      │
│   Reddit Crawler  ──┼──→ Ingestion Service (dedup + save)  │
│   VK Crawler      ──┘          │                           │
│                                ▼                           │
│                           RabbitMQ                          │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────┼──────────────────────────────┐
│                   PROCESSING LAYER                          │
│                (FastStream Workers)                          │
│                                                             │
│   Transcode ──→ OCR ──→ Embedding ──→ Classification       │
│                                              │              │
│                                 ┌────────────┼──────┐       │
│                                 ▼            ▼      ▼       │
│                          Qdrant Sync  Meili Sync  SEO Gen   │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                      SERVICE LAYER                          │
│          (shared Python package — business logic)            │
│                                                             │
│ Search · Recommendations · Collections · Memes · Auth       │
│ Analytics/Interactions · Trends/MOTD                        │
│         │            │            │                         │
│         ▼            ▼            ▼                         │
│   PostgreSQL    Qdrant/Meili    Redis                        │
└──────────┬──────────────────────────┬───────────────────────┘
           │                          │
┌──────────┼──────────────────────────┼───────────────────────┐
│          │   PRESENTATION LAYER     │                       │
│          │                          │                       │
│   FastAPI (HTTP API)    aiogram (Bot)    Channel Bot         │
│        ▲                     ▲              │               │
│        │                     │         Themed Channels       │
│   SvelteKit (SSR) ─┐   Telegram Bot API                     │
│        │           │                                        │
│   Browser      Mini App                                     │
│                                                             │
│   imgproxy + CDN ──→ S3                                     │
└─────────────────────────────────────────────────────────────┘
```

## Service Layer

The core application logic lives in a shared Python service layer — protocol-agnostic, used directly by all Python processes:

```
memexpert/
  services/     # business logic — search, recommendations, collections, memes, auth, analytics
  models/       # SQLAlchemy models, Pydantic schemas
  api/          # FastAPI routes (presentation layer)
  bot/          # aiogram handlers (presentation layer)
  workers/      # FastStream consumers (presentation layer)
  crawlers/     # long-running channel listeners
```

Every process is a thin entry point over `services/`. The service layer owns DB access, RabbitMQ publishing, search/recommendation orchestration, interaction tracking, and auth verification. This means:

- **No internal HTTP calls** — the bot calls services directly, not `HTTP GET /api/v1/search`. Critical for inline query latency (~5s Telegram timeout).
- **Independent failure domains** — API going down doesn't break the bot and vice versa.
- **Consistent behavior** — same validation, access control, search scope, and recommendation logic regardless of entry point.

## Process Boundaries

| Process | Role | Communication |
|---------|------|---------------|
| **FastAPI** | Public HTTP API (presentation layer). Versioned (`/api/v1/`), documented with OpenAPI. Consumed by: SvelteKit SSR, browser, Mini App, future mobile clients. | Services → PG, Qdrant, Meilisearch, Redis |
| **aiogram bot** | Telegram presentation layer. Inline queries, PM features, webhook mode. Resolves/creates full accounts by `telegram_id` and calls service layer directly. | Services → PG, Qdrant, Meilisearch, Redis |
| **SvelteKit** | SSR, public website, admin UI, and Mini App frontend shell. Server-side rendering for SEO and initial page loads. | HTTP → FastAPI |
| **FastStream Workers** | Event-driven processing. Separate consumer groups by resource profile (see Content Pipeline). | RabbitMQ → Services → PG, S3, external APIs |
| **Crawlers** | Long-running listeners per platform (Telethon for Telegram). Listen to channel updates in real-time, catch up from `last_read_post_id` on startup. Publish `raw_meme` events to RabbitMQ. | RabbitMQ, PG, S3 |
| **Scheduler** | APScheduler process for periodic tasks: source-engagement capture enqueueing, materialized-view refresh, search-index sync, Meme of the Day, and scheduled SEO batches. | Services → PG, Redis, RabbitMQ |
| **Channel Bot** | Separate aiogram bot for themed MemeXpert-owned channels. Acts as a virtual user — has its own recommendation profile per channel, selects memes by tag + popularity + novelty, posts 2–4×/day. Monitors subscriber feedback (reactions, views, forwards) to refine per-channel selection. | Services → PG, Telegram Bot API |
| **imgproxy** | On-the-fly image transforms (resize, WebP/AVIF). CDN-cached. | S3 (source), CDN (delivery) |

### FastAPI as Public API

FastAPI is the public HTTP API — versioned (`/api/v1/`), documented with OpenAPI. Routes are thin: parse request, call service, format response. Auth is implemented in the service layer and transported to browser clients through HttpOnly cookie-backed access/session JWTs with nonce-based revocation. The OpenAPI spec enables client codegen for any platform.

### aiogram Bot as Telegram API

The bot is a parallel presentation layer to FastAPI, serving Telegram users. It calls the same service functions — search, recommendations, collection management, interaction tracking. Both processes share the same database connections and business rules, but run as independent processes with separate failure domains.

**Telegram `file_id` caching:** When the bot sends a meme inline for the first time, it uploads the file to Telegram and receives a `file_id`. This `file_id` is cached in `TelegramFileIdCache` (keyed by `meme_file_id` + `media_format`). Subsequent sends of the same meme reuse the cached `file_id` — no upload, instant delivery. Cache is per-bot-token (Telegram scopes `file_id` to the bot). The channel bot uses a different token, so it maintains its own cache entries.

### SvelteKit as SSR Layer, Admin UI, and Mini App

SvelteKit handles server-side rendering for SEO and fast initial page loads. `+page.server.ts` load functions call FastAPI endpoints, passing the user's cookies, and render the response into HTML. The browser calls FastAPI directly for all client-side interactions (likes, saves, search). SvelteKit contains zero durable business logic — if SvelteKit were removed, the API would still be fully functional.

**Admin UI** is part of the same SvelteKit app under `/admin/*` routes, role-gated. UI components (grids, meme cards, collection management) are shared between user-facing and admin views where applicable.

**Telegram Mini App** is the same SvelteKit app registered as a TG Mini App. Same codebase, same routes — Mini App startup reads Telegram `initData`, exchanges it through FastAPI, and receives the same cookie-backed session used by browser routes.

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| **API** | FastAPI (Python) | Async, Pydantic validation, custom JWT/cookie auth |
| **Web / Mini App** | SvelteKit (SSR) | Streaming SSR, service workers, BFF pattern |
| **Telegram Bot** | aiogram (Python) | Async, webhook mode, calls service layer directly |
| **Database** | PostgreSQL 16+ | Source of truth for all relational data |
| **Vector Search** | Qdrant | Filtered ANN + recommend API |
| **Text Search** | Meilisearch | Typo-tolerant, faceted, Russian morphology |
| **Message Broker** | RabbitMQ | Durable event streaming for content pipeline |
| **Event Framework** | FastStream | Async event-driven workers with Pydantic message schemas |
| **Scheduler** | APScheduler | Periodic tasks (source-engagement capture, MV refresh, MOTD, batch sync, scheduled SEO) |
| **Object Storage** | Cloudflare R2 / Backblaze B2 | S3-compatible |
| **CDN** | Cloudflare | Media delivery + imgproxy caching |
| **Image Processing** | imgproxy, Pillow, FFmpeg | On-the-fly + batch transcoding |
| **Embeddings** | Voyage AI (`voyage-multimodal-3.5`) | 1024-dim multimodal, Matryoshka support |
| **OCR** | PaddleOCR helper | Worker-only Python 3.13 helper venv for Russian/English text; fake mode for deterministic CI/E2E |
| **LLM/SEO** | PydanticAI provider boundary | Baseline prompts ported from v0 Rust branch; model configured per environment |
| **Cache / rate limiting** | Redis | Rate limiting, hot caches, optional search candidate pools |
| **Python Tooling** | uv (package manager), ruff (lint), ty (type check), pytest | Single `pyproject.toml`, uv for deps + virtualenv + scripts |
| **SvelteKit Tooling** | pnpm, biome (lint), svelte-check, Vitest, Playwright | Component tests + E2E |
| **Observability** | OpenTelemetry planned | Prometheus endpoint is deferred; structured logs/traces first |

### Why Two Search Engines

- **Meilisearch** excels at typo-tolerant full-text search with Russian morphology and faceted filtering. It handles queries where users type OCR text or tag names.
- **Qdrant** excels at semantic similarity — "when the deadline is tomorrow" finds panic memes even without those words. Also powers recommendations (similar memes, personalized feed) and deduplication.
- Neither alone covers both use cases well. The hybrid approach merges results with tunable weights and logs score components for later tuning.

## Storage Responsibilities

| Store | Role | Data |
|-------|------|------|
| **PostgreSQL** | Source of truth | All entities, relations, interaction events, embedding cache |
| **Qdrant** | Vector search + recommendations | MemeFile embeddings with meme-level payload |
| **Meilisearch** | Text search + facets | Meme documents: OCR text, tags, metadata |
| **S3** | Media storage | Originals + optional GIF/video playback and preview-frame artifacts; static image variants via imgproxy |
| **RabbitMQ** | Event streaming | Content pipeline events, fan-out to sync consumers |
| **Redis** | Cache / rate limiting | Rate limits, hot responses, optional candidate pools |

## Deployment

```
Server 1: FastAPI + SvelteKit + aiogram + Channel Bot + Scheduler + Crawlers + Redis + imgproxy
Server 2: FastStream workers (transcode, OCR, embed, classify, sync, SEO) + worker-only FFmpeg + PaddleOCR helper
Server 3 (or colocated with 1): Qdrant + Meilisearch + RabbitMQ (Docker)

Managed: PostgreSQL, S3 (R2/B2), Cloudflare CDN
```

Qdrant, Meilisearch, and RabbitMQ are lightweight enough to share a server with the API at initial scale. Split to dedicated nodes if latency becomes an issue.

Backend containers are split by runtime target: API, bot, scheduler, and worker images install only their service dependency group plus common Python runtime dependencies. The worker image is intentionally heavier: it is the only Python app image with FFmpeg/FFprobe, Paddle/OpenCV system libraries, and the Python 3.13 PaddleOCR helper venv. PaddleOCR/PaddlePaddle are not installed into the main Python 3.14 app environment.

### Resource Estimates

| Resource | Estimate | Notes |
|----------|----------|-------|
| Qdrant memory | ~2 GB per 1M vectors | 1024-dim float32, HNSW |
| Meilisearch memory | ~500 MB per 1M docs | Short text fields |
| Embedding cache (PG) | ~4 KB per entry | 4096 bytes + metadata |
| S3 storage | ~400 GB at 1M memes | Originals + optional GIF/video playback and PNG preview-frame artifacts; static image variants via imgproxy |
| Voyage AI API | ~$100–180/month | At 5–10K new memes/day |
