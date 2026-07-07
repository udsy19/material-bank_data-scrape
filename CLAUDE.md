# Material Bank India — Autonomous Material-Intelligence Pipeline

Project brief and standing direction for Claude Code. Loaded every session. Read this first, then `VISION.md` (strategy + roadmap), `PIPELINE.md` (harvest-stage detail), and `memory.md` (current state). Rules in `.claude/rules/` apply on top and outrank style preferences.

## What we're building

A **B2B material-intelligence platform for India**: every architectural material — tiles, laminates, sanitaryware, lighting, furniture, surfaces, hardware — as canonical, classified, enriched, provenance-tracked product records with live multi-source pricing and procurement paths. Reference bar: Material Bank (US) for enrichment/facets/trust, MaterialDepot (India) for dual-unit pricing/BOM/commerce UX. Our wedge: **intelligence-first** — breadth + data depth + a trust contract nobody else exposes (see `VISION.md` §2).

The catalog **is** the product. It is sold via API and a faceted catalog UI to architects, designers, and procurement teams; later, suppliers claim and pay to enrich their own records (the flip).

## The prime directive (do not violate)

> **Every field carries its provenance, and only verified-complete records are published.** The raw harvest is an ingredient; the product is the canonical record with a trust contract: `{value, confidence, source, basis, observed_at}` on every claim, a completeness score, and a publish gate.

## Hard rules

- **Never fabricate data.** Missing/estimated is structural (`missing[]`, `basis='estimated'`), never silent. A generated/derived value can never masquerade as a measured one. Placeholder/demo SKUs are filtered; wrong image associations are dropped (a wrong image is a fabrication).
- **Prices are observations, not attributes.** `price_observation` with `basis` (`listed_mrp`/`dealer_quote`/`estimated_band`) + `observed_at` + source. MRP is labelled MRP, never "cost". Retrieval serves the freshest observation with its basis; >90 days ⇒ stale flag. Multi-source observations are kept side-by-side (they power price comparison — never collapsed).
- **Surfaces need units.** Tiles/paint/laminates carry `price_unit`, `coverage_sqft_per_box`, `size_mm`, `finish` — or an explicit missing flag. Dual-unit pricing (₹/sqft ↔ ₹/sheet|box) is the target for all surfaces. BOM = area ÷ coverage → ceil → +10% wastage.
- **Publish gate.** Only records above their category's completeness+confidence threshold are exposed to external consumers. Everything else is visibly "in enrichment", never quietly served.
- **Autonomy-first.** Every capability ships as a durable-queue stage (`pipeline_jobs`) + systemd daemon/timer: idempotent, resumable, self-healing, metric-tracked. Nothing may depend on a live session or a human to keep running. Human review queues are accelerators, never dependencies.
- **Registry-driven harvest.** New supplier = a `suppliers` row, never a code path (bespoke parsers go in `DOMAIN_HARVESTERS`, dispatched by the same registry). Probe before scraping; respect robots.txt; ~1 req/2s per domain (politeness is a hard rule — parallelism is across domains, never against one); archive raw payloads content-addressed. No mass-scraping IndiaMART/Justdial; legal read before redistributing dealer pricing.
- **LLM agents only in verified slots** — probe ambiguity, dedupe adjudication, parser repair, enrichment (descriptions/features/classification), spec-PDF extraction — each with an external verification signal and a daily budget cap. LLM output may only add `content` fields or `estimated`-basis values; measured fields come from deterministic extraction only. Everything else stays deterministic.
- **Taxonomy is standards-grounded.** Our tree maps to OmniClass/Uniclass codes; India facets use BIS/ISI standard numbers, GreenPro, GRIHA/IGBC credit relevance. No invented ontology where a standard exists.
- **Keep all tests green** (206+ offline; browser e2e opt-in `-m browser`); every new module ships with tests; bug fixes get a regression test first. All unit tests run offline (fixtures/fakes).
- **No bloat.** Search before writing; modify in place; delete the old path in the same change; no `_v2` files; no Airflow/Prefect — the SQLite queue + systemd timers are the orchestrator. See `.claude/rules/no-bloat.md`.
- **Commit after every completed change, no AI attribution.**

