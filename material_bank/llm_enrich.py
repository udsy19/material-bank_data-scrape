"""LLM enrichment (Stage B / Phase D) — generated content, verified by construction.

The move that keeps the honesty guarantee: the model must CITE, per sentence and
per tag, which input field(s) or the image it derives from, and a battery of
deterministic verifiers then checks every claim mechanically. Generated fields
are BONUS — stored in ``llm_content``, basis ``generated:llm:*``, and never
counted toward the completeness score that feeds the publish gate. A record must
still clear the gate on measured fields alone.

Conforms to autonomy-first: novelty-gated (re-enrich only changed records),
budget-capped (circuit breaker, not a crash), resumable. The client is injected
so the whole pipeline — including the verifiers, the crux — tests offline with a
fake model; the default client calls Gemini.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .db import now_iso

PROMPT_VERSION = "v3"

# words that don't count as "new information" when checking for restatement filler
_STOPWORDS = {
    "a", "an", "the", "is", "are", "it", "its", "this", "that", "of", "for",
    "with", "and", "or", "to", "in", "on", "from", "as", "by", "has", "have",
    "which", "also", "be", "being", "at", "into", "features", "feature",
}
# meta / plumbing words: describing the record, not the product — never "new info"
_PLUMBING_WORDS = {
    "product", "item", "brand", "category", "supplier", "domain", "sku", "field",
    "standard", "identified", "specified", "titled", "manufactured", "belongs",
    "falls", "under", "named", "called", "listed", "record",
}
# tag sources that are too generic to justify a facet (a tag must trace to a
# DISTINGUISHING attribute or the image, not just name/brand/category)
_GENERIC_FIELDS = {"title", "brand", "category_std", "category", "supplier_domain"}
_ID_IN_PROSE_RE = re.compile(r"\b(?:f\d+|img1)\b", re.I)
_MAX_TAGS = 3

STYLE_VOCAB = {
    "modern", "contemporary", "traditional", "rustic", "industrial", "minimalist",
    "scandinavian", "bohemian", "coastal", "mid-century", "art-deco", "transitional",
    "classic", "luxury",
}
USE_CASE_VOCAB = {
    "living-room", "bedroom", "bathroom", "kitchen", "outdoor", "commercial",
    "residential", "high-traffic", "wet-area", "flooring", "wall", "facade",
    "backsplash", "countertop", "office", "hospitality",
}
MATERIAL_VOCAB = {
    "marble", "granite", "wood", "stone", "concrete", "terrazzo", "metal",
    "ceramic", "porcelain", "glass", "fabric", "leather", "laminate", "quartz",
}
# a sentence citing only the image (img1) is allowed only for a visual claim.
# Covers visual descriptors, colour names, and form/shape words (all observable
# from a photo) — kept broad on purpose: a false reject blocks legitimate copy.
_VISUAL_LEXICON = {
    "colour", "color", "coloration", "tone", "shade", "pattern", "grain", "veining",
    "vein", "texture", "finish", "matte", "glossy", "look", "hue", "surface",
    "speckled", "mottled", "geometric", "floral", "appearance", "appears", "visual",
    "visually", "design", "form", "shape", "silhouette", "profile", "contour",
    "curved", "wavy", "folded", "fold", "round", "square", "rectangular", "slim",
    "sleek", "predominant", "white", "black", "grey", "gray", "brown", "beige",
    "blue", "green", "red", "olive", "orange", "yellow", "pink", "gold", "silver",
    "cream", "ivory", "charcoal", "navy", "teal", "walnut", "oak", "terracotta",
} | MATERIAL_VOCAB
# banned phrase -> the input token that would justify it (None = always banned)
_BANNED = {
    "best-in-class": None, "best in class": None, "world-class": None,
    "number one": None, "unbeatable": None, "guaranteed": None,
    "waterproof": "waterproof", "water-proof": "waterproof",
    "scratch-proof": "scratch", "scratchproof": "scratch",
    "anti-bacterial": "anti-bacterial", "antibacterial": "anti-bacterial",
    "frost-resistant": "frost", "fire-resistant": "fire",
    "suitable for outdoor": "outdoor", "outdoor use": "outdoor",
}
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_STD_RE = re.compile(r"\b(?:ISO|IS|EN|PEI|ASTM|BIS|DIN)\s?-?\s?\d+", re.I)

_INPUT_FIELDS = ("title", "brand", "category_std", "size_mm", "finish", "color",
                 "colour_primary", "price_unit", "supplier_domain", "description")


def serialise(row) -> tuple[str, dict, str | None]:
    """(input_text, field_map {id: (name, value)}, image_url). Every input field
    gets a stable id (f1..); the image is img1 — these ids are what claims cite."""
    fmap: dict[str, tuple[str, str]] = {}
    lines, i = [], 0
    for name in _INPUT_FIELDS:
        v = row[name] if name in row.keys() else None
        if v is None or str(v).strip() == "":
            continue
        i += 1
        fid = f"f{i}"
        fmap[fid] = (name, str(v))
        lines.append(f"{fid} [{name}]: {v}")
    return "\n".join(lines), fmap, (row["image_url"] if "image_url" in row.keys() else None)


def novelty_hash(input_text: str, image_url: str | None) -> str:
    """Version-prefixed so a prompt bump re-enriches: a record stored under v2 has
    hash 'v2:...', which won't match the current 'v3:...' and is re-processed."""
    sha = hashlib.sha1(f"{input_text}|{image_url or ''}".encode()).hexdigest()[:16]
    return f"{PROMPT_VERSION}:{sha}"


