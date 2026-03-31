# Overview

## Executive Summary

### Product Vision

MemeXpert is the first semantic meme search engine for the Russian-speaking internet. The product solves two core problems: fast meme discovery in Telegram conversations via an inline bot, and engaging meme browsing on a dedicated website with AI-powered recommendations. Unlike existing solutions (Tenor, Giphy, @pic bot), MemeXpert focuses specifically on memes (not all images or GIFs only), supports Russian and English content equally, and offers deep semantic search that understands context and intent — not just keywords.

### Product Pillars

- **Semantic meme search** via Telegram inline bot — find the right meme in seconds
- **SEO-optimized meme catalog website** with AI-generated page content and recommendations
- **User collections with sharing** — a Pinterest-like experience for memes
- **Automated content pipeline:** crawl → deduplicate → index → describe → serve
- **Extensible multi-platform content ingestion** — Telegram first, Reddit/VK/others later
- **Network of themed Telegram channels** as a distribution and feedback engine
- **Meme popularity tracking & public analytics** — historical charts, trend comparison, meme timelines
- **Telegram Mini App** — website embedded in Telegram for extended features

### Target Audience

**Primary:** Russian-speaking internet users aged 14–30 who actively use Telegram. A meme-literate generation that communicates through memes in group chats, dorm/school chats, and social media. Comfortable with both Russian and English meme content.

**Secondary:** SMM managers and content creators who need memes for social media posts, stories, and audience engagement. They benefit from trend tracking, popularity analytics, meme search by topic, and the meme editor.

### Key Differentiators

- **Semantic search:** the query "when the deadline is tomorrow" finds panic/stress memes even without those exact words
- **Meme-focused:** curated from top meme channels, not a dump of all internet images
- **Bilingual:** full support for Russian and English meme text
- **Social layer:** collections, sharing, collaborative private meme libraries for communities
- **Meme analytics:** public popularity charts, trend comparison (like Google Trends for memes), origin tracing — valuable for SMM and fun for everyone
- **Multi-source architecture:** designed to ingest memes from Telegram, Reddit, VK, and future platforms

### Current Traction

A working MVP exists: the website receives ~1,000 daily visits and the Telegram bot has an active user base. Demand for web-based meme browsing and inline search is validated. The next step is building the production-ready product described in this document.

### Strategy

**Website = acquisition** (SEO, organic traffic, discovery). **Telegram bot = retention** (daily use in chats, collections, instant access). The website funnels users to the bot; the bot keeps them engaged.

---

## User Personas

### Misha, 20 — University Student

Active in 15+ Telegram group chats. Sends 10–20 memes daily. Has a folder of "local" memes specific to his university. Currently searches by scrolling saved messages. **Wants:** instant meme search in chat, a private collection shared with friends.

### Anya, 17 — High School Student

Lives in meme culture. Browses endlessly. Follows 20+ meme channels. **Wants:** discover new memes, save favorites, scroll infinitely.

### Dima, 27 — SMM Manager

Creates brand social media content. Tracks trends. **Wants:** fast topical/trending meme search, popularity charts to spot rising memes, meme editor, organized collections by campaign.

### Lena, 24 — Community Manager

Manages online communities. Uses memes to keep conversations lively. **Wants:** pinned memes for instant access, organized collections by mood/reaction, shared sets with co-moderators.
