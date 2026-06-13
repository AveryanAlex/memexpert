# MemeXpert — Technical Design Document

**Semantic Meme Search Engine & Social Platform**

Companion document to MemeXpert PRD.

---

- [System Architecture](01-architecture.md) — High-level architecture, tech stack, service boundaries, deployment
- [Data Model](02-data-model.md) — PostgreSQL entities, interaction events, and relationships
- [Search & Discovery](03-search.md) — Hybrid search, private collection scope, Qdrant/Meilisearch configuration, sync, recommendations
- [Content Pipeline](04-content-pipeline.md) — Ingestion, deduplication, processing, media storage, SEO generation, scheduled analytics jobs
- [Accounts & Auth](05-accounts-auth.md) — Account model, cookie/JWT auth, Mini App/bot auth flows, access control
- [Infrastructure](06-infrastructure.md) — Caching, resilience, observability, model upgrades, risks
- [Frontend Charting](frontend-charting.md) — Audit of current bespoke charts and charting library recommendation
