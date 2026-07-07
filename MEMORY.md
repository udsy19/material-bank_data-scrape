# Material Bank India — Memory (living state)

## STRATEGIC PIVOT (2026-07-07) — read VISION.md

- The catalog **is** the product: B2B material-intelligence platform (intelligence-first, commerce-later). Reference bar: Material Bank US (brands-pay flip, enrichment/facets) + MaterialDepot India (dual pricing, BOM, commerce). Our wedge: breadth + **cross-supplier price observations** + the trust contract (per-field provenance, completeness scores, publish gate).
- **CLAUDE.md rewritten** for this product (new hard rules: publish gate, autonomy-first, standards-grounded taxonomy, LLM-only-adds-content/estimated). **VISION.md** holds business model, autonomy flywheel (planner turns metric gaps into pipeline_jobs stages), roadmap A–F, north-star metrics.
- **NEXT BUILD: Phase A — Foundation of Trust**: `canonical_products` + category-specific completeness scoring + `metrics` snapshots table + QA/trust dashboard + publish gate + `mb-planner` timer. Then B (deterministic enrichment: taxonomy v1 w/ OmniClass mapping + extractors + generalized PDF mining + dual-unit prices), then C (entity resolution/golden records → price comparison), then D (LLM enrichment — **blocked on ANTHROPIC/GEMINI keys**).
- Decisions owed by owner: ANTHROPIC key (Gemini key received 2026-07-07), Act-1 pricing posture, product name/domain.

## Phase A — Foundation of Trust: LIVE (2026-07-07)

- **Schema v9**: products carry the trust contract (`completeness` 0–100, `verification_tier`, `publish_ready`, `scored_at`) + `metrics` time-series table. `quality.py` = category-aware scoring (surfaces held to the units bar), deterministic contradiction checks, publish gate (surface≥70 / default≥60 AND not unverified); human tiers never auto-downgraded. `planner.py` (flywheel v0) runs in every hourly sweep: score_all → snapshot_metrics → gap report.
- **First live scoring (143,746 products, 10s)**: 131,861 publish-ready (92%) · 18 unverified · **worst gaps quantified**: laminates|acrylic|cladding 0/1998 ready (median 41 — no units), tiles 4,222/12,935 (median 59 — specs-only Kajaria/Somany below the surface bar). **These gap lists ARE the Phase B work-list.**
- **API**: `/api/catalog` = publish-GATED external surface (tiles: 4,222) · `/api/products` = internal full view w/ trust fields (tiles: 12,935) · `/api/quality` = cockpit (report + trends). Dashboard has publish-ready + median-completeness tiles.
- Deviation from VISION §7 (deliberate, no-bloat): no separate `canonical_products` table yet — trust columns live on `products` until Phase C merging justifies a canonical layer.
- **Gotcha fixed**: writing on a connection whose open read-cursor snapshot went stale ⇒ SQLITE_BUSY_SNAPSHOT instantly (bypasses busy_timeout by design under WAL). Rule: **fully materialize reads before writing on the same connection** (score_all does read-then-write).
- **NEXT: Phase B — deterministic enrichment** targeting the measured gaps: attribute extractors (size/finish/color from titles+descriptions), generalized PDF spec-mining (Somany/Nitco/Simpolo), taxonomy v1, dual-unit price normalization. Gemini key available for the classify-fallback slot.

## Phase B core — deterministic enrichment: LIVE (2026-07-07)

