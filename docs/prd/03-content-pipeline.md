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
- **OCR** (PaddleOCR PP-OCRv5 for Russian + English, Qwen2.5-VL-2B fallback for stylized text)
- **NSFW detection** (filtered by default in search)
- **Political content detection** (deferred — see [Deferred Features](10-deferred.md))
- **Language detection** (ru / en / mixed / none)
- **Popularity scoring** (views, impressions, downloads, reactions, reposts, platform engagement)
- **Template identification** (AI-assigned during SEO generation)

### SEO Page Content Generation

Async, prioritized by popularity. The MVP SEO generation path should be a typed POC using **PydanticAI** with prompts ported from the project's v0 Rust branch as the baseline. Prompt tuning is expected to continue after launch, but the pipeline must already validate structured outputs before persisting.

Current POC quality caveat: the backend SEO generator currently works from OCR text, existing tags, language, and template metadata, not raw image bytes. The prompt and provider must stay grounded in those available facts until image-aware inputs are added later.

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

---

## Meme Templates (V1)

### What a Template Is (V1)

In V1, a template is a **label, not an editor tool.** It has a name, slug, and links to memes. No base image, no text regions, no editing capability.

Example: the meme template "Drake Hotline Bling" — a page at `/templates/drake-hotline-bling` showing all memes in the database that use this template.

### How Templates Are Assigned

AI assigns templates during SEO page content generation. The LLM sees the meme image and identifies whether it uses a known template. If recognized → linked. If the LLM identifies a new common template → creates a new template entity. Admins can manually curate, rename, merge, and manage templates.

### Template Pages

`/templates/{slug}` — always by slug, never by UUID. Contains:

- Template name and description
- Gallery of memes using this template (sorted by popularity)
- Popularity analytics for this template (aggregate of all memes)
- SEO-indexed

### Meme Editor (V2)

In V2, templates are extended with `base_image` and `text_regions` to power a browser-based meme editor: select template → add text → preview → download or save. This builds on the V1 template infrastructure.
