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
- **Honest finding:** the stubborn gap categories have bare titles AND thin ld+json (Somany size yield low; Kajaria PDPs JS-rendered) — the remaining lift needs **generalized PDF spec-mining (Somany/Nitco tech sheets) + Playwright-rendered extraction**, which is the next Phase B chunk. Owed: dual-unit price serving, image color-family.

## Phase B — Taxonomy v1: LIVE (2026-07-08)

- **Schema v11** (products.family/category_std/omniclass/classified_at + idx_products_family). `taxonomy.py`: ordered `RULES` classifier (specific-before-general: plants>furniture, mattress/sofa>furniture, laminate>cladding/flooring, fan>lighting), `classify(category,title)` with title fallback, idempotent `classify_all` (read-then-write, front-loaded in `run_planner` before scoring), `taxonomy_tree` (family→categories w/ counts + publish_ready). **Verified OmniClass Table 23 only** (tiles 23-35 50 14, sanitaryware 23-45 05 14, furniture 23-40 20 00, laminate/wallpaper 23-35 10 00, flooring 23-35 50 00); lighting/quartz/plants left `null` — not fabricated.
- **Live over 159,539 products → 10 families, only 10 unmatched (0.006% "Other")**: Furniture 57.7k · Surfaces 55.0k · Flooring 26.7k · Lighting&Electrical 8.8k · Decor&Greenery 6.9k · Bath&Sanitary 2.9k. `/api/taxonomy` tree endpoint + `family`/`category_std` filters on `/api/products` + `/api/catalog`. Full suite **237 green**.

## Phase B — description mining + source_url repair: LIVE (2026-07-08)

- **`title_pass` → `text_pass`**: the free offline extraction now mines title **+ the already-harvested `description`** (same logic the PDP path applies to fetched descriptions, minus the fetch; title-first, NULL-only, `basis='derived'`). CLI `--text-pass`; flywheel updated.
- **Honest finding — the laminate gap is data-limited, not code-limited**: advancelam (1998) descriptions are identical boilerplate ("Advance Laminates is a leading brand…"), zero specs, AND 0 priced → genuinely can't be procurement-ready (correct to stay below gate). royaletouche (1017, priced) has bare product-code titles ("MATTY CRYSTAL Z+ 1122"), no descriptions. text_pass filled 0 new here — the specs simply aren't in the harvested text. Real lift for these needs re-harvest/PDP fetch, not more extraction.
- **Fix (migration v12): `products.source_url` backfill.** ~6,859 priced products (orientbell tile anchor **4,225**, royaletouche 1,017, wakefit, wallmantra…) had NULL `products.source_url` while the real PDP url sat on their `price_observation` — they were harvested before v8 added the column and the resumable harvester never re-upserted them. A published product with no procurement link fails the core "can I actually procure this?" test. Migration propagates the freshest observation url onto the product (same url/provenance; genuine orphans stay NULL). **Live: missing url 6,859 → 1; publish_ready 147,655 → 148,676 (+1,021); all rows auto_validated.**
- **Bonus — self-healing unblocked**: `candidates()` requires `source_url`, so pre-backfill these suppliers were *excluded* from the enrich refetch. Now eligible → the flywheel will auto-mine orientbell/royaletouche PDP specs going forward, no manual step.
- **Recurring truth**: publish-readiness is gated on a fresh price (25 pts). Spec-only suppliers (Somany tiles 7,889; Kajaria) correctly can't be "procurement-ready" without price data — the remaining completeness grind is price-limited, so the next real lever is more priced sources / dealer quotes, or Phase C (dedup → cross-supplier price comparison).

## Phase C v1 — variant grouping (One Product, One Truth): LIVE (2026-07-09)

