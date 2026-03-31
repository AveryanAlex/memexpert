# System Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     INGESTION LAYER                         │
│                                                             │
│   Telegram Crawler ─┐                                      │
│   Reddit Crawler  ──┼──→ Ingestion Service (dedup + save)  │
│   VK Crawler      ──┘                                      │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────┼──────────────────────────────┐
│                   PROCESSING LAYER                          │
│                  (TaskIQ Workers)                            │
│                                                             │
│   Transcode ──→ OCR ──→ Embedding ──→ Classification       │
│                             │                               │
│                    ┌────────┴────────┐                      │
│                    ▼                ▼                        │
│              Qdrant Sync    Meilisearch Sync                │
│              SEO Generator                                  │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────┼──────────────────────────────┐
│                      SERVING LAYER                          │
│                                                             │
│   SvelteKit (BFF + SSR) ──→ FastAPI REST API               │
│         │                        │                          │
│         │                   ┌────┴────┐                     │
│         ▼                   ▼         ▼                     │
│   Browser / Mini App   Meilisearch  Qdrant                  │
│                                                             │
│   Telegram Bot (aiogram) ──→ FastAPI REST API               │
│                                                             │
│   imgproxy + CDN ──→ S3                                     │
└─────────────────────────────────────────────────────────────┘
```

## Service Boundaries

| Service | Role | Communication |
|---------|------|---------------|
| **SvelteKit** | BFF + SSR. Server-side rendering, client routing, auth cookie handling. `+page.server.ts` load functions call FastAPI internally. | HTTP → FastAPI |
| **FastAPI** | REST API. Business logic, auth, search orchestration, all data mutations. Single source of API truth. | PG, Qdrant, Meilisearch, Redis |
| **TaskIQ Workers** | Async processing. Separate worker pools by resource profile (see Content Pipeline). | Redis broker, PG, S3, external APIs |
| **Telegram Bot (aiogram)** | Inline queries, PM features. Webhook mode. Calls FastAPI for data. | HTTP → FastAPI |
| **Crawlers** | Platform-specific (Telethon for Telegram). Scheduled by TaskIQ. Normalize output to common format. | PG, S3 |
| **imgproxy** | On-the-fly image transforms (resize, WebP/AVIF). CDN-cached. | S3 (source), CDN (delivery) |

### SvelteKit as BFF

SvelteKit acts as a backend-for-frontend: `hooks.server.ts` handles auth (refresh token cookie validation), and `+page.server.ts` load functions call FastAPI endpoints to fetch data for SSR. The browser also calls FastAPI directly for client-side interactions (likes, saves, search). This keeps FastAPI as the single API layer while allowing SvelteKit to handle SSR, routing, and auth cookie lifecycle.

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| **API** | FastAPI (Python) | Async, Pydantic validation, custom JWT auth |
| **Web / Mini App** | SvelteKit (SSR) | Streaming SSR, service workers, BFF pattern |
| **Telegram Bot** | aiogram (Python) | Async, webhook mode |
| **Database** | PostgreSQL 16+ | Source of truth for all relational data |
| **Vector Search** | Qdrant | Filtered ANN + recommend API |
| **Text Search** | Meilisearch | Typo-tolerant, faceted, Russian morphology |
| **Job Queue** | TaskIQ + Redis | Async workers for processing pipeline |
| **Object Storage** | Cloudflare R2 / Backblaze B2 | S3-compatible |
| **CDN** | Cloudflare | Media delivery + imgproxy caching |
| **Image Processing** | imgproxy, Pillow, FFmpeg | On-the-fly + batch transcoding |
| **Embeddings** | Voyage AI (`voyage-multimodal-3.5`) | 1024-dim multimodal, Matryoshka support |
| **OCR** | PaddleOCR PP-OCRv5 + Qwen2.5-VL-2B | Primary + VLM fallback for stylized text |
| **Cache** | Redis | Hot caches, JWT blocklist |
| **Monitoring** | Prometheus + Grafana | Metrics, alerting |

### Why Two Search Engines

- **Meilisearch** excels at typo-tolerant full-text search with Russian morphology and faceted filtering. It handles queries where users type OCR text or tag names.
- **Qdrant** excels at semantic similarity — "when the deadline is tomorrow" finds panic memes even without those words. Also powers recommendations (similar memes, personalized feed) and deduplication.
- Neither alone covers both use cases well. The hybrid approach merges results with tunable weights.

## Storage Responsibilities

| Store | Role | Data |
|-------|------|------|
| **PostgreSQL** | Source of truth | All entities, relations, embedding cache |
| **Qdrant** | Vector search + recommendations | MemeFile embeddings with meme-level payload |
| **Meilisearch** | Text search + facets | Meme documents: OCR text, tags, metadata |
| **S3** | Media storage | All file variants (originals + transcoded) |
| **Redis** | Queue + cache | TaskIQ broker, hot caches |

## Deployment

```
Server 1: FastAPI + SvelteKit + aiogram + Redis + imgproxy
Server 2: TaskIQ workers (transcode, embedding, OCR, SEO, sync) + PaddleOCR + Qwen2.5-VL
Server 3 (or colocated with 1): Qdrant + Meilisearch (Docker)

Managed: PostgreSQL, S3 (R2/B2), Cloudflare CDN
```

Qdrant and Meilisearch are lightweight enough to share a server with the API at initial scale. Split to dedicated nodes if search latency becomes an issue.

### Resource Estimates

| Resource | Estimate | Notes |
|----------|----------|-------|
| Qdrant memory | ~2 GB per 1M vectors | 1024-dim float32, HNSW |
| Meilisearch memory | ~500 MB per 1M docs | Short text fields |
| Embedding cache (PG) | ~4 KB per entry | 4096 bytes + metadata |
| S3 storage | ~2 TB at 1M memes | All file variants |
| Voyage AI API | ~$100–180/month | At 5–10K new memes/day |