- **Schema v10** (description, color, color_family, thickness_mm, enriched_at). `extract.py`: conservative extractors (size w/ ft/in/cm→mm; bare NxM only at mm scale; finish vocab; color→family; sheet-coverage derivation). `enrich.py`: title_pass (every sweep, free) + `enrich` queue stage (per-supplier PDP refetch, 400/supplier/sweep, ld+json description/additionalProperty, enriched_at resume marker). NULL-only COALESCE writes — **harvested values never overwritten**; everything `basis='derived'` with source `extracted:title|pdp`.
- **First live run:** title-pass filled **129,160 fields on 70,343 products in 10s** (+555 over the gate). Refetch loop proven 8/8 jobs: Somany finish 0→254, descriptions storing. **Flywheel closes its first full loop: measure → seed → enrich → re-score, hourly, unattended.**
- **Honest finding:** the stubborn gap categories have bare titles AND thin ld+json (Somany size yield low; Kajaria PDPs JS-rendered) — the remaining lift needs **generalized PDF spec-mining (Somany/Nitco tech sheets) + Playwright-rendered extraction**, which is the next Phase B chunk. Also owed: taxonomy v1 (OmniClass mapping), dual-unit price serving, image color-family.

## CI/CD + durability (2026-07-07)

- **GitHub is now the source of truth**: main pushed (was local-only!). VPS is a real clone with a write **deploy key** (added via gh api, id 156633150).
- **CD**: `mb-deploy.timer` (5 min, root) — origin/main moved ⇒ reset --hard + pip-sync-if-changed + unit-reinstall-if-changed + restart. **Proven end-to-end** (push → live in <4 min). Deploy flow is now: commit locally → `git push` → done. No more rsync.
- **Backups**: `mb-backup.timer` (6h) — full local snapshot (keep 2) + essential dump (all tables except embeddings/FTS, ~11MB gz for 141k products) **verified-by-restore** then force-pushed to `vps-backups` branch. Restore runbook in `deploy/README.md` (backup.py restore rebuilds FTS; embed worker refills vectors).
- **GEMINI_API_KEY** stored in `.env` (600, gitignored) local + VPS, plumbed into all services via `EnvironmentFile` — **Phase D Gemini slots unblocked** (classification, near-dup enrichment). Anthropic key still owed for Haiku/Opus slots.

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

## VPS DEPLOYMENT (2026-07-07) — runs 24/7

