# DSource Catalog Pipeline — Memory (living state)

Single source of truth for *current* state: locked decisions, status, real vs synthetic, open questions. Companion to `CLAUDE.md` (rules) and `PIPELINE.md` (the plan). Update as work lands.

_Last updated: 2026-07-02 (project start)._

## What this project is

The registry-driven, self-maintaining harvest pipeline that gets **all major Indian architectural-material suppliers** (tiles, paint, wallpaper, sanitaryware, laminates, lighting, furniture, …) into one catalog: specs + images + honest pricing, embedded and retrievable, feeding the DSource AI Explore/Specify engine.

## Locked decisions (carried from DSource AI + PIPELINE.md)

- **Never fabricate data.** Every field carries `{value, confidence, source/basis}`; missing/estimated is structural, never silent.
- **Prices are observations, not attributes.** `price_observation` table with `basis` (`listed_mrp` / `dealer_quote` / `estimated_band`), `observed_at`, source. MRP labelled MRP, never "cost". >90 days ⇒ stale flag.
- **Surfaces need units.** `price_unit`, `coverage_sqft_per_box`, `size_mm`, `finish` mandatory for tiles/paint/laminates. BOM = area ÷ coverage → ceil boxes → +10% wastage.
- **Registry-driven.** New supplier = new row in `suppliers.csv`, never a new code path. Probe classifies tier before any scrape.
- **Politeness/legal.** robots.txt respected; ~1 req/2s per domain; raw payloads archived content-addressed; NO mass-scraping IndiaMART/Justdial; legal read before redistributing dealer pricing.
- **LLM agents in four slots only** (probe ambiguity, dedupe adjudication, parser repair, enrichment), each with an external verification signal. Everything else deterministic. Parser repairs ship only with a fixture regression test.
- **Stack (reused from DSource):** curl_cffi 4-tier harvest → Pydantic normalize → marqo-ecommerce-B embeddings → sqlite-vec (+ FTS5 hybrid) → novelty-gated enrichment (Gemini/Haiku/Opus) → cron refresh with yield-drift self-healing.
- **Orchestration:** SQLite `pipeline_jobs` queue + idempotent per-stage workers + cron. No Airflow/Prefect.
- **Tile anchor:** Orientbell first (published MRP/sqft — verified live 2026-07-02, e.g. ₹35–392/sqft range, per-product MRP pages). Kajaria/Somany/Johnson/Nitco harvested specs-only with `price=None` flagged.

## Current status

- ✅ Docs created: `CLAUDE.md` (updated with pipeline rules), `PIPELINE.md` (10-stage plan + build order), this file.
- ✅ Seed registry drafted: `suppliers.csv` (~85 suppliers across 15 categories, domain-confidence flagged — see Honesty ledger).
- ⬜ Repo not initialized / rules not copied (`.claude/rules/no-bloat.md`, `git-workflow.md` live in the DSource repo).
- ⬜ `.env` not set. **ANTHROPIC_API_KEY was truncated in the old repo (401) — paste the FULL key this time** (`read -s` into .env). Gemini key works.
- ⬜ No code exists yet in this project. Existing DSource modules (`harvest/`, `embeddings/`, `match`, `enrichment/`, 749 harvested products, calibrated bands) live in the DSource repo.
- ⬜ Old `data/india/manufacturers.csv` (95 verified suppliers) not yet merged into the new registry.

## Real vs synthetic (honesty ledger)

- **Verified real:** Orientbell publishes per-product MRP (checked 2026-07-02). Category assignments in `suppliers.csv` are high-confidence brand knowledge.
- **Unverified / probe-must-confirm:** most `domain` values in `suppliers.csv` are from knowledge, not live-checked — `domain_confidence` column flags high vs verify. `price_published` is `unknown` wherever not personally verified. **The Stage-1 probe script is the verifier; trust nothing in the CSV until probed.**
- **Synthetic:** nothing yet (no code).

## Open questions

Settled 2026-07-02:
1. ~~Repo split?~~ **RESOLVED: standalone repo** (`material-bank_data-scrape`). It owns its own registry + `catalog.db`; DSource consumes the catalog as an artifact later. Lean `Product`/`NormalizedProduct` re-declared here (not imported cross-repo).
2. ~~Same `dsource.db` or separate?~~ **RESOLVED: new `catalog.db` from the start.** `suppliers.csv` is the seed import; probe writes back into the `suppliers` table in `catalog.db`.
3. ~~v1 scrape scope?~~ **RESOLVED: probe all ~90 rows now** (probe is read-only + polite; it only gates later harvest). Harvest scope chosen from verified probe data afterward.

Still open (do not block Stage 0/1):
4. **Cron on the Mac vs manual runs** for now?
5. **Canonical HSN→GST table** — still missing; `derive_gst(category)` estimated-flag interim.
6. **Enrichment budget ceiling** for the first bulk run (Batches?).

## Top risks

1. Look-alike tile dedupe over-merges on CLIP alone — brand+size gates mandatory before any merge.
2. Reseller price vs brand MRP conflation would corrupt BOMs — Stage-7 separation is the guard.
3. Tier-3 WAF sites may block Playwright — mark `blocked`, move on; don't burn time.
4. Scraped MRP ≠ transaction price (dealer discounts run deep in tiles) — label honestly.
5. Domain rot in the seed CSV — probe validates every row before harvest.
