# Material Bank India — Full Context Brief

*A self-contained brief for external evaluation. Written to be read cold — no prior context assumed. Ethos of the project is data honesty; this document holds itself to the same bar (real traction, real gaps).*

---

## 1. One line

**The single source of truth for architectural materials in India** — every tile, laminate, sanitaryware, lighting, furniture and surface product as a canonical, classified, enriched, provenance-tracked record with live pricing and a real procurement path (who supplies it, where to buy, the direct product URL).

## 2. The problem

An architect, interior designer, or procurement team in India specifying materials for a project has **no single, trustworthy, structured place** to discover, compare, and source products. Reality today:

- Information is scattered across ~thousands of brand websites, PDFs, dealer WhatsApp groups, and directories (IndiaMART/Justdial) that are noisy, unstructured, and pay-to-rank.
- Specs are inconsistent or missing; prices are opaque (MRP vs dealer quote vs "call us"); the same product appears under many names.
- There is **no trust layer** — no way to know if a spec is measured or guessed, if a price is current, or how to actually buy it.

The result: material selection and procurement is slow, manual, relationship-dependent, and error-prone. This is a multi-billion-dollar friction in a construction market growing rapidly.

## 3. The goal / vision

Build the **canonical material-intelligence platform for India**: breadth (everything), depth (rich structured data), and — the wedge — a **trust contract nobody else exposes**. Every field on every record carries its provenance; only verified-complete records are published; nothing is ever faked.

Reference bars:
- **Material Bank (US)** — the enrichment/facets/trust standard; ~400k+ materials; a large share of revenue comes from **brands paying to be on the platform** (the eventual flip).
- **Material Depot (India)** — the commerce/UX bar; dual-unit pricing (₹/sqft ↔ ₹/box), BOM tools; raised ~$14M, early revenue.

Our differentiator vs both: **intelligence-first and honesty-first** — an autonomous data engine that is correct by construction, not a manually-curated catalog or a directory.

## 4. The prime directive (the moat in one sentence)

> **Every field carries its provenance, and only verified-complete records are published.** The raw web scrape is an ingredient; the product is the canonical record with a trust contract — `{value, confidence, source, basis, observed_at}` on every claim, a completeness score, and a publish gate.

Concretely, this is enforced as hard rules in the codebase:
- **Never fabricate.** Missing/estimated is structural (`missing[]`, `basis='estimated'`), never silent. A derived value can never masquerade as a measured one.
- **Prices are observations, not attributes.** Each price is `{price, basis (listed_mrp / dealer_quote / estimated_band), observed_at, source}`. MRP is labelled MRP, never "cost". Multi-source prices are kept side by side (they power comparison). >90 days ⇒ stale flag.
- **Publish gate.** Only records above their category's completeness+confidence threshold are exposed externally. Everything else is visibly "in enrichment," never quietly served.
- **Autonomy-first.** Every capability ships as a durable-queue stage + a scheduled daemon: idempotent, resumable, self-healing, metric-tracked. Nothing depends on a human or a live session to keep running.

## 5. What it is (the product)

**The catalog IS the product.** It is sold in three acts:

1. **Act I — Intelligence (now → near term):** a faceted catalog UI + an API, sold to architects, designers, and procurement teams. Search/browse by material family, spec, colour, price band; one card per design; every record shows its trust score and procurement path.
2. **Act II — Procurement (mid term):** BOM calculators, pincode serviceability, RFQ/quote flows, dealer routing — turn "I found it" into "I bought it."
3. **Act III — The Flip (long term):** suppliers claim and pay to enrich/own their records (the Material Bank model — brands become the paying customer); plus supplier portal, media generation, visual search, BIM export.

## 6. What is actually built today (honest traction)

This is not a slide deck — it is a **live, autonomous system running 24/7** on a VPS, with a public HTTPS API and dashboard. Current state:

**Data (in a single SQLite catalog, schema v14):**
- **~160,000 products** harvested from **~50 supplier/brand sites** (176 in the registry, probed and tiered).
- **~110,000 priced** (listed MRP observations, append-only history — the system observes prices hourly, accumulating a time series no one else has).
- **~131,000 publish-ready designs** past the trust gate.
- **Canonical taxonomy:** all products classified into 10 material families with standards-grounded codes (OmniClass Table 23 where verified), only ~0.006% unclassified.
- **Variant grouping:** 23,000+ near-duplicate SKUs collapsed into ~5,400 design families (e.g. one mattress model that was 188 rows → one card with a price band), non-destructively — every SKU kept.
- **Trust contract live:** every record scored 0–100 for completeness, tiered (unverified → auto_validated → reviewed → golden), and gated for publish.