- ✅ **Deployed to VPS `46.202.179.28` (Ubuntu 24.04, 2 vCPU, 7.8GB RAM, 96GB disk).** App at `/opt/material-bank`, user `mb`, venv `.venv`. SSH: `root@46.202.179.28` (password held by user, not stored here).
- **3 systemd services (auto-restart, survive reboots):** `mb-harvest` (producer sweep + bespoke Kajaria/Steelcase + self-heal, 6h refresh), `mb-embed` (continuous consumer), `mb-api` (search+dashboard on `:8000`, **public → http://46.202.179.28:8000**).
- **Handoff:** stopped local pipelines, checkpointed WAL, rsync'd code + catalog.db (670MB). VPS resumed harvest from exactly where local stopped (source_url + requeue_stale_running). Live catalog: **138k products, 109k priced, 138k vectors, 49 suppliers**.
- **Deploy gotcha fixed:** open_clip re-pulls a PyPI torchvision that breaks a CPU torch wheel (`torchvision::nms does not exist`) — bootstrap now force-reinstalls the matched `torch/torchvision ==+cpu` pair LAST. See `deploy/` (bootstrap.sh, systemd/, harvest_service.sh, README.md).
- **Ops:** `journalctl -u mb-harvest -f`; `systemctl restart mb-*`; add supplier row → next 6h sweep harvests it. Redeploy code: rsync to `/opt/material-bank` + `systemctl restart`. No TLS/nginx yet (raw `:8000`).

## Kajaria + PDF-spec capability (2026-07-06)

- ✅ **`harvest/kajaria.py`** — Kajaria has no catalog/API; PDPs render specs as icon SVGs. But static HTML has name + correct product image + a **technical-PDF link**; those PDFs (parsed with **pdfplumber**, MIT) give real **size/thickness/coverage_sqft_per_box** — the surface field even Orientbell lacks. Static fetch (no Playwright) → distinct correct images. Specs-only (no price; price_unit/finish flagged missing). PDF parse cached per collection.
- **`DOMAIN_HARVESTERS`** (in `harvest/run.py`) routes specific domains to bespoke harvesters (kajariaceramics.com → harvest_kajaria); worker checks it before tier DISPATCH. Reusable pattern for other custom sites.
- **Reusable PDF-spec ingestion** (`specs_from_text` / `parse_technical_pdf`) applies to other spec-only tile brands (Somany/Nitco/Simpolo) + building-material PDFs → toward Stage-6 enrichment.
- Registry repairs done: geeken.in→jsonld (~2400), hindware.com→jsonld (~3500, was hindwarehomes.com dead); whiteteak.com/ozone.in revived to tier3; 6 ambiguous→tier3; marshallswallcoverings→dup. Seed CSV updated + committed.

## System state (2026-07-06, uncapped run in progress)

- **107,049 products · 84,398 priced · 107,049 text vectors (fully embedded+indexed) · 45 suppliers · 4,203 image vectors.** Server live on this.
- Uncapped JSON-LD harvest resuming (jaipurrugs→~38k, wallmantra 13k, somany 7.9k, wakefit 6.1k, urbanladder growing). **source_url resume fix (schema v8) unstuck the giants** (were frozen at 499). qutone dead-lettered (no sitemap — honest). Session teardown kills the bg harvest, but each resume is efficient + durable (queue + source_url) — nothing lost.
- **Resume commands:** harvest → `python -m material_bank.harvest.worker --tiers jsonld --workers 8 --jsonld-limit 0 --reset`; full loop → `python -m material_bank.pipeline`; server → `uvicorn material_bank.serve:app`.

## Earlier milestone (2026-07-05)

- **36 suppliers harvested · 66,387 products · 63,577 priced (96%) · 77,612 price observations · 66,387 text vectors · 4,203 image vectors · 34 categories · 108 quarantined.**
- Full pipeline live end-to-end: **probe → harvest (shopify/woo/jsonld/tier3) → normalize (units+provenance) → price observations → embed (text+image shared space) → FTS5+vector hybrid retrieval → FastAPI serving → browser dashboard**, all deterministic except the 4 LLM slots (none invoked yet).
- **Still owed (flagged, not done):** Stage-4 entity resolution (cross-brand dedupe); coverage backfill for tile BOM whole-box counts; wallpaper/surface `price_unit` normalization; Stage-6 enrichment; Stage-9 cron/drift/repair loop; the remaining ~90 tier3/jsonld suppliers (Playwright machinery exists, just not run at scale).

## Speed + durability + orchestration (2026-07-06)

- ✅ **Parallel harvest** (`harvest/parallel.py`): the ~1 req/2s budget is PER DOMAIN, so distinct domains harvest concurrently (own Fetcher + own connection, WAL+busy_timeout). ~N× wall-clock; each domain still ≤1 req/2s. Single giant domains stay serial by the rule → capped.
- ✅ **Durable job queue with retry** (`jobs.py` + schema **v6** `pipeline_jobs`): one row per (stage,target); atomic claim (only one worker wins), exponential-backoff retry, **dead-letter after max_attempts** (last_error kept), `requeue_stale_running` (recover crashed workers), `retry_dead`. **Nothing silently lost.**
- ✅ **Queue-driven harvest** (`harvest/worker.py`): seed one job/supplier, pool of workers claim+dispatch+report; transient (unreachable) → retried, permanent → dead-lettered. jsonld harvester signals `reachable`.
- ✅ **End-to-end orchestrator** (`pipeline.py`): harvest (queue+retry) → embed (resumable) → FTS rebuild → health report; idempotent, the loop cron drives (Stage 9).
- ✅ **Generic JSON-LD harvester** (`harvest/jsonld.py`): generalizes Orientbell (sitemap→PDP→schema.org Product); priced or specs-only, no fabricated price. Runs via queue with per-supplier cap.
- ✅ **Visibility:** `/api/pipeline` endpoint + dashboard footer line show job counts + dead-letters. Run: `python -m material_bank.pipeline` or `python -m material_bank.harvest.worker`.
- **Tests: 156 backend + 13 browser e2e (redesigned dashboard), all green.** schema_version **6**.

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
