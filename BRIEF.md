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

The result: material selection and procurement is slow, manual, relationship-dependent, and error-prone.

**Sizing honestly.** India's building-materials market is ~$44B and interior design ~$37B (2025), but commodities (cement, steel, aggregates) dominate those numbers and are already owned by giants (Infra.Market, OfBusiness). Our actual wedge is *finish materials specified by design professionals* — tiles, laminates, sanitaryware, lighting, surfaces — plausibly an $8–15B slice, on which our revenue is a thin intelligence/lead layer, not the transaction value. The real buyer set is not "120k registered architects" but the ~15–30k firms doing spec-driven work. This bottoms-up number is unvalidated and is a top research priority (see §15).

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
- **~110,000 priced** — but these are **listed MRP**, which in the Indian trade is largely fiction (real deals run 25–60% off MRP). The hourly, append-only observation history is genuinely un-backfillable, but its value is capped until we add net/dealer-price bands (a top roadmap item, not a solved feature).
- **~131,000 publish-ready designs** past the trust gate — on a *lenient* bar. A record passes with title + brand + image + category + URL + a price. **Actual enrichment depth is thin** and now tracked honestly as its own metric: description 6%, size 17%, finish 7%, colour 38%, thickness ~0% of the corpus. "Median completeness ~100" is real but must be read as "has the procurement basics," not "richly enriched."
- **Images are proxied, not mirrored** — fetched from source on demand and cached at the edge, nothing stored at scale. The highest-risk redistribution asset, we deliberately don't hold.
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

## 8. The moat — stated honestly (this is the section diligence will attack)

**First, the anti-moat, stated plainly:** the data layer is necessary but almost certainly *not sufficient* as a defensible moat. Our own unit-economics pitch (deterministic scraping, a new supplier is a DB row, ~zero marginal cost) also describes how cheap this is for a *funded competitor* to replicate. Material Bank US's real moat was never the catalog — it was overnight sample logistics + brand relationships that made designers dependent and gave brands ROI worth paying for. We should not claim the corpus alone is a moat; a diligence process will (correctly) flag it as reproducible.

**The moats that are actually durable:**
- **The provenance + price time series** — genuinely un-backfillable. A competitor starting today cannot reconstruct months of per-field, timestamped observations. This compounds with time and is the one data asset that is truly ours. (Caveat: today it's an MRP series; its value multiplies once it's a *net-price* series — see §11.)
- **Supplier + buyer relationships and captured intent** — the real two-sided moat, per the Material Bank lesson: brands pay for *identified, in-project demand* (leads), not for records. **This moat does not exist yet** — it requires demand-side adoption (§13). Honestly, it's the goal, not a current asset.
- **Data-discipline as execution proof** — the trust contract (per-field provenance + completeness + publish gate) is expensive discipline competitors mostly skip. It's a real differentiator *and* the clearest evidence of founder execution quality — which is what actually gets priced at this stage (§14).

