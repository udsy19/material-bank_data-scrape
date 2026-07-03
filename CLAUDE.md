# DSource AI

Project brief and standing direction for Claude Code. Loaded every session. Read this first, then `ROADMAP.md` (the plan), `PIPELINE.md` (the catalog/harvest pipeline), and `memory.md` (current state + open questions). The rules in `.claude/rules/` apply on top of this and outrank style preferences when they conflict.

## What we're building

**DSource AI** is a **single-user** platform — used by *both* design professionals and end-clients — that takes any interior (residential, hospitality, retail, or small workplace) from **inspiration → a real, priced, sourceable design**. India-first, architected to expand later.

It repoints the existing *DSource Studio* engine (CAD ingest, 2D/3D viewer, test-fit, wellbeing scoring, pricing connectors, procurement, ControlNet render) away from enterprise/GCC-only toward multi-typology single-user. **Not** enterprise, **not** multi-seat, **not** freemium. (Enterprise "Studio" is a separate, later track on the same engine.)

## The core architecture principle (do not violate)

> **The structured, catalog-backed scene is the source of truth. AI is the inspiration/beauty layer on top — never the source of truth.**

Two modes share one engine:

1. **Explore (creative-first)** — AI generates freely → each element is **back-matched** (CLIP) to the closest real catalog product, surfaced with an explicit confidence label. The moment of inspiration.
2. **Specify (catalog-first)** — the scene is assembled only from real SKUs → exact BOM, INR price + GST, vendor, lead time. The moment of commitment.

The **back-match is the bridge** Explore → Specify. Both modes depend on the **same prerequisite: a real India catalog with image embeddings.** Build that first and "how creative vs how catalog-led" becomes a per-persona **dial**, not a rebuild (end-clients default to Explore, pros default to Specify).

## Hard rules

- **Never fabricate data.** A generated element with no good real match is **flagged** ("No real match"), never faked. Every estimated/missing price or field is marked as such — continue the existing `real=False` discipline. Enforce this *in the schema*: every attribute carries `{value, confidence, source/basis}` so missing/estimated is structural, not silent.
- **Keep all existing tests green** (64 today); add tests for every new module. Bug fixes get a regression test first.
- **Follow the existing design system** in `frontend/src/design/` — warm paper background, ink linework, single terracotta accent, Fraunces serif numerals + Inter. Do not introduce a new visual language.
- **India-first:** prices in INR with GST; sources are Indian vendors.
- **Free / local-first, swappable:** open models + in-process stores behind one interface; the only paid calls are the vision LLMs, and they sit behind a provider-agnostic interface (mirror `routers/render.py`). No heavy/paid infra yet.
- **Prices are observations, not attributes.** Every price lives in `price_observation` with `basis` (`listed_mrp` / `dealer_quote` / `estimated_band`) + `observed_at` + source. MRP is labelled MRP, never "cost". Retrieval serves the freshest observation with its basis; >90-day-old prices are flagged stale.
- **Surfaces need units.** Tiles/paint/laminates carry `price_unit` (per_sqft / per_box / per_piece / per_litre), `coverage_sqft_per_box`, `size_mm`, `finish`. BOM = area ÷ coverage → ceil boxes → +10% wastage. Never ingest a surface SKU without these (or an explicit missing flag).
- **Registry-driven harvest.** New distributor = new row in the `suppliers` registry (domain, tier, price_published, robots status, last yield) — never a new hardcoded path. Probe before scraping; respect robots.txt; ~1 req/2s per domain; archive raw payloads content-addressed. No mass-scraping IndiaMART/Justdial; legal read before redistributing scraped dealer pricing.
- **LLM agents in four slots only** (probe ambiguity, dedupe adjudication, parser repair, enrichment) — each with an external verification signal; everything else deterministic. A parser-repair fix ships only with a regression test against the saved fixture.
- **No bloat.** Search before you write; modify in place; delete the old path in the same change; no parallel `_v2` files. No Airflow/Prefect — SQLite job queue + cron. See `.claude/rules/no-bloat.md`.
- **Commit after every completed change, no AI attribution.** See `.claude/rules/git-workflow.md`.

## The decided stack (free / local-first; see ROADMAP §Phase 1)

