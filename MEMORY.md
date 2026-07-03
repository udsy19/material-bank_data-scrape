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

- ✅ Docs created: `CLAUDE.md`, `PIPELINE.md`, this file.
- ✅ **Stage 0 (registry) + Stage 1 (probe) built & run** (Build order step 1 done). `material_bank/` package: `db` (catalog.db + `schema_version`=1 + idempotent seed merge), `models` (honesty enums), `fetch` (curl_cffi chrome131, 2s/domain, backoff, www-fallback, raw capture), `robots`, `sitemap`, `probe` (tier ladder), `cli` (resumable, capped concurrency, coverage report). **60 tests, all offline.** No harvesting yet — stopped for approval.
- ✅ Seed merged: `suppliers.csv` (90) + DSource `manufacturers.csv` (103, copied to `data/seed/`), deduped on normalized domain → **175 suppliers** in `catalog.db`. Probe columns never seeded (probe is the sole verifier).
- ✅ **All 175 probed** (clean re-run after bug fixes). Coverage: shopify 25 · woo 10 · jsonld 13 · tier3 92 · unclassified 35 (ambiguous 13, blocked/WAF 6, unreachable 13, robots-disallow-root 3). **Priced (price_published=yes): 33 sites (~211k SKU est).** SKU estimate total ~555k.
- ✅ Tile anchor confirmed live: **Orientbell = jsonld + price=yes** (₹/sqft in PDP JSON-LD `offers`, `itemOffered:/sqft`), sku~4928. Kajaria/Nitco/Simpolo/Varmora/Sunhearrt/Cera/HRJohnson = tier3 (specs, Playwright); Somany = jsonld specs (price unknown); Qutone = jsonld priced.
- ⬜ `.env` not set — not needed for the deterministic probe (no LLM calls). Needed later for enrichment/dedupe/repair slots. **Paste FULL ANTHROPIC_API_KEY** (old repo 401 = truncated key).
- ⬜ 13 ambiguous rows await the probe-adjudicator subagent (LLM slot #1); 13 unreachable need manual domain correction (several have working alternates already in-registry, e.g. marshallswallcoverings.com→marshallsindia.com, purpleturtles.com→thepurpleturtles.com).
- ⬜ Harvest scope (which priced rows go first) — awaiting decision. Build order step 2 (price_unit/coverage schema + tile BOM) or step 3 (Orientbell harvest) next.

## Real vs synthetic (honesty ledger)

- **Verified real (probe-confirmed, 2026-07-02):** 33 domains publish prices; Orientbell per-product MRP/sqft confirmed in live PDP JSON-LD. Tier/robots/sku for all 175 written from live probe, not knowledge.
- **Probe corrected the seed (why "trust nothing until probed" earned its keep):** several `verified`/`high` seed domains were wrong or dead — `marshallswallcoverings.com`(verified) unreachable while `marshallsindia.com` is a priced Shopify; `purpleturtles.com`(verified) unreachable while `thepurpleturtles.com` is priced; many brand bare-domains serve only on `www.` (cera, greenlam, hrjohnson, ddecor). Redirects captured: godrejinterio→interio, jaquar→global.jaquar, grohe.in→grohe.com, obeetee.com→obeetee.us(US, use obeetee.in).
- **Still unverified:** 13 ambiguous (mixed CMS signals — subagent queue), 6 WAF-blocked (403), 13 unreachable (need domain fixes). `price_published=unknown` for 142 rows — genuinely undetected, not "no".
- **Synthetic:** nothing. No prices/specs fabricated; probe only classifies.

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