**Weaker-than-it-sounds:** "autonomy = ~zero marginal cost" cuts both ways (it's cheap for the incumbent too); "160k records" is a head-start, not a lock. These help; they don't defend.

**Legal/ethical posture as a genuine asset:** own-domain + public/statutory sources only, robots.txt honored per fetch (logged), no personal PII, images proxied not stored, source+timestamp on every field — and a supplier claim/takedown flow (building now) that converts a brand's objection into an Act III onboarding. This is defensible where directory-scrapers (IndiaMART/Justdial) face active litigation — but see §13 for why "defensible" ≠ "safe" in India.

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

Supply-side (what we have today — all measured, not asserted):
- **Publish-ready designs:** ~131k (lenient bar; read with the next line).
- **Enrichment depth:** mean rich-attribute coverage — honestly thin today (~10–15%), tracked over time so "completeness ~100" can't stand in for it.
- **Cross-supplier overlap rate:** **0.00%** today — the explicit gate on price comparison; it stays near zero until multi-brand/dealer supply is ingested. We instrument it so progress is visible.
- **Priced coverage & freshness:** ~110k priced (MRP), hourly re-observation.
- **Procurement completeness:** % with a valid direct URL (~99%) + a resolved supplier/dealer path (building now).

Demand-side (the metrics that actually determine the outcome — **currently zero, and that's the honest headline**):
- **Weekly active specifiers**, searches-with-click-through, spec-lists created, sample/quote requests routed. Not yet instrumented because there are not yet users. Growing records from 160k→400k moves valuation far less than the first 15 habitual weekly users. Building this instrumentation is a near-term priority precisely so the row can start reading a real (small) number.

## 13. Risks & honest gaps

- **Pre-revenue, no users yet.** The asset today is the working data engine + the enriched corpus + the architecture, not traction/revenue. Valuation should weigh execution capability and defensible data infrastructure, not current ARR.
- **Supply is currently disjoint** (brand-direct catalogs), so cross-supplier price comparison — a headline feature — has no data until overlapping/dealer supply is added. Known; being addressed via the procurement/dealer layer.
- **Prices are mostly listed MRP** from ~50 suppliers, not yet dealer quotes; true procurement pricing needs dealer-quote ingestion (legally scoped).
- **Scale:** SQLite + single VPS is right for now; Postgres/horizontal scaling is a known, straightforward migration when volume demands.
- **Demand is the entire unvalidated bet.** Everything built is supply-side. There is *zero* evidence yet that an architect or procurement team will change workflow for this. The valuation-relevant milestone is not record count — it's ~10–20 firms with weekly retention, or one supplier saying they'd pay to claim their records. This is the #1 risk and the #1 thing to go prove.
- **Legal — defensible ≠ safe (India is not a US-style safe harbor).** Scraping is unregulated but the government has signalled that scraping public data may breach IT Act §43 (unauthorised access), and the Delhi HC has granted injunctions recognising a compiled catalog as copyright-protectable (OLX precedent) — so a *brand* could claim its catalog is a protected compilation. The realistic threat is not losing a case; it's a cease-and-desist forcing per-domain takedowns. Mitigations (some built, some building): facts-not-expression (specs/prices are unprotectable facts; descriptions rewritten), images proxied not stored, robots.txt logged per fetch, a one-click supplier claim/takedown flow marketed as a feature, and a written Indian IP/tech-lawyer opinion before fundraising (~₹1–2L). GSTIN is a business ID, but sole-proprietor contacts blur into DPDP personal-data territory — handled by excluding personal names and keeping only company channels.
- **Competition cuts both ways.** Material Depot raised ~$14M but appears to have **pivoted consumer-ward** (curated home-interiors commerce + physical retail), *after* trying a pro-facing library-first model — which is both encouraging (the professional-intelligence seat may be empty) and a warning (a funded team tried discovery-first and retreated toward commerce). Their post-mortem is the single highest-value competitive intel to obtain. Also real: IndiaMART (owns SEO + habit), brand-run dealer tools, ONDC's B2B ambitions, and the true incumbent — **Google + WhatsApp + existing dealer relationships**, a habit that works "well enough."
- **Solo/small-team execution risk** — partly mitigated because the system is already autonomous and self-healing, reducing human-in-the-loop burden; but demand validation and sales cannot be automated and are the binding constraint.

## 14. What a valuation should weigh (honestly)

Pre-revenue, pre-user, solo-founder projects are priced **almost entirely on founder capability and market size, with the tech as supporting evidence** — not on the corpus, which diligence will correctly flag as reproducible. So weigh:

1. **Founder execution quality (the primary asset).** A solo-built, unattended, self-healing pipeline — CI/CD, resumable jobs, ~250 tests, verified backups, live API, and a trust contract most teams skip — is rare at this stage and is the real signal: this founder ships infrastructure that doesn't fall over. The engine and architecture are best read as *proof of that*, not as standalone value.
2. **Market reality:** an $8–15B specified-finishes wedge in India with a professional-intelligence seat that looks genuinely unoccupied (partly because the funded player retreated toward consumer commerce).
3. **The un-backfillable time series** (once it becomes net-price, not MRP) and the *path* to relationship/intent moats — not the record count.
4. **A clear staged path** to the proven Material Bank / Material Depot playbook, localised for India.

**Comparables cut both ways.** Material Depot's ~$14M is evidence the market is real, but it is *not* a valuation comparable — they raised on revenue and commerce traction, and their existence means an intelligence-first entrant races a funded incumbent. A sophisticated investor will anchor on typical pre-seed terms for the geography, not on that raise.

**The cheapest way to multiply the number is not more records — it's a handful of named design firms using it weekly and one supplier who says they'd pay to claim their records.** That is the work; the engine is already done enough to support it.

**Stage:** pre-revenue, MVP-with-a-live-data-engine, primarily solo-built. The value is founder execution capability, the market opening, and a credible staged path — with the engine/corpus/architecture as strong supporting evidence, not the headline asset.

## 15. Adversarial review — the conditions under which this works, and the kill criteria

*This section is deliberately included: the brief has been stress-tested by an independent adversarial review, and its hardest points are folded into the sections above rather than hidden. A founder who has internalised the make-or-break questions is a better bet than one who hasn't.*

**This works only if four things prove true, roughly in order:**
1. **A specific professional segment adopts one wedge workflow weekly.** Best bet: fee-based commercial ID firms and developer/PMC procurement teams — the segment *aligned* with price transparency (vs commission-earning designers who may be threatened by it). Provable in ~90 days with ~15 design partners.
2. **Intent can be captured cheaply enough to generate leads brands recognise as valuable** — because the Material Bank model sells *identified in-project demand*, not records. Open question: can we do this in India *without* capital-intensive sample logistics (via RFQ routing, dealer intros, spec-list analytics)? A cheap probe: an instrumented "request sample/quote" button routing to the brand's dealer.
3. **We get past MRP to net/dealer-price bands within 6–12 months** — MRP-only pricing caps credibility with exactly the procurement users who would pay. Even coarse crowdsourced/dealer-quoted "typical net band per category/brand/city" would 10× the price layer's value and is itself a defensible asset MRP-scraping can't replicate.
4. **The legal posture survives its first brand cease-and-desist** via a claim/takedown flow that converts adversaries into Act III customers.

**Kill criteria (stated up front):** if after ~15 design-partner onboardings weekly retention is near zero regardless of wedge, the corpus is a **licensing/API asset** — sell structured data to brands (competitive intel), fintechs, and ONDC participants — *not* a platform. That's a smaller but real business, and the pivot costs nothing because the engine is identical.

**The information that would most change the picture (research plan, not assertions):** 25–30 structured buyer/dealer/brand interviews; a 2-week designer lookup-diary study to pick the first wedge; a fake-door pricing test; the Material Depot library-first post-mortem; a supply-overlap audit (what % of SKUs could ever appear in a 2nd source); and a formal Indian IP/DPDP counsel opinion. None of these are code — they are the founder work that de-risks the bet, and they matter more right now than any further engineering.

---

*Everything above is verifiable against a running system and its git history. The catalog, the trust scores, the provenance, and the depth/overlap metrics are live; the gaps named are real, measured, and tracked — not hidden.*
