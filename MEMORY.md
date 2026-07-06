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
- ✅ **Build order step 2 done:** `NormalizedProduct` (per-field provenance + explicit `missing[]`; surface-units validator enforces price_unit+coverage+size_mm+finish for tile/paint/laminate/floor/veneer), `PriceUnit` enum, `bom.boxes_for_area` (ceil area×(1+10%)/coverage, epsilon-guarded against float over-order). `catalog.db` migrated to **schema_version 2** (products table, UNIQUE(brand,sku)); 175 suppliers intact. 71 tests green.
- ✅ **Build order step 3 DONE (harvest half) — Orientbell fully harvested.** schema_version **3** (price_observation append-only + quarantine). `harvest/orientbell.py` parses Magento PDPs: price/unit/brand/title from Product ld+json, sku via `data-sku`, size via magento `"size"`, finish via Finish anchor; **coverage_sqft_per_box honestly flagged missing on every row (not on PDP)**. MRP → `price_observation` basis=`listed_mrp` (never products). **Final: 4,225 products · 4,228 MRP observations · ₹35–2562/sqft (avg ₹274) · all per_sqft · 85 quarantined (all 404 dead sitemap URLs) · 59 non-product pages skipped.** Vendor test SKUs (`Test33`) guarded + purged. BOM verified end-to-end (120 sqft ÷ 15.5 sqft/box +10% → 9 boxes). 80 tests green.
  - Sitemap `ositemap.xml` = urlset 4928 (4374 single-segment PDP candidates; info pages skipped via no-Product-ld+json). price_unit always per_sqft; **coverage never published → BOM needs coverage backfill (dealer sheets / box-size data) before whole-box counts are real.** 12 irregular tiles (wavy/fishscale) legitimately have size flagged missing.
- ✅ **Embeddings DONE (text) / IN PROGRESS (image).** marqo-ecommerce-B (Apache-2.0, 768-dim, open_clip) behind provider-agnostic `Embedder`; one shared text+image index in `catalog.db` (schema **v4** `embeddings` table). **All 4,225 products text-embedded (30s CPU); semantic search validated live** (cement/wood/marble/mosaic queries return correct tiles, cosine 0.70–0.83). Image back-match path built + validated (15 sample); **full ~4,210 image embed running in background** (~2.4h: re-parse PDP for image_url + download + encode, resumable). 94 tests green.
  - **DEVIATION (flagged): NOT sqlite-vec.** This platform's `sqlite3` can't load extensions and no `pysqlite3` wheel exists → vectors are normalized float32 BLOB + numpy cosine (ms at 4k scale), behind `VectorStore` so vec0 drops in later on an extension-capable sqlite build. No `.env`/API key needed — embeddings are fully local.
  - New deps: torch, open_clip_torch, transformers, sqlite-vec (installed, unused), pillow, numpy. `image_url` now captured on future harvests; existing rows backfilled by the image job re-parsing PDPs.
- ⬜ Deferred: probe-adjudicator subagent for 13 ambiguous + 13 unreachable (domain fixes). Not blocking.

## Multi-supplier harvest (2026-07-05)

- ✅ **Generic registry-driven harvesters** (`harvest/shopify.py`, `woocommerce.py`, `run.py` driver, `common.build_product`): dispatch by probed `scrape_tier`, new supplier = new row, no per-site code. Orientbell refactored onto `build_product`. `db.connect` now WAL + busy_timeout for concurrent harvest+embed writers.
- ✅ **35 suppliers harvested (all reachable) → 66,380 products · 77,620 price observations · 96% priced · 62k with image_url · 86 quarantined.** Biggest: giffywalls (~14k, .com/.in auto-deduped via (brand,sku) upsert), imperialknots 8.1k, lifencolors 6.2k, obeetee.in 5.4k, ugaoo 4.7k, marshallsindia 4.4k, orientbell 4.2k, thedecorkart, purpleturtles, crompton, bajaj, spaces, trustbasket, jainsonsemporio, royaletouche.
- **Honest gaps:** specs-only Woo sites (advancelam laminates 2k, stellarglobal, quantra quartz, indigopaints, astralpipes, vantageindia) store products with **no price** (their Store API omits prices) — flagged, not faked. Shopify observation `price_unit=None` (per-item implicit). Per-variant SKUs (colors/sizes = distinct rows). ₹0 samples skipped.
- ⬜ **Stage-4 dedupe still owed:** giffywalls .com/.in collapsed by luck of shared SKUs, but cross-brand look-alikes and reseller/brand overlaps (Pepperfry-class) need the real entity-resolution pass.

## Serving + retrieval + UI + tier3 (2026-07-05)

- ✅ **Retrieval (Stage 8):** schema **v5** FTS5 index (trigger-synced) + `retrieval.py` hybrid search = FTS5 keyword ∪ vector semantic → reciprocal-rank fusion → freshest `price_observation` + basis + >90d stale flag. Validated over 66k catalog.
- ✅ **Serving:** `serve.py` FastAPI (factory-built, testable). `/api/stats`, `/api/match` (hybrid), `/api/product` (detail + observation history + visually-similar via image/text back-match), `/api/image` (concurrent lock-free proxy). Embedder warmed + vectors preloaded (matrix cache) at startup. Run: `uvicorn material_bank.serve:app`.
- ✅ **Dashboard:** `static/dashboard.html` — coverage tiles + live semantic search, result cards with proxied images + price/basis, product modal (warm-paper/ink/terracotta). **Fixed:** image proxy held the DB lock → serialized card images; now 24/24 load concurrently.
- ✅ **Browser tested (Playwright/Chromium):** `tests/browser/` — 13/13 e2e checks pass (self-launches uvicorn), screenshots in `reports/screens/`. Fast suite stays offline; browser marked `-m browser`.
- ✅ **tier3 (build step 4):** `harvest/tier3.py` generic Playwright harvester — render JS PDPs → post-render JSON-LD (auto) or honest specs-only fallback (title+image, specs flagged missing, no invented price). `drop_shared_default_images` guard (nulls default-image heuristic false-positives). Live: 15 Kajaria tiles harvested specs-only, searchable, honestly unpriced.
- **Deps added:** fastapi, uvicorn, playwright(+chromium), httpx.
- **Tests: 128 backend (all offline) + 13 browser e2e, all green.** schema_version **5**.

## FINAL system state (2026-07-05)

- **36 suppliers harvested · 66,387 products · 63,577 priced (96%) · 77,612 price observations · 66,387 text vectors · 4,203 image vectors · 34 categories · 108 quarantined.**
- Full pipeline live end-to-end: **probe → harvest (shopify/woo/jsonld/tier3) → normalize (units+provenance) → price observations → embed (text+image shared space) → FTS5+vector hybrid retrieval → FastAPI serving → browser dashboard**, all deterministic except the 4 LLM slots (none invoked yet).
- **Still owed (flagged, not done):** Stage-4 entity resolution (cross-brand dedupe); coverage backfill for tile BOM whole-box counts; wallpaper/surface `price_unit` normalization; Stage-6 enrichment; Stage-9 cron/drift/repair loop; the remaining ~90 tier3/jsonld suppliers (Playwright machinery exists, just not run at scale).

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
