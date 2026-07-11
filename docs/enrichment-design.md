# LLM Enrichment — Issues, Gaps, and the "Get the Most per Product" Design Brief

*Written to hand off for research. Grounded in the live ledger (~1,740 calls, 1,441 products enriched) as of this pass. The goal: before spending ~$220 running the full 160k catalog, design the schema so each paid call extracts maximum honest value.*

---

## 0. THE HEADLINE FINDING (fix before any full run)

**We are billing for "vision" but never sending the image.** The realtime client sends **text only** — the prompt literally says *"IMAGE: img1 attached"* but no image bytes go to Gemini. So today:
- `vision.colour_primary / material_look / finish` are the model **guessing from the title/text**, not seeing anything. Unreliable.
- Description sentences that cite `img1` ("warm brown veining across the surface") are **inferred from the name** (e.g. "Emperador Marble"), not observed. This is the weakest link in the honesty contract, and the source of the `img-only cite` verifier failures.

**Implication:** the single biggest "get the most" lever is to actually **send the product image** (Gemini is multimodal). That (a) makes the vision fields real and honest, and (b) unlocks the richest enrichment — colour, pattern, material-look, style — which text can't give. ~99% of our products have an image URL. This is the #1 decision.

---

## 1. The issues we're actually facing (from the ledger)

| Class | Count | What it is | Status |
|---|---|---|---|
| `use_case_tags not grounded` | **191** | Model emits functional tags (residential/commercial…) my grounding rule rejects even when correct (e.g. "commercial" for an anti-static tile). Tagging is **brittle**. | Being soft-dropped (no retry); redesign proposed below |
| JSON parse (`Extra data` / delimiter) | **79** | Model returns near-JSON (fences / trailing prose); client threw. Mislabelled as `api_error`. | **FIXED** (`extract_json` recovers without retry) |
| `style_tags not grounded` | 8 | same brittleness, style | soft-drop |
| `img-only cite on non-visual claim` | 4 | model cites the (absent) image for a functional claim | root cause = §0 (no image) |
| genuine `503` | 5 | transient overload | FIXED (backoff-retry) |
| final `enrich_failed` | 23 (1.6%) | genuine description issues after retry | acceptable; re-runnable |

**Two structural problems:** (1) no image is sent (§0), (2) tag grounding is brittle (§3).

The **2× cost retry** (attempt-0 rejected on a tag, attempt-1 passes) is already fixed — tags are now *sanitized* (dropped), not a reason to re-bill a good description. Attempt-0 share went 50% → 88%.

---

## 2. What each call extracts today (the current schema)

```
description[]   : 2–4 cited sentences (generated)
style_tags[]    : controlled vocab (14 styles)
use_case_tags[] : controlled vocab (16 use-cases)
vision          : colour_primary / material_look / finish  (currently GUESSED — see §0)
```
Stored in `products.llm_content`, basis `generated:llm:*`, **bonus only** (never in the publish gate — measured fields stay deterministic). Good honesty posture; thin on value.

---

## 3. The opportunity — "get the most per product"

Since we pay the fixed prompt cost per call regardless, adding output fields is incremental value. With the **image attached**, one call could honestly produce:

**Content fields (basis `generated`):**
1. **Description** — richer, 3–4 sentences *(have)*
2. **Feature bullets** — 3–5 key selling points
3. **Search keywords / synonyms** — alt terms architects search ("carrara", "statuario", "large-format") → directly boosts our retrieval
4. **Style / aesthetic tags** — from the image
5. **Room / application fit** — "bathroom floor, kitchen backsplash, commercial lobby"
6. **Design descriptors** — warm / bold / understated (for facets)
7. **Meta / SEO title + description** — for the eventual public catalog

**Visual attributes from the real image (basis `estimated`, image-grounded):**
8. **Dominant + accent colour** (we also have a deterministic pixel version to cross-check → agreement = high confidence)
9. **Pattern** — marble / wood / concrete / terrazzo / geometric / floral / solid
10. **Surface look** — glossy / matte / textured / structured
11. **Material appearance** — the visual read

