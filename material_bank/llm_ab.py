"""A/B eval: Flash vs Flash-Lite on the SAME products, through the exact
production path (image prep -> batch -> sanitize/verify -> metrics). The decision
rule is PRE-COMMITTED here, before any output is seen, so the winner is
mechanical — no vibes with $104 riding on it.

Flash-Lite wins only if, vs Flash on the same products:
  1. pass rate within 2 points,
  2. vision↔pixel colour agreement within 5 points,
  3. keyword usefulness (overlap with the product's own terms) within 5 points,
and no HARD disqualifier: malformed JSON > 2%, or vision disagreement materially
worse (image-cites that don't match the pixel cross-check = hallucinated vision).
Criterion 3-of-the-brief (blind prose hallucination check) is a human step — this
module dumps shuffled, model-hidden description pairs for that review. Asymmetry:
"passable but flatter" prose still loses to Flash — Flash-Lite must genuinely hold.
"""

from __future__ import annotations

import json
import random
import time

from . import db, image_colour, image_prep
from . import llm_accounting as acct
from .llm_batch import GeminiBatchTransport, build_batch_request
from .llm_enrich import (
    MODEL,
    PROMPT_VERSION,
    _INPUT_FIELDS,
    extract_json,
    sanitize,
    serialise,
    verify,
)

FLASH = MODEL                                # the pinned production model
LITE = "gemini-flash-lite-latest"

_COLOUR_FAMILY = {
    "white": "White", "ivory": "White", "cream": "White", "off-white": "White", "snow": "White",
    "beige": "Beige", "sand": "Beige", "taupe": "Beige", "travertine": "Beige",
    "grey": "Grey", "gray": "Grey", "silver": "Grey", "charcoal": "Grey", "graphite": "Grey",
    "black": "Black", "brown": "Brown", "walnut": "Brown", "teak": "Brown", "oak": "Brown",
    "wenge": "Brown", "chocolate": "Brown", "tan": "Brown",
    "blue": "Blue", "navy": "Blue", "teal": "Blue", "green": "Green", "olive": "Green", "sage": "Green",
    "red": "Red", "maroon": "Red", "terracotta": "Red", "brick": "Red",
    "yellow": "Yellow", "gold": "Gold", "mustard": "Yellow", "orange": "Orange",
    "pink": "Pink", "blush": "Pink", "purple": "Purple",
}


def _tokens(s):
    import re
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _metrics(results, rowmap, *, model, pixel_cache) -> dict:
    n = malformed = passed = 0
    col_hits = col_total = 0
    kw_overlap_sum = kw_count = 0
    in_tok = out_tok = 0
    descriptions = {}
    for res in results:
        row = rowmap.get(res.get("key"))
        if row is None:
            continue
        n += 1
        u = res.get("usage") or {}
        in_tok += u.get("input_tokens", 0); out_tok += u.get("output_tokens", 0)
        if not res.get("text"):
            malformed += 1
            continue
        try:
            out = extract_json(res["text"])
        except ValueError:
            malformed += 1
            continue
        input_text, fmap, _img = serialise(row)
        out = sanitize(out, fmap, input_text)
        if not verify(out, fmap, input_text):
            passed += 1
        # colour vs pixel cross-check
        pf = pixel_cache.get(res["key"])
        if pf and pf != "unknown":
            col_total += 1
            llm_col = str(((out.get("vision") or {}).get("colour_primary") or {}).get("value", "")).lower()
            if _COLOUR_FAMILY.get(llm_col) == pf:
                col_hits += 1
        # keyword usefulness: share a token with the product's title/finish/category
        prod_tok = _tokens(" ".join(str(v) for _, v in fmap.values()))
        kws = out.get("search_keywords") or []
        if kws:
            grounded = sum(1 for k in kws if _tokens(k) & prod_tok)
            kw_overlap_sum += grounded / len(kws); kw_count += 1
        descriptions[res["key"]] = " ".join(s.get("text", "") for s in (out.get("description") or []))
    return {
        "model": model, "n": n,
        "pass_rate": round(passed / n, 3) if n else 0,
        "malformed_rate": round(malformed / n, 3) if n else 0,
        "colour_agreement": round(col_hits / col_total, 3) if col_total else None,
        "colour_checked": col_total,
        "keyword_overlap": round(kw_overlap_sum / kw_count, 3) if kw_count else 0,
        "cost_inr": round(acct.call_cost(in_tok, out_tok, model, batch=True), 3),
        "avg_cost_per_product": round(acct.call_cost(in_tok, out_tok, model, batch=True) / n, 5) if n else 0,
        "_descriptions": descriptions,
    }


