# DSource AI — Agentic Catalog Pipeline

Companion to `CLAUDE.md` / `ROADMAP.md`. Extends the existing Phase-1 harvest (4-tier fetch, `sqlite-vec`, novelty-gated enrichment) into a self-maintaining pipeline covering all major distributors. Design principle carried over from the agent-critic finding: **deterministic code first; LLM agents only where there is genuine novelty and an external verification signal.**

---

## Stage 0 — Supplier registry (control plane)

Everything is driven by one table, not code changes.

- Extend `data/india/manufacturers.csv` → a `suppliers` table: `domain, brand, category, scrape_tier, price_published (bool), robots_ok, sitemap_url, sku_estimate, last_harvest, last_yield, status`.
- Adding a distributor = adding a row. The pipeline picks it up on the next run.
- Seed: existing 95 + tile brands (Orientbell first — priced; Kajaria, Somany, Johnson, Nitco specs-only) + sanitaryware/laminates/paint as demand dictates.

## Stage 1 — Probe agent (classify before scraping)

Deterministic script per new domain:
1. Fetch `robots.txt` → record disallows; skip disallowed paths.
2. Try `/products.json` (Shopify) → Woo Store API → JSON-LD sample from sitemap pages → else mark Tier 3 (Playwright).
3. Estimate SKU count from sitemap; write tier + estimate to registry.

LLM involvement: only when the probe is ambiguous (mixed signals, odd CMS) — a Claude Code subagent inspects saved HTML and writes the tier decision + a parser note. Log every decision.

## Stage 2 — Harvest workers

- Tiered fetchers over `curl_cffi` (chrome131), one worker per domain, **rate-limited ~1 req/2s**, exponential backoff, resumable.
- **Warm-cache guard** (mirror `realdata.py`): content-hash / ETag skip — re-runs never re-fetch unchanged pages.
- **Incremental mode:** diff sitemap `lastmod` against `last_harvest`; fetch only new/changed URLs.
- **Raw capture:** save every fetched payload gzipped, content-addressed (`raw/{sha256}.gz`) — reproducibility + fixtures for parser repair.
- Playwright pool only for Tier-3 domains, capped concurrency (it is 10× the cost).

## Stage 3 — Normalize + validate

- One Pydantic `NormalizedProduct`; per-field `{value, confidence, source}` (existing discipline).
- **Units are mandatory for tiles/surfaces:** `price_unit` (per_sqft | per_box | per_piece | per_litre), `coverage_sqft_per_box`, `size_mm`, `finish`. BOM math = area ÷ coverage → ceil boxes → +10% wastage.
- GST: `derive_gst(HSN/category)`, always `basis='estimated'` until a canonical HSN table lands.
- Records failing schema → `quarantine` table with reason; never silently dropped, never silently ingested.

## Stage 4 — Entity resolution (dedupe across distributors)

Same SKU will appear on the brand site AND reseller sites at different prices. Resolve, don't duplicate:
1. Exact: `(brand, sku)` upsert (existing pattern).
2. Fuzzy: same brand + size match + CLIP image cosine above the image band → **candidate-duplicate queue**.
3. Queue adjudication: cheap LLM call (Gemini) confirms/rejects merges; decisions logged; a confirmed merge links records, keeping each source's price as a separate observation (Stage 7).

## Stage 5 — Embed + index

- Unchanged: marqo-ecommerce-B at ingest (never per-request), single `sqlite-vec` index serving match/Explore/novelty-gate.
- Add SQLite **FTS5** over title/description/material → hybrid retrieval (keyword + semantic, rank-fused). Cheap, no new infra.

## Stage 6 — Enrichment

- Existing novelty-gated router (Gemini near-dup / Claude Haiku novel / Opus spec-PDFs), content-hash cached, Batches for bulk runs. No change; it just consumes the queue Stage 3 emits.

## Stage 7 — Price layer (separate from the spec layer)

Prices are observations, not product attributes:
- `price_observation`: `product_id, vendor_id, price_inr, price_unit, basis (listed_mrp | dealer_quote | estimated_band), observed_at, source_url`.
- Sources: scraped published MRP (Orientbell-class sites), manual Bengaluru dealer quotes (Phase 5a), flagged category bands as last resort.
- Retrieval always serves the freshest observation + its `basis`; staleness (>90 days) shown honestly.
- **Legal line:** harvest published MRP freely; do NOT mass-scrape IndiaMART/Justdial; get a legal read before *redistributing* scraped dealer pricing.

## Stage 8 — Retrieval / serving

- `/api/match` (existing) + faceted filters: category, typology, size, finish, price band, PEI/maintenance axes, serviceable pincode (Phase 5 vendor layer).
- Query path: FTS5 candidates ∪ vector candidates → rank fusion → band label → attach freshest price observation.

## Stage 9 — Refresh + self-healing (the agentic loop)

- **Scheduler:** cron (local-first). Priced sources weekly; spec-only monthly; new suppliers immediately after probe.
- **Drift detection:** per-domain yield tracked in the registry. Yield drop >30% or schema-validation spike ⇒ parser probably broke ⇒ auto-open a repair task.
- **Repair agent:** Claude Code subagent gets the failing domain + saved raw fixtures + the parser file; must produce a fix **plus a regression test against the fixture** before the domain re-enables. This is the one place an LLM agent clearly beats deterministic code — parsers rot, and the fixture is the external verification signal.
- **Dashboard (one HTML page):** suppliers covered, SKUs total, % priced, % enriched, no-match rate, stale-price count, quarantine size.

---

## Orchestration — keep it boring

- No Airflow/Prefect (bloat; violates local-first). One `pipeline_jobs` SQLite table (queue) + one idempotent worker script per stage + cron. Every stage re-runnable from the raw store.
- Claude Code roles: **probe adjudicator**, **dedupe adjudicator**, **parser repairer**, **enricher** (existing). Everything else is plain Python.

## Build order

1. Registry table + probe script (1–2 sessions).
2. `price_unit`/`coverage` schema migration + tile BOM math + tests.
3. Orientbell harvest (priced anchor) → embed → verify in material swap.
4. Kajaria/Somany/Johnson/Nitco specs-only, flagged prices.
5. Price-observation table + wire retrieval to freshest observation.
6. Cron + drift detection + repair-agent loop.
7. FTS5 hybrid retrieval + coverage dashboard.

## Risks (carry-forward + new)

- Tier-3 sites behind WAFs may block even Playwright — respect it, mark `status=blocked`, move on.
- Reseller prices ≠ brand MRP; without Stage 7 separation they would corrupt BOMs.
- Cross-distributor dedupe on look-alike tiles is genuinely hard — the image band alone will over-merge; size+brand gates are mandatory.
- Scraped MRP is *list* price; real transaction prices in tiles run materially below MRP via dealer discounts — label MRP as MRP, never as "cost".