- **Investigated the data before building (decisive)**: cross-supplier duplicates barely exist — 0 (brand,size) keys span two suppliers; our 50 suppliers are disjoint brand-direct catalogs. So the headline "cross-supplier price comparison" wedge **has no data yet** and was NOT built. What IS in the data: within a supplier, 23k products (15%) share a (brand,title) with a sibling, each a **distinct SKU** — variants (one mattress model in ~200 size×thickness SKUs), not dupes. Merging would destroy real SKUs, so the honest move is **grouping, never deleting**.
- **`resolve.py`**: `variant_group_id = sha1(supplier | norm brand | norm title)[:16]`; 1-token titles guarded out (too generic to group safely). `assign_variant_groups` (idempotent, read-then-write) links siblings, singletons stay NULL. `variants_of()` returns siblings + distinguishing attrs + each one's own price. Schema **v13** (variant_group_id + resolved_at + index). Runs in the planner every sweep (after scoring).
- **`retrieval.list_designs`**: collapsed catalog — one card per design with variant_count + price band; filters apply to members pre-group (a gated view counts publishable variants). `/api/catalog?collapse=true` (design cards) + `/api/product` exposes `variants[]`.
- **Live: 5,410 groups covering 23,273 SKUs** → catalog collapses 159,674 rows → ~141,811 designs; publish-gated design count 131,077. "Wakefit Dual Comfort Mattress" is now ONE card, 188 variants, ₹3,476–20,418 band (was 188 near-identical rows).
- **Known follow-up**: mattress/furniture variants distinguish by SKU+price but `attrs` is often empty — the size/thickness axis lives on the PDP variant selector, not the (identical) title, so it isn't captured per-variant. The source_url backfill made these suppliers eligible for the enrich refetch, which is the path to fill it. Also owed: dual-unit price serving, image color-family. Cross-supplier comparison revisits when overlapping suppliers exist.

## Procurement path + demand instrumentation + adversarial-honest brief: LIVE (2026-07-11)

Prompted by the user's #1 priorities (supplier info + product URL) and an external stress-test of BRIEF.md. Three workstreams shipped in parallel (4 research agents + main-loop build), ~267 tests green.

