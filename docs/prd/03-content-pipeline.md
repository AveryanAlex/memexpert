# Content Pipeline

## Content Ingestion

### Source Channels

Crawls public meme channels. Content deduplicated across sources.

Initial target: ~100 public Telegram meme channels.
Planned: Reddit, VK, Twitter/X, meme aggregators.

### Source Suggestions

Users can suggest new channels for crawling via a form in the bot PM and on the website. Suggestions enter a moderation queue for admin review and approval.

### Supported Media Types

- **Images** (JPEG, PNG, WebP) — primary
- **GIFs** — converted to MP4 for delivery
- **Videos** (MP4, WEBM) — website and Mini App only, excluded from TG inline

### Content Processing

Every meme goes through:

- **Deduplication** (perceptual hash + embedding similarity)
- **OCR** (PaddleOCR for Russian + English; deterministic fake provider in CI/E2E; no active Qwen/VLM fallback in the current backend slice)
- **NSFW detection** (filtered by default in search)
- **Political content detection** (deferred — see [Deferred Features](10-deferred.md))
- **Language detection** (ru / en / mixed / none)
- **Popularity scoring** (views, impressions, downloads, reactions, reposts, platform engagement)
- **Template identification** (AI-assigned during SEO generation)

### SEO Page Content Generation

Async, prioritized by popularity. The MVP SEO generation path should be a typed POC using **PydanticAI** with prompts ported from the project's v0 Rust branch as the baseline. Prompt tuning is expected to continue after launch, but the pipeline must already validate structured outputs before persisting.

Current POC quality caveat: live SEO generation attaches eligible primary image bytes when object storage can resolve them safely, while static/local generation remains text-only. If the primary image is missing, unsupported, oversized, or unreadable, the prompt and provider stay grounded in OCR text, existing tags, language, safe media metadata, and template metadata.

Each generated meme receives:

- **URL slug** (human-readable, SEO-friendly)
- **Page title** and **meta description**
- **Alt text** and **caption**
- **Body text** (2–4 paragraphs)
- **Tags** (for categorization and filtering)
- **Template assignment** (if the AI recognizes a known meme template)

URL strategy: memes with SEO content → `/memes/{slug}` (indexed). Without → `/memes/{id}` (accessible, not indexed). When SEO content is generated, `/memes/{id}` 301-redirects to `/memes/{slug}`.

### Popularity Tracking

Periodic snapshots and/or materialized views track meme popularity over time. Use materialized views where they simplify trend, tag, template, and timeline queries without duplicating business logic in application code.

Popularity tracking powers:

- Popularity charts on meme pages (public)
- Template and tag-level trend analytics (public)
- Trend comparison tool (public, Phase 2)
- "Meme of the day" automated selection
- Rising memes detection
- Trending feed
- Recommendation cold-start fallback

### Visibility and deduplication policy

- User and operator uploads are private by default. Public crawler discoveries are public by default.
- SHA-256 equality is the only deduplication rule allowed to cross users or the public/private boundary. It reuses the same canonical meme and file, then adds source provenance and the uploader's collection membership.
- A crawler exact-SHA match promotes an automatic private upload to public. An admin-forced private meme remains private even after crawler provenance is attached.
- Perceptual-hash and embedding similarity are approximate. They may merge public with public, or private with private only when both memes have the same sole uploader. They never merge public and private content or private content belonging to different/shared uploaders.
- Exact SHA lookup covers every file variant of a meme and is serialized in PostgreSQL so concurrent uploads converge on one meme/file.

---

## Meme Templates (V1)

### What a Template Is (V1)

In V1, a template is a **label, not an editor tool.** It has a name, slug, and links to memes. No base image, no text regions, no editing capability.

Example: the meme template "Drake Hotline Bling" — a page at `/templates/drake-hotline-bling` showing all memes in the database that use this template.

### How Templates Are Assigned

AI assigns templates during SEO page content generation. In live mode, the LLM sees eligible primary image bytes and identifies whether the meme uses a known template. If recognized → linked. If the LLM identifies a new common template → creates a new template entity. Admins can manually curate, rename, merge, and manage templates.

### Template Pages

`/templates/{slug}` — always by slug, never by UUID. Contains:

- Template name and description
- Gallery of memes using this template (sorted by popularity)
- Popularity analytics for this template (aggregate of all memes)
- SEO-indexed

### Meme Editor (V2)

In V2, templates are extended with `base_image` and `text_regions` to power a browser-based meme editor: select template → add text → preview → download or save. This builds on the V1 template infrastructure.