SYSTEM_PROMPT = f"""You write SHORT, useful catalog copy for architectural materials, and you NEVER invent facts.
Rules (prompt {PROMPT_VERSION}):
- SYNTHESIZE, do not enumerate. 2-4 sentences that tell a buyer what it is, what it's like, and where it fits — NOT a restatement of the fields. A sentence that only repeats one field's value is banned.
- NEVER write field ids (f1, img1), field names, the supplier domain, ".com", "category standard", or internal plumbing in the prose. Write for a customer, not about the database.
- Every sentence and tag still CITES its source id(s) in the JSON `sources` array (not in the prose text). Cite img1 only for visual claims (colour, pattern, texture, material look, finish, shape).
- Never state a number, dimension, or standard code (ISO/IS/PEI/BIS) not in the input. No superlatives or performance claims (waterproof, scratch-proof, best-in-class, outdoor) unless that exact property is in the input.
- Tags: at most {_MAX_TAGS} style and {_MAX_TAGS} use-case tags. Prefer two right tags over six safe ones. A tag must be justified by a DISTINGUISHING attribute (size, finish, colour, the image) — not merely the name/brand/category. Empty style_tags ([]) is perfectly fine.
"""


def build_prompt(input_text: str, has_image: bool) -> str:
    return (f"{SYSTEM_PROMPT}\n"
            f"ALLOWED style_tags (design styles only, [] if none fit): {sorted(STYLE_VOCAB)}\n"
            f"ALLOWED use_case_tags: {sorted(USE_CASE_VOCAB)}\n"
            f"vision.material_look — ONLY one of {sorted(MATERIAL_VOCAB)} or \"unknown\".\n"
            f"vision.colour_primary — a single common colour word or \"unknown\".\n"
            f"INPUT FIELDS:\n{input_text}\n"
            f"IMAGE: {'img1 attached' if has_image else 'none'}\n"
            "Return JSON: {description:[{text,sources[]}], style_tags:[{tag,sources[]}], "
            "use_case_tags:[{tag,sources[]}], vision:{colour_primary:{value,confidence}, "
            "material_look:{value,confidence}, finish:{value,confidence}}, "
            "self_report:{input_sufficient:bool,notes}}")