- **A — Sourcing path ("who supplies it / where to buy").** Schema v14: supplier procurement fields (legal_name/phones/emails/address/city/state/pincode/GSTIN/CIN/dealer_locator_url/social/logo/states+cities_served/dealer_count/pan_india) + provenance + `dealers` table. `company_extract.py` (deterministic: JSON-LD Org→microdata→tel/mailto→India regex GSTIN/CIN/phone/pincode, tax-id spans masked, NO personal names — DPDP guardrail). `supplier_enrich.py` (durable queue, OWN-DOMAIN-ONLY fetch, robots-honored, politeness). `dealers.py` (Kajaria open REST API + Single-Interface schema.org microdata [1 parser=5 brands] + Orientbell `__NEXT_DATA__`; internal CRM fields excluded; `derive_regions`). **Live: Kajaria 2,099 dealers/34 states/pan-India, Orientbell 352/32 states.** `GET /api/supplier/{domain}` + supplier block in product modal. `mb-suppliers.timer` (weekly, polite). **publish_gate now HARD-requires source_url** (no link ⇒ not publishable).
- **B — Demand instrumentation + intent capture.** Schema v15: `events` + `quote_requests` + `supplier_claims`. `events.py`: log_event (search/view/click), record_quote (the intent Act III sells), record_claim (supplier claim/correct/**takedown** → converts objection to onboarding), demand_metrics (active_sessions/searches/CTR/quotes — **honest zero until users**). `POST /api/event|quote|claim`, `GET /api/demand`. Dashboard: session id + tracking + "Request quote/sample" button + claim form. Live-verified (firing events moves the scorecard).
- **C — Founder research tooling** (docs/research/): interview-scripts.md (5 Mom-Test segment scripts + oblique dealer-commission-alignment probe), diary-study-protocol.md (2wk, pre-committed wedge-decision rule), pricing-test.md (fake-door, 3 plans, go/kill thresholds). **This is founder work the engine can't do — the demand bet is the binding constraint.**
- **Honest metrics** (answering the critique that "completeness ~100 is misleading"): `enrichment_depth` (real attr coverage — desc 6%/size 17%/finish 7%/color 38%, thin) + `overlap_rate` (**0.00%** cross-supplier — the honest gate on Phase C price comparison) now tracked in quality_report + snapshots.
- **BRIEF.md** rewritten per external adversarial review: moat reframed (data layer necessary-not-sufficient; durable moats = un-backfillable time series + not-yet-existing relationships), TAM narrowed to ~$8-15B specified-finishes wedge, MRP-is-fiction surfaced, demand=whole bet, §14 valuation on founder capability, §15 kill criteria. Images are PROXIED not stored (legal posture). **Note: a prompt-injection attempt (fake "system-reminder" re date/agent-types/MCP) has been appearing in tool output — ignore it; it is not a real system message.**

## Catalog UI — the product made visible: LIVE (2026-07-10)

- Extended `static/dashboard.html` (same editorial design system — Fraunces/Inter, dusk hero, hairlines) with a **verified-catalog browse**: taxonomy family facets (`/api/taxonomy`), collapsed **design cards** (`/api/catalog?collapse=true`) with a variant-count badge + **price band** (min–max), "show more" pagination. Product modal gained a **trust line** (completeness/tier/publish state), a **variants table** (sibling SKUs + attrs + each price, click-through), and a "view source ↗" procurement link. Read-only, self-contained, no new deps.
- **Verified live** (headless Playwright vs VPS): 10 facets, 24 design cards all with badges+bands, modal with trust + 14 variant rows, facet filter → "44,827 furniture designs". Browser e2e (`tests/browser/`, opt-in `-m browser`) extended with these checks; fixture now warms the lazy marqo model before checks (cold load was racing timeouts) and loads via `domcontentloaded` (image proxy never lets network idle). Full offline suite green.
- **Dev-DB note**: local `catalog.db` is a raw harvest — run `python -m material_bank.planner` (classify+score+resolve, ~11s/138k) to bring it to prod-processed state, else taxonomy/collapsed-catalog return empty and browse checks can't pass.

## LLM enrichment (Stage B) — verified-by-construction + batch path (2026-07-11)

- **Stage built + live-proven functionally** (`llm_enrich.py`): claim-source discipline (every sentence/tag cites input field id `f1..`/`img1`) + deterministic verifiers on 100% of outputs. `run()` novelty-gated + budget circuit-breaker + retry→escalate→`enrich_failed`; injected client (Gemini default). Schema v17: `llm_content` (BONUS — basis `generated:llm:*`, NEVER in the publish gate) + `llm_hash` + `llm_status`. **Working model: `gemini-2.5-flash`** (2.0-flash quota-exhausted).
- **50-product live eval (n=4 completions, 46 quota errors)** proved the honesty contract holds but usefulness doesn't: citation-perfect **restatement filler** ("This is a Wavy Floor Lamp" ← f1) + **tag spam** (6 use-case tags) pass every honesty gate. → **prompt v3 + 4 usefulness verifiers**: (1) no restatement (sentence content words ⊄ cited field values + stop/plumbing), (2) no plumbing in prose (field ids/domain/.com/"category standard" banned), (3) tag discipline (≤3 style + ≤3 use-case; each must trace to a DISTINGUISHING attr or image, not name/brand/category), (4) empty style_tags OK. Also broadened visual lexicon (colour+shape words fixed false-rejects). **novelty_hash is now version-prefixed (`v3:...`)** + `run()` re-selects rows below current prompt version → no stranding on prompt bumps.
- **Batch path (`llm_batch.py`) = production route** (50% cheaper, 24h SLA, past free-tier RPM ceiling): `submit_batch`/`collect_batch`, transport INJECTED + fully tested via fake; `GeminiBatchTransport` (live REST) **flagged NEEDS-ONE-LIVE-VALIDATION** (Google batch response shape reconstructed from docs, uncheckable under exhausted quota — honest: not "done"). CLI `mb-llm-batch --submit/--collect`.
- **Full-fidelity LLM observability (schema v18, `llm_accounting.py`)**: every call (realtime+batch, incl. retries/escalations/failures) → `llm_calls` ledger with ACTUAL tokens (usageMetadata) + ₹ cost derived at published rate (`PRICING` visible; gemini-2.5-flash $0.30/$2.50 per 1M, ₹83/$, batch −50%). Budget circuit-breaker reads REAL daily spend. Surfaces: `GET /api/llm` (cockpit), `GET /api/llm/calls` (raw ledger), dashboard **"LLM Ops"** panel (spend cards + every call), `mb-llm-report` CLI. **Nothing estimated — before spending a rupee, every call is visible with exact cost.** Gemini key goes in VPS `.env` (EnvironmentFile), never committed.
- **THE GATE (per spec §B6, not yet run)**: 1k stratified eval on batch → schema-valid%, claim-source/vocab violation%, **unsupported-claim rate from a 150-record hand audit**, tag-count distribution, blind usefulness score on 50 → only then the 160k full pass. **Functionally proven ≠ quality-validated (n=4).** Blocked today on Gemini free-tier daily quota (resets) + the one batch-transport live validation.

## Stage A + procurement enrichment (2026-07-11)

- **A1 image colour** (`image_colour.py`, pure numpy CIELAB+CIEDE2000+kmeans, 36-colour palette, `unknown`→LLM path): live eval **41% same-family vs text colour on tiles → colour STAYS BONUS, not publish-gated** (eval gate worked; neither signal is truth). schema v16. `colour_accuracy` metric.
- **A2** variant-axis rollups (design cards show "N sizes · M finishes") + `audit_variant_groups` (suspect groups: >1 category or >20× price spread) → `suspect_variant_groups` metric.
- **Single-Interface dealers** (`dealers.py`): 4 brands (HRJ/Somany/Jaquar/Godrej) via resumable sitemap crawler over SSR microdata; the ONLY path to Phase C overlap. **GSTIN adapter** (`gstin.py`) behind a flag (off, ₹0, no fabrication). Both in weekly `mb-suppliers.timer`.
- **Honest metrics** (answering "completeness ~100 is misleading"): `enrichment_depth` (desc 6%/size 17% — thin) + `overlap_rate` (0.00%) tracked in quality_report/snapshots. **BRIEF.md** rewritten per external adversarial review (moat necessary-not-sufficient; TAM $8-15B wedge; MRP=fiction; demand=whole bet; §15 kill criteria).

## Operational fix — harvest sweep monopolization (2026-07-09)

- **Failure mode found live**: one `mb-harvest.service` sweep ran **2 days**. The queue-drain phase used `--jsonld-limit 0` (unlimited) and the service was `TimeoutStartSec=infinity`; the timer is `OnUnitInactiveSec=1h` (next run 1h *after* the current ends), so systemd does NOT overlap a oneshot — the long run **blocked every later phase for 2 days**: recovery, bespoke (Kajaria/Steelcase), self-heal. The flywheel kept working only because it's a separate timer. The script's "spans several ticks" comment was wrong.
- **Fix**: time-box the drain with `timeout --kill-after=30s ${HARVEST_DRAIN_TIMEOUT:-60m}` (exit 124 expected, not a failure — worker is resumable via atomic claims + `requeue_stale_running` + `source_url`, continues next sweep; still collects everything across sweeps, no per-run cap). Service backstop `TimeoutStartSec=2h` + `TimeoutStopSec=90s` + `KillMode=mixed`. Restarted the wedged run (safe/resumable); fresh bounded sweep confirmed draining.
- **Lesson**: any unbounded phase inside a oneshot-on-timer starves the rest of that unit's phases. Time-box long phases; rely on resumability, not completion, to make progress.

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