def decide(flash: dict, lite: dict) -> dict:
    """The pre-committed mechanical rule. 'blind_prose_pending' means the numbers
    pass but a human must still clear criterion 3 (no hallucination/flatter prose)."""
    # hard disqualifiers for lite
    if lite["malformed_rate"] > 0.02:
        return {"winner": FLASH, "reason": f"lite malformed JSON {lite['malformed_rate']:.1%} > 2%"}
    if (flash["colour_agreement"] is not None and lite["colour_agreement"] is not None
            and lite["colour_agreement"] < flash["colour_agreement"] - 0.10):
        return {"winner": FLASH, "reason": "lite vision↔pixel materially worse (>10pt) — hallucination risk"}
    pass_ok = lite["pass_rate"] >= flash["pass_rate"] - 0.02
    colour_ok = (flash["colour_agreement"] is None or lite["colour_agreement"] is None
                 or lite["colour_agreement"] >= flash["colour_agreement"] - 0.05)
    kw_ok = lite["keyword_overlap"] >= flash["keyword_overlap"] - 0.05
    if pass_ok and colour_ok and kw_ok:
        return {"winner": "flash-lite (pending blind prose review)",
                "reason": "within thresholds on pass / colour / keywords",
                "checks": {"pass_ok": pass_ok, "colour_ok": colour_ok, "kw_ok": kw_ok}}
    return {"winner": FLASH,
            "reason": f"lite outside thresholds pass_ok={pass_ok} colour_ok={colour_ok} kw_ok={kw_ok}"}