**Pipeline (fully autonomous, self-healing):**
`probe → harvest (Shopify/Woo/JSON-LD generic + bespoke parsers) → normalize (units + provenance + missing[]) → price observations → embed (shared text+image vector space) → hybrid keyword+vector retrieval → FastAPI`. A separate "flywheel" loop continuously re-scores, classifies, enriches, and groups. Drift/quarantine detection opens self-repair jobs.

**Deployment:** systemd timers/daemons on a VPS (hourly harvest sweep, 45-min flywheel, embed consumer, API, 6-hourly verified backups to a git branch), HTTPS via Caddy, **CI/CD** (push to git → auto-deploy in minutes). Runs unattended; survives reboots; every long job is resumable and safe to kill.

**Product surface:** a live faceted catalog UI (taxonomy browse, design cards with variant counts + price bands, product detail with trust score + variants + "view source" procurement link) and a documented REST API (`/api/match`, `/api/catalog`, `/api/products`, `/api/product/{id}`, `/api/suppliers`, `/api/taxonomy`, `/api/quality`, `/api/pipeline`).

**Engineering discipline:** ~250 automated tests (offline + opt-in browser e2e), TDD, small commits, no dead code. The whole thing is built and operated by deterministic engineering — LLMs are used only in tightly-scoped, verified slots (and even those are gated behind availability).

**In progress (this week):** the **supplier procurement layer** — for each supplier, deterministically extracting company name, contact, address, GSTIN, and dealer/store-locator network from the supplier's *own* website (legally clean), plus guaranteeing a direct product URL on every record. Schema + extractor + enrichment stage are built and tested; dealer-network harvesters and UI are next.

## 7. How it works (architecture / the "how")

The system is an **autonomous data refinery** with four properties that compound into the moat:

1. **Registry-driven acquisition.** A new supplier is a database row, not a code change. A prober classifies each domain's best harvest strategy (Shopify JSON / WooCommerce API / schema.org JSON-LD / JS-render); bespoke parsers handle important sites (Kajaria PDFs, Orientbell, Steelcase). Politeness (~1 req/2s/domain), robots.txt respect, and content-addressed raw capture are enforced.

2. **Provenance-native normalization.** Every product is assembled into a canonical record where each field records how it was obtained. Surfaces are held to a stricter bar (they need units — size, finish, coverage, price unit — or an explicit missing flag).

3. **The trust flywheel.** A durable job queue (claim/retry/backoff/dead-letter) + scheduled daemons continuously: classify → score completeness → gate publish → enrich (deterministic extraction from titles, descriptions, PDFs, images) → group variants → snapshot metrics. Measured gaps become prioritized queued work. "Getting better" is a stored time series.

4. **One shared intelligence layer.** Text and images live in one embedding space (marqo-ecommerce), powering hybrid search, visual similarity, and (soon) cross-supplier entity resolution.

**Why deterministic-first matters:** the correctness guarantee (never fake a measured value) is only possible because measured fields come from deterministic extraction. LLMs are allowed only to *add* content (descriptions, tags) or *estimated*-basis values, each with an external verification signal and a budget cap — never to assert a measured spec.

## 8. The moat (why this is hard to copy)

- **Data depth + honesty as a contract.** Anyone can scrape a catalog. Almost no one maintains per-field provenance, a completeness score, and a publish gate — because it's expensive discipline, not a feature you bolt on. This is the trust layer buyers actually need and competitors don't have.
- **Autonomy compounding.** The catalog improves every hour without human labor. A competitor with a manually-curated catalog pays linearly for breadth+freshness; we pay ~zero marginal cost (see §11).
- **Breadth × structure.** 160k structured, classified, deduplicated records across every material family is a cold-start asset that takes months of correct engineering to reproduce.
- **The eventual flip.** Once the catalog is the reference buyers use, suppliers pay to be well-represented in it (the Material Bank ~50%-of-revenue model) — a two-sided moat.
- **Legal/ethical posture as an asset.** We source only from suppliers' own domains and public/statutory data, honor robots.txt, exclude personal PII, and attach source+timestamp to every field — defensible and takedown-ready, unlike directory-scrapers who face active litigation (IndiaMART/Justdial precedent).

## 9. Roadmap (phases; where we are)