**NOT allowed from the LLM (honesty hard rule):** measured specs — size, thickness, PEI, water absorption, BIS/ISI numbers, coverage. These come from deterministic extraction only; the LLM may never assert them.

**Derivable deterministically (don't spend an LLM call on these):** price tier/percentile, colour (pixel k-means we already built), size/finish (extractors), dual-unit price, dealer geography.

---

## 4. Cost mechanics (this constrains the schema)

- Per call today ≈ **₹0.10** (~1,500 input tokens of mostly-fixed prompt + ~350 output). **Output ($2.50/1M) dominates.**
- **A richer schema = more output tokens = higher per-call cost.** Doubling output (features + keywords + richer vision) → ~₹0.15–0.20/call → 160k ≈ **₹28k–32k (~$340–385)** — **exceeds the $250 credit.**
- **The image adds input tokens** (~260–1,000 depending on resolution) → modest cost bump, but it's cheap input ($0.30) and unlocks the highest-value fields.

**Levers to fit a richer schema in budget (research these):**
- **Prompt caching** — the fixed system prompt (~1.5k tokens) cached at 75% off → big input saving on 160k calls.
- **Model split** — `gemini-flash-lite` ($0.10/$0.40, ~6× cheaper output) for the mechanical parts (tags/keywords), `flash` for prose + vision. Or flash-lite for everything if quality holds.
- **Trim the prompt** — the vocab lists (~200 tokens) can move to cached context.
- **Prioritise fields** — pick the 3–4 highest-value additions, not all 11.
- **Cap image resolution** — 512px is plenty for colour/pattern and keeps input tokens low.

---

## 5. Tagging — the robust redesign (proposal)

The brittle part is grounding tags to field-ids. Proposed **evidence-based tagging**:
- Each vocab tag has **evidence keywords** (e.g. `commercial` ← anti-static, conductive, office, hospitality, heavy-duty; `bathroom` ← bath, sanitary, shower, wc).
- A tag is **kept if there's textual evidence** for it in the record (title/finish/description/category) **or**, for style tags, the image supports it. "commercial" survives for an anti-static tile; "residential" on a plain tile is dropped.
- **Deterministically derive** use-case tags from the same evidence → grounded *by construction*, no dependence on the LLM getting citations right, and **no retries from tags ever**.
- Result: reliable, grounded tags; the LLM focuses on style/visual nuance it's actually good at.

*(This is deterministic, so it can re-tag the already-enriched 1,441 for free — no LLM calls.)*

---

## 6. Open design questions (for your research)

1. **Send the image?** (Strongly recommended — makes vision real + honest, unlocks the top fields. Yes/no?)
2. **Which additional fields** matter most for architects — feature bullets, search keywords, room-fit, pattern, style? Pick the priority set.
3. **Budget vs richness:** a full rich schema likely exceeds $250. Which lever — prompt caching, flash-lite, prioritise fields, or raise budget?
4. **Tagging:** evidence-based (§5) as proposed, or a taxonomy you want to design?
5. **Vision colour:** trust the LLM (image) or the deterministic pixel version we already have, or cross-check both (agree → high confidence, disagree → flag)?
6. **One rich call vs two cheap calls** (e.g. flash-lite for tags/keywords + flash for description/vision)?

---

## 7. Recommended path (my opinion, if useful)

1. **Attach the image** (fixes honesty + unlocks value). 
2. Expand the schema modestly: **description + feature bullets + search keywords + evidence-based tags + image-grounded vision (colour/pattern/look)** — the high-value set, not all 11.
3. **Enable prompt caching + cap image at 512px** to keep it inside $250.
4. **Evidence-based tagging** (deterministic derivation + keyword verification).
5. Run the 1k eval on the new schema → confirm quality + real per-product cost → then the full pass **once**.

Everything above is live-verifiable; the mass drain is **paused** so we don't spend on the thin schema before this is decided.