def _words(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _sentences_ok(items, fmap, input_text, supplier_domain):
    valid_ids = set(fmap) | {"img1"}
    fails = []
    for it in items:
        if not isinstance(it, dict) or "text" not in it:
            return ["malformed description item"]
        text = it["text"]
        low = text.lower()
        srcs = it.get("sources") or []
        if not srcs or any(s not in valid_ids for s in srcs):
            fails.append(f"bad/absent source cite: {text[:40]!r}")
        if srcs == ["img1"] and not any(w in low for w in _VISUAL_LEXICON):
            fails.append(f"img-only cite on non-visual claim: {text[:40]!r}")
        # (1) no restatement filler: a text-only sentence that adds no word beyond
        #     its cited fields' values (minus stop/plumbing words) is worthless.
        if "img1" not in srcs:
            field_words = _words(" ".join(fmap[s][1] for s in srcs if s in fmap))
            novel = _words(text) - _STOPWORDS - _PLUMBING_WORDS - field_words
            if not novel:
                fails.append(f"restatement filler: {text[:40]!r}")
        # (2) no plumbing in prose: field ids, field names, the domain, ".com"
        if _ID_IN_PROSE_RE.search(text):
            fails.append(f"citation id in prose: {text[:40]!r}")
        if ".com" in low or "supplier domain" in low or "category standard" in low or (
                supplier_domain and supplier_domain.lower() in low):
            fails.append(f"plumbing in prose: {text[:40]!r}")
        for num in _NUM_RE.findall(text):
            if num not in input_text:
                fails.append(f"invented number {num!r}")
        for std in _STD_RE.findall(text):
            if std.replace(" ", "").lower() not in input_text.replace(" ", "").lower():
                fails.append(f"invented standard {std!r}")
        for phrase, need in _BANNED.items():
            if phrase in low and (need is None or need not in input_text.lower()):
                fails.append(f"banned phrase {phrase!r}")
    return fails


def verify(output: dict, field_map: dict, input_text: str) -> list[str]:
    """Deterministic verification of one LLM output. [] == passes."""
    if not isinstance(output, dict):
        return ["not a JSON object"]
    valid_ids = set(field_map) | {"img1"}
    supplier_domain = next((v for n, v in field_map.values() if n == "supplier_domain"), None)
    fails: list[str] = []
    for key in ("description", "style_tags", "use_case_tags", "vision"):
        if key not in output:
            fails.append(f"missing key {key}")
    if fails:
        return fails
    if not isinstance(output["description"], list) or not output["description"]:
        fails.append("description must be a non-empty list")
    elif len(output["description"]) > 6:
        fails.append("description too long (>6 sentences)")
    else:
        fails += _sentences_ok(output["description"], field_map, input_text, supplier_domain)
    # (3) tag discipline: cap count; every tag must trace to a DISTINGUISHING
    #     source (a non-generic field or the image), not just name/brand/category.
    for key, vocab in (("style_tags", STYLE_VOCAB), ("use_case_tags", USE_CASE_VOCAB)):
        tags = output.get(key) or []
        if len(tags) > _MAX_TAGS:
            fails.append(f"{key}: too many ({len(tags)} > {_MAX_TAGS})")
        for it in tags:
            if not isinstance(it, dict) or it.get("tag") not in vocab:
                fails.append(f"{key}: out-of-vocab {it.get('tag') if isinstance(it, dict) else it!r}")
                continue
            srcs = it.get("sources") or []
            if not srcs or any(s not in valid_ids for s in srcs):
                fails.append(f"{key}: bad source cite for {it.get('tag')!r}")
            elif not ("img1" in srcs or any(field_map.get(s, ("", ""))[0] not in _GENERIC_FIELDS
                                            for s in srcs if s in field_map)):
                fails.append(f"{key}: {it['tag']!r} not grounded in a distinguishing attribute")
    v = output.get("vision") or {}
    ml = ((v.get("material_look") or {}).get("value"))
    if ml and ml != "unknown" and ml not in MATERIAL_VOCAB:
        fails.append(f"vision.material_look out-of-vocab: {ml!r}")
    return fails


def _unpack(res):
    """Accept a rich client result {output, usage} or a bare output dict (fakes)."""
    if isinstance(res, dict) and "output" in res and "usage" in res:
        return res["output"], res.get("usage") or {}
    return res, {}


_SELECT_COLS = f"id, {', '.join(_INPUT_FIELDS)}, image_url, llm_hash"
_NEEDS_ENRICH = ("llm_status IS NULL OR llm_status='stale' OR llm_hash NOT LIKE ?")


def enrich_one(conn, row, *, client, client_strong=None, model_name: str = "gemini-flash-latest",
               phase: str = "realtime") -> str:
    """Enrich one product: call -> verify -> log every attempt -> write. Returns
    'enriched' | 'failed' | 'skipped'. Commits its own row (safe under threads)."""
    import time

    from . import llm_accounting as acct
    input_text, fmap, image_url = serialise(row)
    h = novelty_hash(input_text, image_url)
    if row["llm_hash"] == h:
        return "skipped"
    prompt = build_prompt(input_text, bool(image_url))
    out, ok = None, False
    for attempt, cl in enumerate((client, client, client_strong or client)):
        t0 = time.monotonic()
        try:
            res = cl(prompt, image_url)
        except Exception as exc:
            acct.log_call(conn, product_id=row["id"], model=model_name, phase=phase, attempt=attempt,
                          latency_ms=int((time.monotonic() - t0) * 1000), status="api_error",
                          fail_reason=str(exc), prompt_version=PROMPT_VERSION)
            continue
        latency = int((time.monotonic() - t0) * 1000)
        out, usage = _unpack(res)
        fails = verify(out, fmap, input_text)
        acct.log_call(conn, product_id=row["id"], model=model_name, phase=phase, attempt=attempt,
                      input_tokens=usage.get("input_tokens", 0),
                      output_tokens=usage.get("output_tokens", 0), latency_ms=latency,
                      status="enriched" if not fails else "verifier_failed",
                      fail_reason=(fails[0] if fails else None), prompt_version=PROMPT_VERSION)
        if not fails:
            ok = True
            break
    ts = now_iso()
    if ok:
        content = {**out, "_meta": {"basis": f"generated:llm:{model_name}:prompt_{PROMPT_VERSION}", "at": ts}}
        conn.execute("UPDATE products SET llm_content=?, llm_hash=?, llm_status='enriched', "
                     "llm_enriched_at=? WHERE id=?", (json.dumps(content), h, ts, row["id"]))
    else:
        conn.execute("UPDATE products SET llm_hash=?, llm_status='enrich_failed', "
                     "llm_enriched_at=? WHERE id=?", (h, ts, row["id"]))
    conn.commit()
    return "enriched" if ok else "failed"


def run(conn: sqlite3.Connection, *, client, client_strong=None, model_name: str = "gemini-flash-latest",
        budget_inr: float | None = None, limit: int = 100, phase: str = "realtime") -> dict:
    """Sequential novelty-gated, budget-capped pass (used by tests + small runs).
    The budget circuit-breaker reads REAL daily spend from the ledger."""
    from . import llm_accounting as acct
    rows = conn.execute(f"SELECT {_SELECT_COLS} FROM products WHERE {_NEEDS_ENRICH} "
                        "ORDER BY id LIMIT ?", (f"{PROMPT_VERSION}:%", limit)).fetchall()
    stats = {"scanned": len(rows), "enriched": 0, "failed": 0, "skipped_novelty": 0, "spend_inr": 0.0}
    for row in rows:
        if budget_inr is not None and acct.spend_since(conn, 1) >= budget_inr:
            break
        r = enrich_one(conn, row, client=client, client_strong=client_strong,
                       model_name=model_name, phase=phase)
        stats["enriched" if r == "enriched" else "failed" if r == "failed" else "skipped_novelty"] += 1
    stats["spend_inr"] = acct.spend_since(conn, 1)
    return stats


def drain_concurrent(db_path, *, model_name: str = "gemini-flash-latest", workers: int = 16,
                     budget_inr: float | None = None, batch: int = 40, client_factory=None) -> dict:
    """The production enrichment path: N threads, each a private connection +
    client, partition products by id%workers (no claim contention), enrich until
    dry or budget hit. Every call is on the ledger; watch spend live on /llm.
    Fully resumable — a killed run just re-selects what's still un-enriched."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from . import db
    from . import llm_accounting as acct
    make_client = client_factory or (lambda: gemini_client(model_name))
    stop = threading.Event()
    totals = {"enriched": 0, "failed": 0}
    lock = threading.Lock()

    def worker(wid: int) -> None:
        conn = db.connect(str(db_path), check_same_thread=False)
        client = make_client()
        try:
            while not stop.is_set():
                if budget_inr is not None and acct.spend_since(conn, 1) >= budget_inr:
                    stop.set(); break
                rows = conn.execute(
                    f"SELECT {_SELECT_COLS} FROM products WHERE ({_NEEDS_ENRICH}) "
                    "AND (id % ?) = ? ORDER BY id LIMIT ?",
                    (f"{PROMPT_VERSION}:%", workers, wid, batch)).fetchall()
                if not rows:
                    break
                for row in rows:
                    if stop.is_set():
                        break
                    r = enrich_one(conn, row, client=client, model_name=model_name)
                    if r in ("enriched", "failed"):
                        with lock:
                            totals[r] += 1
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in [pool.submit(worker, i) for i in range(workers)]:
            f.result()
    control = db.connect(str(db_path))
    out = {**totals, "spend_inr": acct.spend_since(control, 1),
           "remaining": control.execute(
               f"SELECT COUNT(*) FROM products WHERE {_NEEDS_ENRICH}", (f"{PROMPT_VERSION}:%",)
           ).fetchone()[0]}
    control.close()
    return out


def gemini_client(model: str = "gemini-flash-latest"):
    """Default live client (realtime Gemini). Requires GEMINI_API_KEY.
    Auth via the x-goog-api-key header (works for both key formats).
    Returns a callable(prompt, image_url) -> {output, usage}."""
    import os

    def _call(prompt: str, image_url: str | None) -> dict:
        from curl_cffi import requests
        key = os.environ["GEMINI_API_KEY"]
        parts = [{"text": prompt}]
        # (image bytes would be attached here for the vision path; omitted in v1
        #  realtime client — the batch pipeline attaches inline_data)
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": parts}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=60)
        body = r.json()
        if r.status_code != 200 or "candidates" not in body:
            # surface quota/safety errors clearly instead of a bare KeyError
            raise RuntimeError(f"gemini {r.status_code}: "
                               f"{(body.get('error') or {}).get('status', body)}")
        um = body.get("usageMetadata") or {}
        return {"output": json.loads(body["candidates"][0]["content"]["parts"][0]["text"]),
                "usage": {"input_tokens": um.get("promptTokenCount", 0),
                          "output_tokens": um.get("candidatesTokenCount", 0)}}

    return _call


def main(argv=None) -> int:
    import argparse
    import sys

    from . import db

    ap = argparse.ArgumentParser(prog="mb-llm-enrich")
    ap.add_argument("--drain", action="store_true", help="concurrent full drain (production path)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=100, help="sequential mode only")
    ap.add_argument("--budget-inr", type=float, default=500.0,
                    help="hard daily spend cap (circuit breaker, reads real ledger spend)")
    ap.add_argument("--model", default="gemini-flash-latest")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    db.migrate(db.connect(args.db))
    if args.drain:
        stats = drain_concurrent(args.db, model_name=args.model, workers=args.workers,
                                 budget_inr=args.budget_inr)
    else:
        conn = db.connect(args.db)
        stats = run(conn, client=gemini_client(args.model), model_name=args.model,
                    budget_inr=args.budget_inr, limit=args.limit)
        conn.close()
    print(json.dumps(stats), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
