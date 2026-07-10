# Pricing / Willingness-to-Pay Fake-Door Test

Status: proposal, not yet run. Owner decision needed before launch (see `VISION.md` §7 "Act-1 pricing posture — owed").
Companion to `VISION.md` (business model) and `memory.md` (system state). Nothing here requires billing code — the whole point is to answer "should we build billing" before we build it.

**Premise.** We are pre-revenue with a real, growing dataset (159k+ products, 50 suppliers, live pricing) but zero evidence anyone will pay. Indian design studios are price-sensitive per-seat SaaS buyers; the plausible Act-1 money is developers/API integrators, PMC (project management consultancy) firms, large ID studios doing procurement at scale, and materials brands wanting competitive intelligence on their own category. This test is designed to find out **which of those segments has real budget**, cheaply, before writing a Stripe integration.

---

## 1. Fake-door mechanics

Three pricing-page variants, each a single static page (no checkout, no real plan enforcement — clicking "Subscribe" always routes to a capture form, never a payment form). Each is a distinct URL so traffic and signal don't mix:

- `/pricing/studio` — per-seat, self-serve framing
- `/pricing/api` — usage-based API / enterprise framing
- `/pricing/intelligence` — materials-brand competitive-intelligence framing

All three share one visual system (reuse `static/dashboard.html`'s existing editorial design — Fraunces/Inter, dusk hero, hairlines) so the only variable being tested is **who the page is for and what it's selling**, not visual quality. Each shows real product counts and one real example screenshot/GIF of the catalog (`/api/catalog`, `/api/product/{id}`) — proof this isn't vapourware, since Mom-Test discipline says people commit harder to something they can already see working.

### 1.1 Variant A — Studio plan (per-seat)

Framing: "Material search + specs + price observations for design teams." Named seats, monthly.

| Tier | Price (₹/seat/mo) | What's shown |
|---|---|---|
| Free | ₹0 | Search 500 curated SKUs, no export, no price history |
| Studio | ₹2,999/seat/mo | Full catalog search, BOM calculator, price-comparison view, 3 exports/mo |
| Studio Team (5+ seats) | ₹2,399/seat/mo | Above + shared boards, CSV export, priority support |

CTA: **"Start 14-day trial"** → capture form (see 1.4). We already suspect this segment under-pays (per CLAUDE.md market read) — this variant exists specifically to **measure that suspicion** rather than assume it, since assuming without testing is itself a way of fooling ourselves.

### 1.2 Variant B — API / Enterprise plan

Framing: "The pricing + specs API for anyone building on Indian building-materials data." Aimed at proptech, BIM tool vendors, PMC firms building internal procurement tools, ID-studio tooling teams.

| Tier | Price | What's shown |
|---|---|---|
| Developer | ₹0, 100 calls/day | `/api/match`, `/api/products`, rate-limited, watermarked responses |
| Growth | ₹40,000/mo | 50k calls/mo, `/api/product`, price-observation history, SLA email support |
| Enterprise | "Talk to us" | Unlimited calls, bespoke feeds (BOM-ready exports), dedicated Slack channel, contract |

CTA on Developer/Growth: **"Request API key"**. CTA on Enterprise: **"Book a call"** (this is the true soft-commit tier — see 2.3). Page shows a real curl example against `/api/match` and a sample JSON payload with provenance fields (`{value, confidence, source, basis, observed_at}`) — the trust contract *is* the sales pitch for this segment, so it needs to be visible, not just claimed.

### 1.3 Variant C — Materials-brand intelligence plan

Framing: "See how your SKUs are priced and positioned across every Indian dealer channel — before your competitors do." Aimed at brand/category managers at tile, laminate, sanitaryware manufacturers (Kajaria, Somany, Greenlam, Century, Cera, etc. — i.e. the *supply side*, which is also the Act-2 flip audience, so this variant doubles as an early read on that thesis).

| Tier | Price | What's shown |
|---|---|---|
| Category Snapshot | ₹15,000 one-time | A PDF-style sample report: your brand's SKUs vs. 2 named competitors, price bands, dealer-listing spread, staleness of competitor pricing |
| Monthly Intelligence | ₹60,000/mo | Live dashboard, alerting on competitor price changes, SKU-gap report (what competitors list that you don't) |
| Enterprise Data Feed | "Talk to us" | Raw feed access, custom category coverage |

CTA: **"Request sample report"** (Snapshot/Monthly) or **"Book a call"** (Enterprise). This variant is the cheapest to fulfil manually if signal is strong — a report can be hand-built once from data we already have — so it doubles as inventory for the sales calls in Part 3.

### 1.4 The capture form (same component on all three variants)

Every CTA — regardless of tier — opens the same short form, never a payment page:

```
Email*            [_______________]
Company / studio* [_______________]
Role*              ⌄ (Owner/Partner · Procurement lead · Architect/Designer ·
                       Developer/Engineer · Brand/Marketing manager · Other)
Company size*       ⌄ (1-5 · 6-25 · 26-100 · 100+)
Which plan?*        (pre-filled from the tier clicked, editable)
[optional] What are you hoping this solves?  [_______________]

     [ Get access ]              [ or, book a 15-min call instead → ]
```

The **company-size + role fields are the soft-commit filter**, not decoration — a real buyer answers them without friction; a curiosity-clicker abandons the form. The "book a call instead" link is the strongest soft-commit and routes straight to a Calendly-style scheduler; anyone who takes it becomes a Part-3 call automatically.

### 1.5 Instrumentation — events to fire

The platform's event-logging endpoint (add as `POST /api/event`, alongside the existing `/api/*` routes in `material_bank/serve.py`, backed by a `pricing_events` SQLite table analogous to `pipeline_jobs`/`harvest_history` — append-only, no PII beyond what the form already collects) should log this event set. Fire client-side on the pricing pages, with a `session_id` (anonymous, cookie/localStorage) to stitch a visitor's funnel:

```json
{
  "event": "pricing_page_view",
  "variant": "studio | api | intelligence",
  "session_id": "uuid",
  "ts": "2026-07-10T12:00:00Z",
  "utm_source": "linkedin | email | community | direct",
  "utm_campaign": "..."
}
```

Full event taxonomy (all carry `variant` + `session_id` + `ts` + UTM fields):

| Event | Fires when | Extra fields |
|---|---|---|
| `pricing_page_view` | page loads | referrer |
| `plan_scroll_view` | a specific tier card enters viewport | `tier` |
| `cta_click` | any "Start trial / Request API key / Request sample report / Book a call" button clicked | `tier`, `cta_label` |
| `form_open` | capture form modal opens | `tier` |
| `form_submit` | form submitted | `email`, `company`, `role`, `company_size`, `tier`, `free_text` |
| `form_abandon` | modal closed without submit, or blurred >20s idle | `tier`, `fields_filled` |
| `call_booked` | "book a call instead" scheduler confirms a slot | `tier`, `scheduled_at` |
| `email_confirmed` | double opt-in link clicked (also lets us drop obviously fake emails) | — |
| `sample_report_sent` | (Variant C only) manual ops action logged when we actually send the hand-built report | `company` |

This gives four funnel stages to compute per variant: **view → click → form_submit → call_booked/email_confirmed**, which is exactly the curiosity-vs-intent gradient used in §2.3.

---

## 2. Experiment design

### 2.1 Segments × channels (map each variant to where its buyer actually is)

| Variant | Target segment | Channels | Why this channel |
|---|---|---|---|
| A — Studio | Architecture/ID studio owners, procurement leads at studios 10-100 seats | LinkedIn (IIA — Indian Institute of Architects — groups, CoA alumni networks), Instagram DMs to studios with active project pipelines, WhatsApp/Slack architect communities (e.g. "Architecture Community India"), direct email to studio principals scraped from public project credits | This audience lives on LinkedIn/Instagram professionally, not on Google search — cold search traffic would be noisy |
| B — API/Enterprise | CTOs/eng leads at proptech (Livspace/HomeLane-adjacent tooling teams, Zolo/NoBroker-style platforms), PMC firms (L&T Construction, JLL, CBRE project teams), BIM/Revit plugin vendors | Direct outbound (LinkedIn Sales Navigator + cold email) to named individuals, Hacker News "Who's Hiring"-adjacent dev communities, a Show-HN-style post if the API has a public sandbox | Enterprise/dev buyers respond to direct 1:1 outreach and technical proof, not ads |
| C — Brand intelligence | Category/brand managers, market-research and competitive-intelligence teams at materials manufacturers (tile/laminate/sanitaryware majors and mid-tier challengers) | Direct outbound to named marketing/BD contacts (LinkedIn + email), trade-show contact lists (Acetech India, IndiaBuild), industry association mailers if accessible | This buyer doesn't self-serve discover a pricing page; it has to be pushed to them with the sample report as the hook |

Do **not** run paid search or generic display ads for any variant — the population that clicks a Google ad for "material pricing India" is not the population with a budget line, and it will pollute every downstream number. Every channel above is chosen so that a click already implies some professional relevance.

### 2.2 Sample sizes

This is an enterprise/considered-purchase motion, not e-commerce, so the ceiling on sample size is *targeted reach*, not raw traffic. Budget 3-4 weeks per variant, run overlapping but on separate UTM campaigns:

| Variant | Targeted reach (people actually contacted/exposed) | Minimum for a real read |
|---|---|---|
| A — Studio | 800-1,200 (LinkedIn/Instagram outbound + community posts) | ≥600, else conversion % is noise |
| B — API/Enterprise | 150-250 (this is 1:1 outbound, small-N by nature) | ≥100 named contacts |
| C — Brand intelligence | 60-100 (very small, named-account outbound) | ≥40 named contacts |

B and C are intentionally small-N — enterprise buyers don't need thousands of impressions, they need the right 100 people. Don't inflate B/C reach with low-quality contacts to hit an arbitrary number; a wrong-fit contact who clicks is a false positive, not signal.

### 2.3 Success thresholds — curiosity vs. real intent

The single biggest way to fool yourself here is treating `form_submit` (an email address) as validated demand. It isn't — emails are nearly free to give. Treat the funnel as a ladder and only the top two rungs count as real signal:

```
pricing_page_view        (rung 0 — reach)
  → cta_click             (rung 1 — curiosity: "sounds interesting")
    → form_submit         (rung 2 — mild intent: gave an email + role, still cheap)
      → company_size + free-text filled meaningfully   (rung 3 — soft-commit: gave
                                                          context a curiosity-clicker won't)
        → call_booked                                   (rung 4 — real intent: gave up
                                                          15 real minutes)
```

**Per-variant thresholds to justify moving to the next stage (build billing, or at minimum keep investing sales motion in that segment):**

| Metric | Weak (kill/rework messaging) | Moderate (iterate, don't build billing yet) | Strong (proceed toward billing) |
|---|---|---|---|
| `cta_click` / `pricing_page_view` | <5% | 5-10% | >10% |
| `form_submit` / `cta_click` | <25% | 25-40% | >40% |
| Rung-3 (company_size+free-text filled) / `form_submit` | <30% | 30-50% | >50% |
| `call_booked` / targeted reach (variant-level, not just page traffic) | <2% | 2-5% | >5% |
| Of booked calls: % that state a **specific current spend** on a comparable tool/process (see Part 3) | <15% | 15-35% | >35% |
| Of booked calls: % that give an **unprompted number** they'd pay, or ask for a contract/PO | <10% (<2 of 20) | 10-25% (2-5 of 20) | >25% (5+ of 20) |

**Decision rule:** billing gets built for a segment only if that segment hits *Strong* on `call_booked`/reach **and** *Moderate-or-better* on the two call-outcome rows. A segment that's Strong on clicks but weak on calls is curiosity, not revenue — this is the exact trap to avoid (Studio variant is the likeliest place this happens, given the stated price sensitivity).

### 2.4 Avoiding fooling yourself — checklist

- **Never let a raw email count stand in for intent.** Always report rung-3/rung-4 numbers alongside rung-2, never rung-2 alone, in any update to the team.
- **Segment every number by channel and by role**, not just by variant. A Studio-plan click from a "Procurement lead" at a 100+ seat firm is a different data point than one from a "Architect/Designer" role at a 1-5 seat studio — collapsing them hides which sub-segment is real.
- **Price-anchor honestly.** Showing a real ₹ number (not "contact us" everywhere) is itself part of the test — "contact us" pages get curiosity clicks with no signal about affordability; a visible price that still gets a `call_booked` is a much stronger data point.
- **Use a decoy-free control read.** For variant A only, also track how many `cta_click`s come from people who scroll past the price without hesitating (time-on-price-card <2s) vs. those who pause (>5s) — a pause-then-click is a better intent signal than an instant click, and the reverse (instant bounce after seeing price) is itself a datum about price sensitivity.
- **Don't seed traffic from people who already know us.** Friends-of-founder clicks and internal team testing must be excluded via a `?internal=1` UTM tag that's filtered out of all reporting — otherwise the numerator is inflated by people who were never going to pay anyway.
- **Cap the free-text field's leading power.** Don't pre-fill or suggest what to type in "what are you hoping this solves" — a specific unprompted answer ("I need dealer price comparison for tile RFQs") is worth far more than a vague one ("looks useful"), and pre-filling collapses that distinction.
- **Run all three variants concurrently, not sequentially.** Sequential runs confound segment signal with time-of-market (a bad week for outbound looks like a bad segment). Concurrent runs let you compare cta_click and call_booked rates fairly across A/B/C.

---

## 3. The 20-conversation sales-call script

Every `call_booked` event and every Enterprise/Brand-intelligence "Book a call" CTA feeds this call queue. Target: 20 completed calls across all three segments combined, roughly proportional to how many book (expect skew toward B/C since their outbound is higher-intent by construction). 15 minutes each, recorded with consent, notes logged against the same `session_id` so call outcomes join back to the funnel data.

**Mom Test discipline, non-negotiable:**
- Talk about *their* life, not our product. Don't pitch until minute 12+, if at all.
- Ask about the **past and present** ("what did you do last time you needed this"), never the **hypothetical future** ("would you use a tool that...").
- Every claim they make about willingness to pay must be backed by a **specific number, name, or action** — vague enthusiasm ("this is great, we'd definitely use it") is noise; a number, a tool name, or a next step is signal.
- We talk <30% of the time. If a call feels like a demo, it went wrong.

### 3.1 Opening (all segments, ~1 min)

> "Thanks for booking time — before I say anything about what we're building, I want to understand how you currently handle [material sourcing / pricing lookups / competitive tracking] today, so I don't waste your time pitching something you don't need. Mind if I ask a few questions first?"

### 3.2 Core question bank (pick 6-8 per call, segment-appropriate)

**History / current behavior (never hypothetical):**
1. "Walk me through the last time you needed to price out [tiles/laminate/sanitaryware] for a project — what did you actually do, step by step?"
2. "What tool or spreadsheet are you using for that today?" *(if none: "so how do you keep track of it?")*
3. "Who else touches that process — do you do it yourself, or does someone on your team?"
4. "When's the last time a wrong or outdated price caused a real problem — a re-quote, a client complaint, a delay?"

**Budget & authority (the Mom-Test-hardest part — ask for numbers, not opinions):**
5. "What are you currently paying for [comparable tool — BIM library, sample-ordering platform, market-research subscription, dealer-data service]? Even roughly."
6. "Who signs off on a new software line item at your firm — is that you, or does it go up a level?"
7. "Is there a budget already allocated for this kind of thing this year, or would it be a new ask?"
8. "What's the last piece of software your team started paying for — what made that one clear the bar?"

**Specific job-to-be-done (get concrete, not "everything would help"):**
9. "If you could only fix one part of [sourcing/pricing/competitive tracking], which part costs you the most time or money right now?"
10. "Show me — do you have a recent RFQ/spec sheet/report you were working from? What was missing or wrong in it?"
11. *(Variant C only)* "How do you currently find out what a competitor is charging for a comparable SKU? How often, and how confident are you in what you find?"
12. *(Variant B only)* "What data source do you currently integrate for [pricing/specs/catalog] data, and what does that cost/how reliable is it?"

**Close — the soft-commit ask, not a hypothetical yes:**
13. "Given what you've told me, here's roughly what this would cost for your setup: [state the real number from the pricing page]. Is that in the range of what you'd expect to pay, over or under?"
14. "If I sent you a one-page pilot proposal at that number this week, is there someone besides you who'd need to see it before you could say yes?"
15. "Would it make sense to start with [the smallest paid tier / the sample report] so you can see it against real data before committing further?"

### 3.3 What counts as a strong signal on the call

- They name a **specific current spend** (even a rough number) on something comparable — strongest budget signal.
- They **volunteer** a number for what they'd pay before you state one, or push back on your number with a counter-number ("we'd do it at ₹X, not ₹Y") — this is negotiating, which only happens when someone is real.
- They ask **"who do I send a PO to"** or **"can you send a contract/proposal"** — treat this as a rung-4+ event and log it even though it's not a captured `POST /api/event` field (add it manually to the call notes as `outcome=po_requested`).
- They bring **someone else into the conversation unprompted** ("let me loop in our procurement head") — budget authority is real and they're routing to it.

### 3.4 What is a false positive (do not count these as wins)

- "This is really cool, we'd definitely use this" with no number and no next step.
- Enthusiasm about the *data* ("wow you have that much coverage") without any statement about paying for it.
- "Send me more info" with no scheduled follow-up and no number discussed.
- Anyone who spent the whole call asking *you* questions about the technology rather than answering yours about their process — that's curiosity about the build, not the buy.

---

## 4. What number means what — decision table

| Signal observed | Interpretation | Action |
|---|---|---|
| High `cta_click`, low `form_submit` across all variants | Pricing page is interesting but form friction or price sticker-shock is killing intent | Shorten form, or A/B the price number itself before concluding "no market" |
| Studio (A) clicks high, calls near-zero, no one states a current spend | Confirms the price-sensitivity thesis — studios browse, don't buy at this price point | Do not build per-seat billing yet; keep Studio as free/lead-gen into A/B (per Act-2 flip logic in `VISION.md`) |
| API/Enterprise (B) small-N but `call_booked` >5% of the ~150-250 reached, and ≥2 calls end in "send a proposal/PO" | Small but real enterprise demand exists | Build a manual/invoiced billing path first (no self-serve Stripe needed) — take the first 1-2 deals by hand, automate after |
| Brand intelligence (C) sample-report requests convert to paid Monthly tier interest in calls, with named current competitive-intel spend | Validates the Act-2 "brands pay" thesis earlier than planned | Prioritize: hand-build 2-3 more sample reports as sales collateral before any code; this could pull Act-2 revenue forward |
| Across all variants: rung-3 (company_size+free-text) rate <30% even where rung-2 is decent | Volume looks fine but it's mostly curiosity clicks, not qualified leads | Fix targeting/channel before concluding anything about price or product |
| 0/20 calls produce a specific number, current spend, or PO request | No segment tested has demonstrated real WTP yet | Do not build billing. Re-segment (e.g. narrower vertical — only PMC firms, or only tile/sanitaryware brands) and re-run before declaring "no market" |
| 5+/20 calls produce an unprompted number or PO request, concentrated in one variant | Real, segment-specific WTP found | Build billing scoped to *that* variant only (e.g. invoiced Enterprise API contracts) — resist building all three tiers just because the test covered all three |
| Strong signal on B or C, weak on A | Confirms CLAUDE.md's working hypothesis (API/enterprise > per-seat studio SaaS in Act 1) | Reallocate roadmap: prioritize `/api/*` productization (Phase E) over any studio-facing paywall |

**Bottom line rule:** billing gets built only for the specific variant(s) that clear the *Strong* row in §2.3 AND produce at least one real PO/proposal request in §3. Anything short of that is more pricing-page iteration or more calls — never a reason to write Stripe integration code.
