# MemeXpert — Technical Design Document

**Semantic Meme Search Engine & Social Platform**

Companion document to MemeXpert PRD.

---

- [System Architecture](01-architecture.md) — High-level architecture, tech stack, service boundaries, deployment
- [Data Model (Draft)](02-data-model.md) — PostgreSQL entities and relationships
- [Search & Discovery](03-search.md) — Hybrid search, Qdrant/Meilisearch configuration, sync, recommendations
- [Content Pipeline](04-content-pipeline.md) — Ingestion, deduplication, processing, media storage, SEO generation
- [Accounts & Auth](05-accounts-auth.md) — Account model, JWT, auth flows, 152-FZ compliance, access control
- [Infrastructure](06-infrastructure.md) — Caching, resilience, monitoring, model upgrades, risks