- **Phase A — Foundation of Trust ✅ (done, live):** completeness scoring, verification tiers, publish gate, metrics snapshots, trust dashboard.
- **Phase B — Deterministic Enrichment ✅ mostly (live):** canonical taxonomy + OmniClass; attribute extractors (size/finish/colour/thickness); description mining; PDF spec-mining (started). *Remaining:* dual-unit price serving, image-derived colour, per-variant attributes.
- **Phase C — One Product, One Truth 🟡 (v1 live):** non-destructive variant grouping done. *Next:* cross-supplier entity resolution → golden records → cross-supplier price comparison (blocked until supplier overlap exists — currently our suppliers are disjoint brand-direct catalogs).
- **Procurement layer 🟡 (in progress this week):** supplier company/contact/GSTIN + dealer network + guaranteed product URL. *This is the current build, prompted by the #1 user need: "who supplies it and how do I buy it."*
- **Phase D — Intelligence at Scale (designed, partially unblocked):** LLM enrichment daemon (rich descriptions, style/use-case tags, ambiguous-tail classification, vision-based colour/material from product images) — novelty-gated, budget-capped, verified. Gemini available; Anthropic key pending.
- **Phase E — Procurement Product:** BOM calculators, serviceability, RFQ, API productization, architect-facing catalog polish.
- **Phase F — The Flip:** supplier portal, discovery to 500+ suppliers, media generation, visual search, BIM export.

## 10. Business model

- **Act I:** subscription/API access to the intelligence catalog (architects, design firms, procurement teams, contractors). Tiered by seats/API volume.
- **Act II:** transaction/lead take on RFQ + procurement routing to dealers.
- **Act III (the big one):** suppliers pay to claim, enrich, and feature their records + get analytics — the Material Bank model where brands fund ~half the revenue once the platform is the buyer's default.

## 11. Unit economics / cost (a key strength)

The data engine is **near-free to run and scales sub-linearly**:
- Harvest + enrichment + supplier sourcing are **deterministic HTTP fetches** of legally-safe pages — bandwidth only. Scaling from 50 → 500 suppliers is ~zero marginal cost.
- Compute is a single modest VPS. No expensive per-record LLM cost in the core path.
- Optional paid layers (licensed GST-registry verification ~₹1–5/lookup; LLM enrichment, budget-capped) are switches, not dependencies.

So the cost curve is dominated by fixed engineering, not variable per-record cost — the opposite of a manual-curation catalog. This is what makes "breadth + freshness + honesty" economically defensible.

## 12. Metrics / north stars

- **Publish-ready records** (breadth × quality passing the gate) — currently ~131k designs.
- **Median completeness** of published records — currently ~100/100 on the gated set.
- **Priced coverage & price freshness** — ~110k priced; hourly re-observation.
- **Procurement completeness** — % of records with a valid direct URL (~99%) and a resolved supplier contact/dealer path (building now).
- **Autonomy health** — % of the catalog improved without human touch per week (the flywheel throughput).

## 13. Risks & honest gaps

- **Pre-revenue, no users yet.** The asset today is the working data engine + the enriched corpus + the architecture, not traction/revenue. Valuation should weigh execution capability and defensible data infrastructure, not current ARR.
- **Supply is currently disjoint** (brand-direct catalogs), so cross-supplier price comparison — a headline feature — has no data until overlapping/dealer supply is added. Known; being addressed via the procurement/dealer layer.
- **Prices are mostly listed MRP** from ~50 suppliers, not yet dealer quotes; true procurement pricing needs dealer-quote ingestion (legally scoped).
- **Scale:** SQLite + single VPS is right for now; Postgres/horizontal scaling is a known, straightforward migration when volume demands.
- **Legal:** business-data aggregation in India is defensible from own-domain + statutory sources with attribution + takedown, but the space is unsettled; we've done a legal read and encoded conservative guardrails (own-domain-only, no personal PII, robots-honored, provenance-stamped).
- **Competition:** Material Depot (funded) and others exist; our bet is that intelligence-first + honesty + autonomy beats manual curation on breadth, freshness, and trust over time.
- **Solo/small team execution risk** — mitigated by the fact that the system is already autonomous and self-healing, reducing the human-in-the-loop burden.

## 14. What a valuation should weigh

1. **A working, autonomous, self-healing data engine** producing ~160k provenance-tracked, classified, deduplicated material records — a cold-start asset in an underserved multi-billion-dollar market.
2. **A defensible architecture** (trust contract + publish gate + autonomy) that is expensive to reproduce and directly addresses the buyer's real need (trustworthy, procurable data).
3. **Near-zero marginal cost economics** — breadth and freshness scale without linear labor/compute.
4. **A clear, staged path** to the proven Material Bank / Material Depot playbook (intelligence → procurement → supplier-funded flip) localized for India.
5. **Comparables:** Material Depot (India) raised ~$14M; Material Bank (US) built a category-defining business on exactly this catalog-as-product + brands-pay model. This is that thesis, built intelligence-first, for the Indian market.

**Stage:** pre-revenue, MVP-with-a-live-data-engine, primarily solo-built. The value is the engine, the corpus, the architecture, and the demonstrated ability to execute the roadmap autonomously.

---

*Everything above is verifiable against a running system and its git history. The catalog, the trust scores, and the provenance are live; the gaps named are real and tracked, not hidden.*