def _wait(transport, job, *, timeout=900, every=15):
    for _ in range(timeout // every):
        try:
            return transport.results(job)
        except Exception:
            time.sleep(every)
    raise TimeoutError(f"batch {job} not done in {timeout}s")


def run_ab(db_path, *, n=600, out_dir="reports", seed=13) -> dict:
    conn = db.connect(str(db_path))
    db.migrate(conn)
    rows = conn.execute(
        f"SELECT id, {', '.join(_INPUT_FIELDS)}, image_url FROM products "
        "WHERE title IS NOT NULL AND image_url IS NOT NULL "
        "ORDER BY id LIMIT ?", (n * 3,)).fetchall()
    random.Random(seed).shuffle(rows)
    rows = rows[:n]
    rowmap = {str(r["id"]): r for r in rows}
    reqs = [build_batch_request(r) for r in rows]              # image prep (cached), model-agnostic
    # pixel colour cross-check per product (from the cached ≤384px image)
    pixel = {}
    for r in rows:
        img = image_prep.prepare_image(r["image_url"])
        pixel[str(r["id"])] = image_colour.analyze(img)["colour_family"] if img else None
    # submit both models on the SAME requests
    jobs = {m: GeminiBatchTransport(m).submit(reqs) for m in (FLASH, LITE)}
    results = {m: _wait(GeminiBatchTransport(m), j) for m, j in jobs.items()}
    rep = {m: _metrics(results[m], rowmap, model=m, pixel_cache=pixel) for m in (FLASH, LITE)}
    rep["decision"] = decide(rep[FLASH], rep[LITE])
    _dump_blind_pairs(rep, rowmap, out_dir, seed)
    for m in (FLASH, LITE):
        rep[m].pop("_descriptions", None)
    return rep


def run_canary(db_path, *, model=MODEL, n=400, prepare=None,
               transport_factory=GeminiBatchTransport, value_sorted=True) -> dict:
    """Re-canary the PRODUCTION config (batch path, thinking-off) on ``model`` before
    resuming the drain. This validates a genuinely NEW, untested configuration:
    Flash-thinking-off. The A/B's 0.64 keyword grounding was Flash-thinking-ON, a
    config that no longer exists; Flash-Lite's 0.48 was its natural (no-think) mode.
    Flash-thinking-off sits between them — expected to hold (structured extraction
    with the image rarely needs chain-of-thought), but unmeasured. ``model`` is a
    parameter precisely so a ~$1 Flash-Lite arm can be bolted on for one run if
    grounding slides toward Lite territory, reviving its $28 finish as a live option.

    Emits five signals:
      pass_rate          — first-pass verifier survival (target ≈ the A/B's 0.98)
      keyword_grounding  — search-keyword overlap with the product's own terms (≈0.64)
      thinking_tokens_max— proof thinkingBudget=0 is honored (MUST be 0)
      retry_rate         — first-pass verifier-fail fraction: in the one-shot batch
                           drain this is paid-but-no-product WASTE, and equals the
                           retry-trigger rate if a retry policy were added (each a $)
      inr_per_product    — MEASURED ₹/product, the reconciliation seed for step 4

    Runs through the batch path (what the drain uses). Logs each call to the ledger
    (phase='canary') so the spend is captured for reconcile(); products are NOT
    mutated — pure eval, they re-enrich in the real drain (trivial re-cost)."""
    from .llm_batch import _prep_image, _select
    prepare = prepare or _prep_image
    conn = db.connect(str(db_path)); db.migrate(conn)
    if value_sorted:
        rows = _select(conn, n)                                # value-first, un-enriched
    else:
        rows = conn.execute(f"SELECT id, {', '.join(_INPUT_FIELDS)}, image_url, llm_hash "
                            "FROM products WHERE title IS NOT NULL ORDER BY id LIMIT ?", (n,)).fetchall()
    if not rows:
        return {"model": model, "n": 0, "note": "no un-enriched products to canary"}
    rowmap = {str(r["id"]): r for r in rows}
    reqs = [build_batch_request(r, prepare=prepare) for r in rows]
    tp = transport_factory(model)
    results = _wait(tp, tp.submit(reqs))
    for res in results:                                        # ledger the spend (recon seed)
        u = res.get("usage") or {}
        acct.log_call(conn, product_id=res.get("key"),
                      model=res.get("model_version") or model, phase="canary", attempt=0,
                      input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                      status="enriched" if res.get("text") else "api_error",
                      batch=True, prompt_version=PROMPT_VERSION)
    conn.commit()
    m = _metrics(results, rowmap, model=model, pixel_cache={})
    thinks = [(r.get("usage") or {}).get("thinking_tokens", 0) for r in results]
    return {
        "model": model, "n": m["n"],
        "pass_rate": m["pass_rate"],
        "keyword_grounding": m["keyword_overlap"],
        "malformed_rate": m["malformed_rate"],
        "thinking_tokens_max": max(thinks) if thinks else 0,
        "retry_rate": round(1 - m["pass_rate"], 3),
        "inr_per_product": m["avg_cost_per_product"],
        "usd_per_product": round((m["avg_cost_per_product"] or 0) / acct.USD_INR, 6),
        "canary_spend_inr": round(m["cost_inr"], 3),
    }


def _dump_blind_pairs(rep, rowmap, out_dir, seed):
    import pathlib
    fd, fl = rep[FLASH]["_descriptions"], rep[LITE]["_descriptions"]
    keys = [k for k in fd if k in fl][:30]
    rng = random.Random(seed)
    lines, key_map = [], {}
    for i, k in enumerate(keys, 1):
        a, b = (("A", fd[k]), ("B", fl[k])) if rng.random() < 0.5 else (("A", fl[k]), ("B", fd[k]))
        key_map[i] = {"product": (rowmap[k]["title"] if k in rowmap else k),
                      "A": "flash" if a[1] == fd[k] else "flash-lite"}
        lines.append(f"### {i}. {rowmap[k]['title'] if k in rowmap else k}\n- **A:** {a[1]}\n- **B:** {b[1]}\n")
    p = pathlib.Path(out_dir); p.mkdir(parents=True, exist_ok=True)
    (p / "ab_blind_pairs.md").write_text("# Blind description pairs (model hidden)\n\n" + "\n".join(lines))
    (p / "ab_key.json").write_text(json.dumps(key_map, indent=1))


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="mb-llm-ab")
    ap.add_argument("--canary", action="store_true",
                    help="re-canary the production config (batch, thinking-off) on --model")
    ap.add_argument("--model", default=MODEL, help="model to canary (e.g. gemini-flash-lite-latest for the $1 arm)")
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    if args.canary:
        out = run_canary(args.db, model=args.model, n=args.n)
    else:
        out = run_ab(args.db, n=args.n)
    print(json.dumps(out, indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