## The system as built (schema v8, 2026-07)

**Pipeline:** probe → harvest (shopify/woo/jsonld generic + bespoke Orientbell/Kajaria/Steelcase) → normalize (`build_product`: units + provenance + missing[]) → price observations → embed (marqo-ecommerce-B, one shared text+image space) → FTS5+vector hybrid retrieval → FastAPI. Self-healing: yield-drift + quarantine spikes → repair queue → re-probe → (LLM slot) parser repair.

**Data:** `catalog.db` — `suppliers` (registry/control-plane) · `products` (UNIQUE(brand,sku), provenance JSON, missing[], source_url) · `price_observation` (append-only) · `embeddings` (text/image BLOB vectors) · `products_fts` (trigger-synced) · `pipeline_jobs` (durable queue: claim/retry/backoff/dead-letter) · `harvest_history` (drift signal) · `quarantine` · `schema_version`. ~142k products, 50 suppliers, ~110k priced.

**Deployment (24/7, VPS 46.202.179.28):** systemd — `mb-harvest.timer` (hourly sweep: recover → tier-aware refresh (shopify daily/jsonld weekly/spec monthly) → drain → bespoke → self-heal) · `mb-embed` (45s consumer) · `mb-api` (uvicorn :8000) · `caddy` (HTTPS: https://46.202.179.28.sslip.io). Deploy: rsync + `systemctl restart`; runbook in `deploy/README.md`.

**API:** `/api/match` (hybrid search) · `/api/products` (filtered/paginated listing) · `/api/suppliers` · `/api/product/{id}` (observations + similar) · `/api/stats` · `/api/pipeline` (queue health) · `/api/image` (proxy).

**Module map:** `material_bank/` — `probe` `fetch` `sitemap` `robots` (acquisition) · `harvest/` (`run` dispatch + `worker` queue + `parallel` + generic tiers + bespoke + `images`) · `db` `models` `jobs` (spine) · `embeddings` `vectorstore` `embed_worker` (index) · `retrieval` `serve` + `static/dashboard.html` (serving) · `drift` `repair` `pipeline` (self-healing/orchestration) · `bom` (BOM math).

## What we're building next (roadmap in VISION.md §7)

- **Phase A — Foundation of Trust:** `canonical_products` + category-specific completeness scoring + `metrics` snapshots + QA/trust dashboard + publish gate + `mb-planner` (turns measured gaps into prioritized queue jobs — the flywheel).
- **Phase B — Deterministic Enrichment:** taxonomy v1 + attribute extractors (title/description, controlled vocab) + generalized PDF spec-mining + dual-unit price normalization + image color-family.
- **Phase C — One Product, One Truth:** entity resolution → golden records → cross-supplier price comparison.
- **Phase D — Intelligence at Scale:** LLM enrichment daemon (novelty-gated, budget-capped, verified). *Blocked on API keys.*
- **Phase E — Procurement Product:** BOM calculators, serviceability, RFQ, API productization.
- **Phase F — The Flip:** supplier portal, discovery to 500+ suppliers, media generation, visual search, BIM export.

## Working method

1. Search the repo before writing anything new; extend in place (no-bloat).
2. TDD: failing test → implement → green; validate live on a small sample before any large run; iterate on real defects found (they're the best tests).
3. Long work runs as background jobs / VPS services — never block on a session. Every long job is resumable (`source_url`, queue claims) and safe to kill.
4. Ship in small commits; run the full suite each time; deploy = rsync + restart; verify on the VPS after deploy.
5. Keep `memory.md` current (state, decisions, honest gaps) — it is the cross-session brain.
6. Data honesty beats data volume. When a source is ambiguous: flag, quarantine, or skip — never guess.