| Slot | Pick |
|---|---|
| Harvest | 4-tier `fetch_products(domain)`: Shopify `/products.json` → WooCommerce Store API → schema.org JSON-LD (+ sitemap) → Playwright, all over `curl_cffi` (impersonate chrome131) |
| Catalog schema | Extend the existing `Product` model; add `image_url`, `price_inr`, `gst_rate`, `provenance` JSON with per-field flags |
| Image embeddings | `Marqo/marqo-ecommerce-embeddings-B` (Apache-2.0, 768-dim, via `open_clip`) — text+image one shared space |
| Vector store | `sqlite-vec` (`vec0` table) inside the existing `dsource.db` |
| Enrichment | Novelty-gated router: CLIP cosine ≥~0.85 + category overlap ⇒ `gemini-2.5-flash`; novel/first-seen ⇒ `claude-haiku-4-5`; hard spec sheets ⇒ `claude-opus-4-8`. Behind a `VisionEnricher` interface. One Pydantic schema drives both providers. PDF via **pdfplumber** (MIT — never PyMuPDF/AGPL) |
| Material → maintenance | Pure `derive_material_attributes()` over one flat `material_attributes` table; 6 standard-backed axes (Martindale/PEI/AC/Janka/GREENGUARD-CARB), each with a `basis` enum (`measured_standard`/`derived_proxy`/`estimated`) |
| Vendor model | Three entities: `vendor` + `vendor_offering` + reuse `manufacturers.csv`; pincode serviceability via data.gov.in GODL pincode CSV + haversine. Bengaluru bootstrapped manually (20–50 vetted) |
| Explore | FastSAM-s masks → same CLIP encoder → cosine back-match → confidence band; generation via the existing Replicate Flux proxy (canny+depth control) |
| AR | `<model-viewer>` (MIT), curated GLB set, start with tile/paint surfaces |
| Entity resolution | `(brand, sku)` exact upsert → brand+size+CLIP-cosine fuzzy → candidate-duplicate queue, Gemini adjudicates; merged records keep per-source price observations |
| Retrieval | SQLite FTS5 (keyword) ∪ sqlite-vec (semantic) → rank fusion → band label + freshest price |
| Refresh | cron; priced sources weekly, spec-only monthly; per-domain yield drift >30% ⇒ auto repair task (Claude Code subagent + fixture + regression test) |
| Tile anchor | Orientbell first (published MRP/sqft); Kajaria/Somany/Johnson/Nitco specs-only with flagged prices, filled by Bengaluru dealer quotes |

**One vector index, used three ways:** Explore back-match, Specify retrieval, and the enrichment novelty gate all share the single `sqlite-vec` index. Do not introduce a second embedding space.

## Existing codebase — orient here first

- `backend/app/floorplan/` — `dxf_ingest.py` (DXF/DWG ingest + unit normalization), `cad_geometry.py`, `capacity.py`.
- `backend/app/testfit/` — generative space planning (currently office-only: `rooms.py`, `zones.py`). Gate behind `typology == workplace`.
- `backend/app/pricebook/`, `coop/`, `gsa/`, `realdata.py` — real pricing connectors (~53% real today). `realdata.py` has the **warm-cache guard** pattern to mirror.
- `backend/app/pricing/engine.py`, `ingest/sif.py`, `ingest/service.py` — pricing/quote spine; upsert on `(manufacturer_code, sku)`; `infer_category`.
- `backend/app/routers/render.py` — **provider-agnostic interface to mirror** for enrichment. `config.py` holds swappable model names.
- `backend/app/procurement/models.py` — `seed_vendors` pattern to mirror for the vendor layer.
- `data/india/manufacturers.csv` — 95 verified India suppliers (the brand registry); `data_type`/`scrape` columns drive harvest tier selection.
- `frontend/src/Studio.tsx`, `components/CadViewer.tsx` (2D+3D; `FLOOR_MATS/WALL_MATS/FURN_MATS` palettes defined, awaiting wiring to real SKUs), `SpaceView.tsx`, `Procurement.tsx`, `design/`.

## Working method

1. Explore the repo and search for existing implementations before writing new code.
2. Post a short written plan (files to add/change, schema, test list). **Wait for confirmation before starting a new phase.**
3. Implement in small, reviewable commits; run tests after each; commit per the git-workflow rule.
4. Keep `memory.md` updated as decisions land and status changes.

Stack rationale and citations live in the research synthesis referenced from `memory.md`.
