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

PROMPT_VERSION = "v1"

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
# a sentence citing only the image (img1) is allowed only for a visual claim
_VISUAL_LEXICON = {
    "colour", "color", "tone", "shade", "pattern", "grain", "veining", "vein",
    "texture", "finish", "matte", "glossy", "look", "hue", "wood", "marble",
    "stone", "surface", "speckled", "mottled", "geometric", "floral",
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
    return hashlib.sha1(f"{input_text}|{image_url or ''}".encode()).hexdigest()[:16]


SYSTEM_PROMPT = f"""You write catalog copy for architectural materials, and you NEVER invent facts.
Rules (prompt {PROMPT_VERSION}):
- Every description sentence and every tag MUST cite the input field id(s) (f1, f2, ...) or the image (img1) it is based on.
- Cite img1 only for visual claims (colour, pattern, texture, material look, finish).
- Never state a number, dimension, standard code (ISO/IS/PEI/BIS), or material fact that is not in the input.
- No superlatives or performance claims (waterproof, scratch-proof, best-in-class, outdoor-suitable) unless that exact property is in the input.
- Controlled vocabularies only for tags. Output strict JSON matching the schema.
"""


def build_prompt(input_text: str, has_image: bool) -> str:
    return (f"{SYSTEM_PROMPT}\nINPUT FIELDS:\n{input_text}\n"
            f"IMAGE: {'img1 attached' if has_image else 'none'}\n"
            "Return JSON: {description:[{text,sources[]}], style_tags:[{tag,sources[]}], "
            "use_case_tags:[{tag,sources[]}], vision:{colour_primary:{value,confidence}, "
            "material_look:{value,confidence}, finish:{value,confidence}}, "
            "self_report:{input_sufficient:bool,notes}}")


def _sentences_ok(items, valid_ids, input_text):
    fails = []
    for it in items:
        if not isinstance(it, dict) or "text" not in it:
            return ["malformed description item"]
        srcs = it.get("sources") or []
        if not srcs or any(s not in valid_ids for s in srcs):
            fails.append(f"bad/absent source cite: {it.get('text','')[:40]!r}")
        if srcs == ["img1"] and not any(w in it["text"].lower() for w in _VISUAL_LEXICON):
            fails.append(f"img-only cite on non-visual claim: {it['text'][:40]!r}")
        for num in _NUM_RE.findall(it["text"]):
            if num not in input_text:
                fails.append(f"invented number {num!r}")
        for std in _STD_RE.findall(it["text"]):
            if std.replace(" ", "").lower() not in input_text.replace(" ", "").lower():
                fails.append(f"invented standard {std!r}")
        low = it["text"].lower()
        for phrase, need in _BANNED.items():
            if phrase in low and (need is None or need not in input_text.lower()):
                fails.append(f"banned phrase {phrase!r}")
    return fails


def verify(output: dict, field_map: dict, input_text: str) -> list[str]:
    """Deterministic verification of one LLM output. [] == passes."""
    if not isinstance(output, dict):
        return ["not a JSON object"]
    valid_ids = set(field_map) | {"img1"}
    fails: list[str] = []
    for key in ("description", "style_tags", "use_case_tags", "vision"):
        if key not in output:
            fails.append(f"missing key {key}")
    if fails:
        return fails
    if not isinstance(output["description"], list) or not output["description"]:
        fails.append("description must be a non-empty list")
    else:
        fails += _sentences_ok(output["description"], valid_ids, input_text)
    for key, vocab in (("style_tags", STYLE_VOCAB), ("use_case_tags", USE_CASE_VOCAB)):
        for it in output.get(key) or []:
            if not isinstance(it, dict) or it.get("tag") not in vocab:
                fails.append(f"{key}: out-of-vocab {it.get('tag') if isinstance(it, dict) else it!r}")
            elif not (it.get("sources") and all(s in valid_ids for s in it["sources"])):
                fails.append(f"{key}: bad source cite for {it.get('tag')!r}")
    v = output.get("vision") or {}
    ml = ((v.get("material_look") or {}).get("value"))
    if ml and ml != "unknown" and ml not in MATERIAL_VOCAB:
        fails.append(f"vision.material_look out-of-vocab: {ml!r}")
    return fails


def run(conn: sqlite3.Connection, *, client, client_strong=None, model_name: str = "gemini-flash",
        budget_inr: float | None = None, cost_per_call: float = 0.4, limit: int = 100) -> dict:
    """Novelty-gated, budget-capped enrichment pass. ``client(prompt, image_url)``
    returns a parsed JSON dict (or raises). Failures retry once, then escalate to
    ``client_strong``, then mark enrich_failed (honest absence, never hand-waved)."""
    rows = conn.execute(
        f"SELECT id, {', '.join(_INPUT_FIELDS)}, image_url, llm_hash "
        "FROM products WHERE llm_status IS NULL OR llm_status='stale' "
        "ORDER BY id LIMIT ?", (limit,)).fetchall()
    stats = {"scanned": len(rows), "enriched": 0, "failed": 0, "skipped_novelty": 0, "spend_inr": 0.0}
    for row in rows:
        input_text, fmap, image_url = serialise(row)
        h = novelty_hash(input_text, image_url)
        if row["llm_hash"] == h:
            stats["skipped_novelty"] += 1
            continue
        if budget_inr is not None and stats["spend_inr"] + cost_per_call > budget_inr:
            break                                     # circuit breaker: pause, don't crash
        prompt = build_prompt(input_text, bool(image_url))
        out, ok = None, False
        for attempt, cl in enumerate((client, client, client_strong or client)):
            try:
                out = cl(prompt, image_url)
                stats["spend_inr"] += cost_per_call * (2 if attempt == 2 else 1)
            except Exception:
                continue
            if not verify(out, fmap, input_text):
                ok = True
                break
        ts = now_iso()
        if ok:
            content = {**out, "_meta": {"basis": f"generated:llm:{model_name}:prompt_{PROMPT_VERSION}",
                                        "at": ts}}
            conn.execute("UPDATE products SET llm_content=?, llm_hash=?, llm_status='enriched', "
                         "llm_enriched_at=? WHERE id=?", (json.dumps(content), h, ts, row["id"]))
            stats["enriched"] += 1
        else:
            conn.execute("UPDATE products SET llm_hash=?, llm_status='enrich_failed', "
                         "llm_enriched_at=? WHERE id=?", (h, ts, row["id"]))
            stats["failed"] += 1
    conn.commit()
    stats["spend_inr"] = round(stats["spend_inr"], 3)
    return stats


def gemini_client(model: str = "gemini-2.5-flash"):
    """Default live client (batch/realtime Gemini). Requires GEMINI_API_KEY.
    Returns a callable(prompt, image_url) -> parsed JSON dict."""
    import os

    def _call(prompt: str, image_url: str | None) -> dict:
        from curl_cffi import requests
        key = os.environ["GEMINI_API_KEY"]
        parts = [{"text": prompt}]
        # (image bytes would be attached here for the vision path; omitted in v1
        #  realtime client — the batch pipeline attaches inline_data)
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            json={"contents": [{"parts": parts}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=60)
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(txt)

    return _call


def main(argv=None) -> int:
    import argparse
    import sys

    from . import db

    ap = argparse.ArgumentParser(prog="mb-llm-enrich")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--budget-inr", type=float, default=500.0,
                    help="hard daily spend cap (circuit breaker)")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    stats = run(conn, client=gemini_client(args.model), model_name=args.model,
                budget_inr=args.budget_inr, limit=args.limit)
    print(json.dumps(stats), file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
