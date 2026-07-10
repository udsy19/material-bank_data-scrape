# 2-Week Material-Lookup Diary Study — Wedge Selection Protocol

Status: ready to run. Owner: founder. Window: pick any Monday–Sunday×2; recruit the week before.

## 0. The decision this informs

We are pre-user and must build ONE workflow first:

- **(a) Spec-verify** — "find/verify the spec sheet, confirm this SKU and its sizes/variants actually exist" (our provenance + `products`/`price_observation` schema is strong here — we already carry `{value, confidence, source, basis, observed_at}`).
- **(b) BOQ/spec-list assembly** — turn a room/project list into a bill-of-quantities with live product URLs and prices attached (leans on `bom` module + dual-unit pricing).
- **(c) Sourcing/dealer-proximity** — "who supplies this near my project site, and can I get it there" (leans on a dealer/where-to-buy layer we don't have yet).

**Decision rule (fixed before data collection, not after):** score each wedge as

```
wedge_score = event_share × failure_rate × normalized_time_cost
```

where for each wedge:
- `event_share` = % of all logged lookup events that belong to that wedge (frequency — how often does this pain actually occur in a normal two weeks of work)
- `failure_rate` = % of that wedge's events where the participant marked "not found" / used a workaround
- `normalized_time_cost` = mean minutes spent on that wedge's events, rescaled 0–1 against the slowest wedge

Build the workflow for the **highest-scoring wedge**, provided it clears two floors: `event_share ≥ 15%` (else it's not frequent enough to be a wedge) and `failure_rate ≥ 30%` (else the current tools already solve it well enough that we add no value). If two wedges land within 10% of each other on `wedge_score`, break the tie using **frustration score** (mean 1–5 self-rating) — build the one that hurts more, since hurt drives willingness-to-pay/switch. Full mechanics in §6.

This rule is written down now, before a single diary entry exists, specifically so post-hoc narrative can't bend the result toward whichever wedge is more fun to build.

## 1. Participants

**Target N = 10, stratified across the segments that actually do material lookup:**

| Segment | N | Why included |
|---|---|---|
| Architecture firm — principal/design lead (studios, 3–15 people) | 3 | Spec-verify + BOQ decisions concentrate here |
| Interior designer — independent or boutique studio | 3 | Highest volume of day-to-day SKU/price lookups; residential + commercial mix |
| Site/project procurement or execution engineer (works at a contractor or a design firm's site team) | 2 | This is the "who supplies near my site" persona — needed to fairly test wedge (c) |
| Junior architect/designer (0–3 yr, does the actual spec-sheet grunt work) | 2 | Does the actual lookup even when a principal decides; captures workaround behavior honestly |

Recruit through: personal network, 2–3 architecture/design WhatsApp groups or Instagram DMs to studios already following material/design content, and IIA (Indian Institute of Architects) or NASA (student body alumni) local chapter contacts if available. Screen for: actively working on ≥1 live project during the study window, uses a phone for work messaging (near-universal), comfortable sending WhatsApp voice notes/photos.

**Incentive:** ₹3,000 UPI on completion of ≥80% of expected entries (see cadence in §4) + a ₹500 top-up for finishing the full 14 days with the mid-study call attended. Paid via UPI within 48h of study close — fast payment matters more than amount for repeat-recruitability. Total budget: 10 × ₹3,500 = ₹35,000, plus a WhatsApp Business API cost of roughly ₹0 (see §3, we recommend not needing a paid API tier at this N).

Do not incentivize per-entry (pay-per-log) — it invites fabricated entries to hit quota. Pay for completion of the study, not entry count.

## 2. The logging instrument (per-event template)

Every time a participant looks up, tries to verify, or tries to source a material during their normal work, they log one event. Target: **under 60 seconds on a phone**, so the template is mostly tap/select with one free-text field.

**Fields (in the order they're asked, since order sets the perceived effort):**

1. **Trigger / JTBD** (single-select, tap): `Client asked` / `Confirming for BOQ` / `Site queried availability` / `Comparing options` / `Double-checking a spec before order` / `Other (say in voice note)`
2. **What were you looking for** (free text, short — e.g. "800x800 vitrified tile, matte, ₹ under 90/sqft")
3. **Where did you look** (multi-select, tap): `Supplier website` / `WhatsApp to dealer/sales rep` / `Google search` / `Called dealer` / `Site visit / showroom` / `Asked a colleague` / `MaterialBank/other aggregator` / `PDF catalog on file` / `Other`
4. **What wedge does this match** — *do not ask the participant this; the founder tags it during weekly review* (see §6) — keep the instrument free of our own jargon.
5. **Did you find what you needed** (single-select): `Yes, fully` / `Partially` / `No`
6. **If not fully — what did you do instead (workaround)** (free text, optional, skippable — e.g. "picked a different SKU that was in stock" / "gave client an estimate without confirming")
7. **Time spent on this lookup** (single-select buckets, not free text — faster to tap): `<5 min` / `5–15 min` / `15–30 min` / `30–60 min` / `>1 hr` / `Still unresolved`
8. **Frustration** (1–5 tap-scale, 1 = no big deal, 5 = this wrecked my day)
9. *(optional)* Photo or forwarded link/PDF of what they were looking at — huge for us to later classify the wedge correctly and see what "found" actually looked like.

That's 6 required taps + 1 short text field + 1 optional text + 1 optional photo. Realistic fill time: 30–45 seconds once habituated (day 1–2 will run slower).

### Filled example — 3 diary entries (verbatim style participants will produce)

**Entry 1 — Interior designer, Day 3**
- Trigger: Confirming for BOQ
- Looking for: "600x1200 GVT tile matte finish for a lobby floor, need exact size + box coverage sqft"
- Where: Supplier website, WhatsApp to dealer
- Found: Partially
- Workaround: "Website showed the SKU but no coverage/sqft-per-box, WhatsApped the dealer, he took 40 min to reply with a photo of the box label"
- Time spent: 30–60 min
- Frustration: 4
- *(tag applied on review: wedge (a) spec-verify — coverage/spec data missing at source of truth)*

**Entry 2 — Architecture principal, Day 6**
- Trigger: Client asked
- Looking for: "Laminate options under ₹1500/sheet in a walnut finish available in Pune"
- Where: Google search, called dealer, asked a colleague
- Found: No
- Workaround: "Ended up quoting from memory of what we used on the last project, will confirm price before finalizing BOQ line"
- Time spent: >1 hr
- Frustration: 5
- *(tag applied on review: mixed — starts as (a)/(b) but fails specifically because there's no "supplier near Pune with this in stock" answer → wedge (c))*

**Entry 3 — Site engineer, Day 9**
- Trigger: Site queried availability
- Looking for: "Need 40 boxes of the exact same batch/shade of tile already laid on site — site foreman says current stock ran out"
- Where: Called dealer, site visit
- Found: Yes, fully
- Workaround: (none — but took a site visit to confirm shade match in person)
- Time spent: 15–30 min
- Frustration: 2
- *(tag applied on review: wedge (c) — resolved by a physical dealer visit; would have been faster with a live dealer-stock layer, but not high-frustration because they had a known dealer relationship)*

These three alone already hint at the pattern the study is designed to surface: entry 1 and 2 land partly on spec-verify pain and partly on sourcing pain simultaneously — which is exactly why the *tagging in weekly review* (§6), not the participant's own categorization, has to do the wedge classification. Participants should never be asked to self-label which "wedge" a lookup belongs to; they don't think in our taxonomy, and asking would bias them toward whichever wedge we describe most legibly.

## 3. Capture mechanism — recommendation: WhatsApp, not a Google Form

**Recommended: a WhatsApp number (personal or WhatsApp Business, not the paid Cloud API tier) running the log as a scripted conversation, not a form link.**

Why WhatsApp over Google Form for this population:
- Indian design professionals already live in WhatsApp all day for client and dealer communication — zero new app, zero new login, zero context-switch. A Google Form requires opening a browser, which measurably kills same-day completion rates for anything logged in the field (on-site, in a supplier showroom, standing at a dealer counter — exactly where these events happen and mobile data may be patchy).
- Voice notes are a first-class input Indians already default to over typing on mobile — the free-text fields (what were you looking for / workaround) work far better as a 10-second voice note than as typed text. A Google Form cannot accept a voice note inline as naturally as WhatsApp can.
- Numbered/lettered quick-reply templates ("Reply 1/2/3/4/5") get higher completion than a multi-page form with required-field validation errors, which is where Google Forms lose people who are filling this out one-handed on a site visit.
- No participant account creation, no link to lose track of — the study lives in a thread they already have open.

**Operational build (lightweight, no paid infra needed at N=10):**
- One dedicated WhatsApp number the founder monitors (personal SIM or WhatsApp Business app — free tier is sufficient at this volume; do not build a Cloud API bot for a 10-person 2-week study, that's over-engineering the instrument for the sample size).
- Pin a message in each participant's chat with the 8-field template as a numbered checklist they copy-paste-fill, OR send it back to them pre-filled with blanks after each daily prompt, so they just reply inline. Either works; test with 1 pilot participant on day 0 (see §4) and pick whichever gets faster replies.
- Founder (or a single research assistant) logs each incoming reply into a shared spreadsheet (Google Sheet, one row per event, columns = the 8 fields + participant ID + timestamp + founder's wedge tag) within 24 hours, so the weekly review in §6 has current data and so any pattern of confusion in how people are answering gets caught and corrected via a quick WhatsApp message before it corrupts a second week of entries.

**When to deviate to a Google Form instead:** only if a specific participant explicitly prefers it (some principals delegate logging to an assistant who may prefer a form) — offer it as a fallback option per-participant, not as the default.

## 4. Cadence

- **Day 0 (before the study starts):** 15-minute kickoff call per participant (can be a shared group call for 3–4 people at once to save time) — explain the study in one sentence ("we want to know where finding/verifying/sourcing materials wastes your time"), do a live example log together on WhatsApp so the template is unambiguous, confirm UPI ID for payment, confirm they understand this measures their normal work, not a special task — they should NOT go looking for extra material lookups to log, only log lookups that would have happened anyway.
- **Daily prompt:** one WhatsApp message at 7:00 PM IST — "How many material lookups today? Send one entry per lookup (reply 0 if none today)." Evening timing works better than morning because it catches the day's site visits/dealer calls in recent memory; do not ask for real-time logging as the *primary* ask (it's a bonus if they do it live) since real-time logging is the first thing that gets dropped under actual work pressure.
- **Expected volume:** design professionals doing active projects typically hit 1–4 genuine material lookups on an active day, close to 0 on an admin-only day. Do not treat zero-entry days as dropout — they are valid signal (low frequency in itself is informative) as long as the participant confirms "0 today" rather than going silent.
- **Mid-study check-in (Day 7, mandatory, ties to full incentive):** 10-minute call, not a text. Ask three things only: (1) "What's the most annoying material-lookup moment you've had this week?" (open-ended, catches things the structured log undersells), (2) spot-check: does their WhatsApp log so far match what they actually remember doing — corrects silent misuse of the template, (3) re-motivate: tell them how many entries they've logged vs. expected pace, and that finishing gets the ₹500 top-up. This single call is the highest-leverage anti-dropout mechanism in the whole design — diary studies die in week 2 from fatigue, and a human check-in mid-way measurably recovers completion (standard finding across diary-study methodology, not specific to this study).
- **Day 14 close-out:** short WhatsApp voice-note ask — "Of everything you logged, what ONE thing would you pay to never have to deal with again?" This single open question at the end is the cheapest possible face-validity check against the quantitative wedge_score — if the qualitative answers cluster on a different wedge than the math says, that's a signal to dig in before committing engineering months, not to override the math with a good anecdote.

## 5. Weekly (not just final) review — where wedge-tagging happens

Do not wait until day 14 to look at the data. Every Sunday (end of week 1 and end of week 2):
1. Read every logged event that week.
2. Tag each event's *underlying* wedge — (a) spec-verify, (b) BOQ-assembly, (c) sourcing/dealer-proximity, or `mixed` if it genuinely spans two (see Entry 2 above) — split a mixed event's time/frustration proportionally across the wedges it touches rather than double-counting or arbitrarily picking one.
3. Flag any participant whose entries look templated/low-effort (e.g., every entry says "Yes, fully," frustration always 1) and follow up personally — low-signal entries dilute the dataset and are worth a WhatsApp nudge ("tell me about a time this WEEK it was annoying, even a little") rather than being silently kept or silently dropped.
4. Update a running tally of `event_share`, `failure_rate`, and mean time-cost per wedge so that by day 14 the decision rule computation in §6 is a 10-minute exercise, not a scramble.

## 6. Analysis plan — from logged events to a wedge decision

**Step 1 — Tag.** Every event gets exactly one primary wedge tag (a/b/c), with mixed events split proportionally (§5.2). Discard entries that are not material-lookup-related (rare, but a participant may occasionally log something off-topic).

**Step 2 — Compute per wedge, across all 10 participants' pooled events:**

| Metric | Definition | Source fields |
|---|---|---|
| `event_share` | (events tagged to wedge) / (total events) | tag |
| `failure_rate` | (events tagged to wedge where Found = "No" or "Partially") / (events tagged to wedge) | Found field |
| `mean_time_cost` | mean of the time-bucket midpoints (`<5`→2.5, `5–15`→10, `15–30`→22.5, `30–60`→45, `>1hr`→75, `Still unresolved`→90) for events tagged to wedge | Time field |
| `normalized_time_cost` | `mean_time_cost` / max(`mean_time_cost` across the 3 wedges) | derived |
| `mean_frustration` | mean of 1–5 frustration for events tagged to wedge | Frustration field |
| `wedge_score` | `event_share × failure_rate × normalized_time_cost` | derived |

**Step 3 — Apply the decision rule from §0:**
1. Drop any wedge with `event_share < 15%` (too rare to be a wedge, regardless of pain) or `failure_rate < 30%` (current tools/workarounds already handle it — we'd be solving a solved problem).
2. Among the remaining wedges, pick the highest `wedge_score`.
3. If the top two remaining wedges are within 10% of each other on `wedge_score`, break the tie with `mean_frustration` — higher frustration wins.
4. Cross-check qualitatively: does the Day-14 "what would you pay to never deal with again" answer (§4) point at the same wedge? If yes, ship the decision with confidence. If a majority of participants named a *different* wedge than the math selected, do not silently override the math — instead run one extra week of a targeted 5-person mini-follow-up on the disagreement before committing, since that disagreement usually means the instrument mis-tagged something (commonly: `mixed` events being tagged to the wrong primary wedge) rather than that the math is wrong.

**Step 4 — Segment cut.** Re-run the same table split by participant segment (architect / interior designer / site-procurement / junior). If the winning wedge is only winning within one segment (e.g., sourcing/dealer-proximity wins only among site engineers, who are 2 of 10 participants and not our primary buyer), downweight accordingly — the wedge must win among the segments that are actually the intended first customer (architecture firms + interior designers, per `VISION.md`'s architect/procurement-team target buyer), not just in the aggregate.

**Step 5 — Write the decision down** in one paragraph in `MEMORY.md` under a new dated entry: which wedge won, the four numbers that drove it (`event_share`, `failure_rate`, `normalized_time_cost`, `wedge_score`) per wedge, and the qualitative cross-check result. This becomes the citation the day someone (including future-you) asks "why did we build BOQ-assembly first instead of spec-verify."

## 7. What "done" looks like

- 10 participants recruited, Day-0 calls completed.
- ≥ 80% of expected daily entries logged per participant (expected pace ≈ 1–4/active day; exact count varies naturally — track completion as "logged every day, even if 0" rather than a fixed entry-count target).
- Mid-study check-in call completed with all 10.
- Weekly tagging done both Sundays, not deferred to the end.
- Final table (Step 2 in §6) computed, decision rule applied, tie-break and qualitative cross-check documented.
- One paragraph landed in `MEMORY.md` naming the chosen wedge and the numbers behind it.
- Total cost: ≤ ₹35,000 incentive + founder time (~15 min/day monitoring WhatsApp, ~1 hr/week tagging, ~3 hrs Day-0 calls, ~2 hrs mid-study calls, ~2 hrs final analysis). No new paid tooling required.
