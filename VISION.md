# VISION — India's Material Intelligence Platform

_The single source where everything an architect needs is in one place: organised, classified, enriched, verified, priced, procurable._

Working name: **Material Bank India** (this repo). Strategy locked 2026-07-07 after studying Material Bank (US) and MaterialDepot (India) as reference products.

---

## 1. The vision

Every interior/architecture project in India starts with the same broken loop: browse 40 supplier sites with incompatible catalogs, call dealers for prices, collect PDFs, guess at specs, re-verify everything manually. **We replace that loop with one trusted database**: every material a project needs — tiles, laminates, sanitaryware, lighting, furniture, surfaces, hardware — as *canonical, classified, enriched, provenance-tracked product records* with live multi-source pricing, honest gaps, and procurement paths.

Not a marketplace first. Not a scraper. A **product graph with a trust contract**: every field says where it came from, how confident we are, and when it was last verified. That's what lets a B2B buyer *check* the data — and what none of the supplier sites, and neither reference player, expose.

## 2. What the reference players teach us

**Material Bank (US)** — [free for designers; manufacturers pay](https://www.materialbank.com/how-it-works) recurring monthly fees by SKU count + per-sample fees (~50% of ARR from manufacturer subscriptions), plus data/insights revenue. 400+ brands, ~400k materials, 100k+ members; robotic Memphis hub consolidates overnight sample boxes. ([Forbes](https://www.forbes.com/sites/amyfeldman/2021/05/06/the-latest-hot-marketplace-material-bank-raises-100-million-to-source-architectural-and-construction-products---with-help-from-robots/), [TechCrunch](https://techcrunch.com/2020/04/30/material-bank-a-logistics-platform-for-sourcing-architectural-and-design-samples-raises-28m/), [Bain Capital Ventures](https://baincapitalventures.com/insight/material-bank-ceo-adam-sandow-on-building-an-unstoppable-business/))
**The lesson:** the end-state business is *brands paying to be well-represented in front of aggregated architect demand*. Brands *supply* the data. Scraping is only the cold-start.

**MaterialDepot (India)** — Bengaluru, YC + [Accel/Stellaris $10M Series A](https://www.indianretailer.com/news/funding-alert-home-interiors-startup-material-depot-secures-10-mn-series-accel-stellaris), [~$14.1M total, profitable at ~$5M ARR](https://tracxn.com/d/companies/material-depot/__o2cL6O4OE7gCqCHVXQIhx8un5Q1XJA_oDj1WmkSH3M0). Commerce-first: curated listings, guaranteed pricing, own logistics, 3D visualizer, image search.
**The lesson:** the India model works — but they're *narrow-and-curated commerce*. The open flank is **breadth + data depth + trust**: they can't show cross-supplier price comparison or provenance; we can.

**Our differentiated wedge:** *intelligence-first, commerce-later.*
1. **Act 1 (now):** the richest open index of Indian building materials — search, facets, specs, **cross-supplier price observations** (we already store prices as observations per source; nobody else in India shows "this SKU: ₹63 at Somany dealer listing, ₹78 at MaterialDepot, MRP ₹95"). Sell as **API + workspace for architects/procurement teams**.
2. **Act 2 (the flip):** **supplier portal** — brands claim, correct, and enrich their own records (free), then pay for placement, leads, and analytics (Material Bank's model). Contribution replaces scraping; legal posture inverts; data quality compounds.
3. **Act 3:** transactions — RFQ/lead-gen first (asset-light), samples/commerce only when demand justifies logistics.

## 3. What we already have (the part everyone skips)

- **142k+ products, 50 suppliers, ~110k priced**, growing hourly, fully autonomous on a VPS (harvest timer → embed daemon → API, self-healing, durable queue with retry/dead-letter).
- The **honesty substrate** most catalogs never build: per-field `{value, confidence, source, basis}`, prices-as-observations with `observed_at` + staleness, quarantine (nothing silently dropped), `missing[]` (nothing silently absent).
- Hybrid semantic+keyword search over one shared text/image embedding space; PDF spec-mining (pdfplumber) proven on Kajaria (real coverage/box data); 3 bespoke harvesters + 3 generic tiers; 206 offline tests.

**The honest gap:** these are *rows*, not *product records*. No canonical schema, no taxonomy, no attribute richness, no dedup across sources, no quality grading, no publish gate. A buyer can't yet *trust or procure* from it. Closing that gap is the entire roadmap.

## 4. Evaluation of the prior six-layer plan (what changes)

Kept: the six layers (canonical schema, enrichment engine, taxonomy, trust engine, media, procurement) — all correct.
**Corrections after research:**
1. **The flywheel was missing.** "Autonomous" isn't a scheduler; it's *metrics that generate work*. Added: the Planner (§6).
2. **Taxonomy must be standards-grounded**, not invented: our tree maps to **OmniClass/Uniclass** codes (what Revit/BIM tools speak — instant B2B credibility) and carries **India facets**: BIS/ISI standard numbers (e.g. IS 15778 for CPVC), **GreenPro** product certification, GRIHA/IGBC credit contribution. ([classification comparison](https://girihx.com/knowledge/data-classification-aeco-uniclass-omniclass/), [India green certs](https://www.studiomatrx.org/guides/green-building-certifications-india))
3. **Dedupe before LLM enrichment** (Phase C before D) — never pay to enrich the same product twice.
4. **The supplier flip is strategy, not a feature** — Act 2 above; scraped data bootstraps, contributed data defends.
5. **API is the product surface**, not the dashboard — architects live in tools; the dashboard is our cockpit.
6. **Human effort is budgeted, not assumed**: the system runs zero-touch; a review queue makes ≤1 human-hour/day maximally leveraged, and golden records need spot-audits, not gatekeeping.

## 5. Target data model — the canonical product card

What every published record looks like (modeled directly on the Material Bank/MaterialDepot cards):

```
CANONICAL PRODUCT
├─ Identity      brand · collection · model/SKU · our canonical_id · variant group
├─ Taxonomy      family → category → subcategory (+ OmniClass/Uniclass code)
├─ Physical      size (mm+ft/in) · thickness · finish · texture · color + color_family
│                pattern · weight · coverage/unit (sqft-per-box/sheet/roll/litre)
├─ Application   rooms[] · surfaces[] · indoor/outdoor · residential/commercial
├─ Performance   PEI · Martindale · Janka · slip · fire class · water absorption
│                — each {value, basis: measured|derived|estimated, standard: IS/ISO#}
├─ Compliance    BIS/ISI code · GreenPro · GREENGUARD/CARB/FSC · GRIHA/IGBC credits
├─ Commercial    dual-unit price (₹/sqft AND ₹/sheet|box) · MRP · discount% · MOQ
│                lead time · pincode serviceability · price history (all observations)
├─ Media         swatch · lifestyle renders · spec-sheet PDF · 3D/GLB
├─ Content       enriched description · key features · best-suited-for (LLM, verified)
└─ TRUST         completeness score (0–100, category-specific requirements)
                 verification tier: unverified → auto-validated → reviewed → golden
                 per-field provenance · freshness · publish_gate: bool
```

## 6. The autonomy flywheel — "keeps running and keeps getting better"

The insight: we already own the right substrate — the **durable `pipeline_jobs` queue** (retry/backoff/dead-letter) + systemd timers. Autonomy = *more stages on the same queue* + a **Planner that turns measured gaps into prioritized jobs*. Nothing depends on a live session or a human.

```
                       ┌──────────────── MEASURE ───────────────┐
                       │  metrics snapshots (nightly + hourly)  │
                       │  completeness/category · freshness ·   │
                       │  unclassified · dup-candidates ·       │
                       │  dead-letters · publish-ready count    │
                       └───────────────┬────────────────────────┘
                                       ▼
   mb-planner (timer) — generates prioritized jobs from the worst, highest-value gaps
        │ enqueues stages on pipeline_jobs, budget-capped (crawl politeness, LLM ₹/day)
        ▼
┌───────────────────────── WORK (daemons/timers, all idempotent+resumable) ─────────────┐
│ harvest (hourly)     — new suppliers + tier-aware refresh (shopify daily/jsonld weekly)│
│ extract              — deterministic attributes from titles/descriptions (regex+vocab)│
│ pdf-mine             — spec-sheet mining (coverage, ratings) — generalized Kajaria path│
│ classify             — taxonomy assignment (rules first, LLM fallback)                │
│ dedupe               — exact → fuzzy(attrs+image cosine) → LLM adjudication queue     │
│ enrich-llm           — descriptions/features/applications (novelty-gated, verified)   │
│ media                — color-family extraction, image embed, (later) renders          │
│ verify               — sampled re-fetch of golden records; cross-checks; price sanity │
│ discover (weekly)    — candidate suppliers → probe → registry (candidate, not active) │
│ repair               — yield-drift & quarantine spikes → re-probe → LLM parser repair │
│ embed (45s)          — index whatever any stage produced                              │
└──────────────────────────────────────┬────────────────────────────────────────────────┘
                                       ▼
                       scorecard trend (is completeness ↑? freshness ↑? publish-ready ↑?)
                       — the Planner reads yesterday's scorecard; the loop closes itself
```

Rules that make it trustworthy:
- **Publish gate:** only records above the completeness+confidence threshold for their category are exposed to B2B consumers. Everything else is visibly "in enrichment." The public catalog is trustworthy *by construction*.
- **Every LLM output has an external verification signal** (schema validation, cross-source agreement, spot re-fetch) and a budget cap; failures quarantine, never publish.
- **Human-in-the-loop is optional, not required:** a review queue surfaces low-confidence merges/enrichments; the system runs without it, just slower to golden tier.
- **Metrics history is stored** (a `metrics` table) — "getting better" is a plotted trend, not a claim.

## 7. Roadmap

| Phase | Name | Builds | Success metric |
|---|---|---|---|
| **A** | Foundation of Trust *(start now)* | `canonical_products` + category-specific completeness scoring · `metrics` snapshots · QA/trust dashboard · publish gate · mb-planner v0 | 100% of products scored; scorecard live; publish-ready count known & trending |
| **B** | Deterministic Enrichment | taxonomy v1 (own tree + OmniClass/Uniclass mapping + BIS/GreenPro facets) · title/description attribute extractors (controlled vocab) · PDF miner generalized (Somany/Nitco/Simpolo/Greenlam…) · dual-unit price normalization (₹/sqft ↔ ₹/sheet) · image color-family | >95% classified; median completeness 70+ in top-5 categories; all surfaces dual-unit priced |
| **C** | One Product, One Truth | entity resolution at scale (exact → fuzzy attr+image → adjudication queue) · golden records keeping all price observations · **cross-supplier price comparison** (our unique feature) | sampled dup rate <1%; price-comparison live on API+UI |
| **D** | Intelligence at Scale | LLM enrichment daemon (Haiku/Gemini novelty-gated; Opus for spec PDFs) — descriptions, key features, best-suited-for, ambiguous classification; verification loop; ₹/day budget | 60%+ of priced catalog publish-ready with rich content; cost/product tracked |
| **E** | Procurement Product | per-category BOM calculators · pincode serviceability · RFQ/sample-request workflow · **API productization** (keys, rate limits, docs, SDK) · architect-grade faceted catalog UI | first external B2B consumer on the API; RFQ flow end-to-end |
| **F** | The Flip & Scale | supplier portal ("claim your catalog") · discovery agent → 500+ suppliers · media generation (lifestyle renders via existing ControlNet path) · visual search · BIM/OmniClass export | first supplier-contributed catalog; first paying brand |

Continuous, every phase: autonomy hardening — each capability ships as a queue stage + timer, resumable, self-healing, metric-tracked.

## 8. North-star metrics (the scorecard)

1. **Publish-ready products** (passing the gate) — the real catalog size, not raw rows
2. **Median completeness** per top category
3. **Price freshness** — % of priced records observed <7 days
4. **Coverage** — suppliers live / categories with >500 publish-ready products
5. **Zero-touch days** — days the system ran and improved with no human action
6. **Enrichment cost per product** (LLM spend ÷ records enriched)
7. (Act 1 commercial) API consumers · queries/day · (Act 2) claimed catalogs

## 9. Risks

| Risk | Mitigation |
|---|---|
| Scraped-data legality for a commercial product | Act 2 flip to contributed data; till then: published-MRP only, robots respected, per-source attribution, takedown process; legal review before reselling dealer pricing (existing rule) |
| LLM enrichment hallucinating specs | enrichment can only *add* to `content` fields or propose `estimated`-basis values; measured fields come only from deterministic extraction; verification signal per slot; publish gate |
| Dedup over-merging look-alikes (tiles!) | brand+size hard gates before image cosine; adjudication queue; merge is reversible (golden record links, never deletes sources) |
| Single VPS = single point of failure | nightly `catalog.db` backup off-box; everything rebuildable from raw + registry; (later) move to litestream/Postgres |
| MaterialDepot competitive response | they're commerce-narrow; we win breadth+trust+API; speed matters — ship Act 1 |

## 10. Decisions needed from the owner

1. **ANTHROPIC/GEMINI API keys** → unlocks Phase D (and LLM slots in dedupe/repair). Everything through Phase C ships without them.
2. **Act 1 pricing posture** — free-while-bootstrapping vs. early API keys for design firms (recommend: free + waitlist, instrument usage).
3. **Name/domain** for the public product (currently raw sslip.io URL).
