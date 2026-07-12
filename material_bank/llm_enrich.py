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

PROMPT_VERSION = "v4"

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
_ID_IN_PROSE_RE = re.compile(r"\b(?:f\d+|img1)\b", re.I)
_MAX_TAGS = 3

# Evidence-based tagging (data, not code): each vocab tag carries the keywords
# that justify it. use_case tags are DERIVED deterministically from the record's
# text (grounded by construction, never an LLM guess); style tags come from the
# LLM but are kept only with textual evidence or image support. Editing the
# taxonomy is a JSON edit, not a deploy.
import pathlib as _pathlib  # noqa: E402

_TAG_VOCAB = json.loads((_pathlib.Path(__file__).parent / "tag_vocab.json").read_text())
_UC_EVID: dict = _TAG_VOCAB["use_case"]
_STYLE_EVID: dict = _TAG_VOCAB["style"]
STYLE_VOCAB = set(_STYLE_EVID)
USE_CASE_VOCAB = set(_UC_EVID)
MATERIAL_VOCAB = {
    "marble", "granite", "wood", "stone", "concrete", "terrazzo", "metal",
    "ceramic", "porcelain", "glass", "fabric", "leather", "laminate", "quartz",
}
PATTERN_VOCAB = {
    "marble", "wood", "stone", "concrete", "terrazzo", "geometric", "floral",
    "solid", "plain", "abstract", "mosaic", "textile", "metallic", "cement",
}
SURFACE_VOCAB = {
    "glossy", "matte", "satin", "textured", "structured", "polished", "rustic",
    "sugar", "lappato", "carving", "honed", "brushed",
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
The product IMAGE is attached when available — describe what you actually SEE, don't guess.
Rules (prompt {PROMPT_VERSION}):
- description: 3-4 sentences that SYNTHESIZE what it is, what it looks like, and where it fits — NOT a restatement of the fields. A sentence that only repeats one field's value is banned. Cite source id(s) in the JSON `sources` array (never in the prose). Cite img1 only for visual claims (colour, pattern, texture, finish, shape) — only when the image is attached.
- NEVER write field ids (f1, img1), field names, the supplier domain, ".com", or internal plumbing in the prose. Write for a customer, not about the database.
- Never state a number, dimension, or standard code (ISO/IS/PEI/BIS) not in the input. No superlatives/performance claims (waterproof, scratch-proof, best-in-class, outdoor) unless that exact property is in the input.
- feature_bullets: 3-5 short factual selling points from the input/image (no invented specs).
- search_keywords: 5-8 alternative terms a buyer might search (synonyms, looks, e.g. "carrara", "large-format", "wood-look"). Lowercase words/phrases.
- style_tags: 0-3 from the allowed vocab, only if the image or text supports it. Empty [] is fine.
- vision (from the IMAGE only; "unknown" if no image or unsure): colour_primary, pattern, surface_look, material_appearance.
"""


def build_prompt(input_text: str, has_image: bool) -> str:
    return (f"{SYSTEM_PROMPT}\n"
            f"ALLOWED style_tags: {sorted(STYLE_VOCAB)}\n"
            f"vision.pattern — one of {sorted(PATTERN_VOCAB)} or \"unknown\".\n"
            f"vision.surface_look — one of {sorted(SURFACE_VOCAB)} or \"unknown\".\n"
            f"vision.material_appearance — one of {sorted(MATERIAL_VOCAB)} or \"unknown\".\n"
            f"vision.colour_primary — a single common colour word or \"unknown\".\n"
            f"INPUT FIELDS:\n{input_text}\n"
            f"IMAGE: {'attached (describe what you see)' if has_image else 'NONE — set all vision values to unknown and do not cite img1'}\n"
            "Return strict JSON: {description:[{text,sources[]}], feature_bullets:[str], "
            "search_keywords:[str], style_tags:[{tag,sources[]}], "
            "vision:{colour_primary:{value,confidence}, pattern:{value,confidence}, "
            "surface_look:{value,confidence}, material_appearance:{value,confidence}}}")


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


def _has_evidence(keywords, text: str) -> bool:
    return any(k in text for k in keywords)


def derive_use_case_tags(text: str) -> list[str]:
    """Deterministic, evidence-grounded use-case tags from the record's own text
    (title/category/finish/colour). Grounded by construction — never a guess."""
    t = " " + (text or "").lower() + " "
    return [tag for tag, kws in _UC_EVID.items() if _has_evidence(kws, t)][:_MAX_TAGS]


def _bullet_clean(b, input_text: str) -> bool:
    """A feature bullet is kept if it's a non-empty string that invents no number,
    standard, or banned claim absent from the input (same honesty bar as prose)."""
    if not isinstance(b, str) or not b.strip():
        return False
    low, itl = b.lower(), input_text.lower()
    if any(n not in input_text for n in _NUM_RE.findall(b)):
        return False
    if any(s.replace(" ", "").lower() not in itl.replace(" ", "") for s in _STD_RE.findall(b)):
        return False
    return not any(p in low and (need is None or need not in itl) for p, need in _BANNED.items())


def _vision_vocab(v: dict, key: str, vocab: set) -> None:
    val = (v.get(key) or {}).get("value")
    if val and val != "unknown" and str(val).lower() not in vocab:
        v[key] = {"value": "unknown", "confidence": 0.0}


def sanitize(output: dict, field_map: dict, input_text: str = "") -> dict:
    """Make everything except the description robust WITHOUT a retry:
      - use_case_tags: deterministic, evidence-derived.
      - style_tags: LLM's, kept only with textual evidence OR image support.
      - feature_bullets: drop any that fabricate; cap 5.
      - search_keywords: lowercase, dedupe, cap 8.
      - vision: null out-of-vocab pattern/surface_look/material_appearance.
    The description is hard-verified by ``verify`` (the only thing that retries)."""
    if not isinstance(output, dict):
        return output
    o = dict(output)
    t = " " + (input_text or "").lower() + " "
    o["use_case_tags"] = [{"tag": tag, "sources": ["derived"]}
                          for tag in derive_use_case_tags(input_text)]
    style = []
    for it in (o.get("style_tags") or []):
        if not isinstance(it, dict) or it.get("tag") not in STYLE_VOCAB:
            continue
        tag, srcs = it["tag"], (it.get("sources") or [])
        ev = _has_evidence(_STYLE_EVID.get(tag, []), t)
        if ev or "img1" in srcs:
            style.append({"tag": tag, "sources": ["evidence"] if ev else ["img1"]})
    o["style_tags"] = style[:_MAX_TAGS]
    o["feature_bullets"] = [b.strip() for b in (o.get("feature_bullets") or [])
                            if _bullet_clean(b, input_text)][:5]
    seen, kw = set(), []
    for k in (o.get("search_keywords") or []):
        kl = k.strip().lower() if isinstance(k, str) else ""
        if kl and kl not in seen:
            seen.add(kl); kw.append(kl)
    o["search_keywords"] = kw[:8]
    v = dict(o.get("vision") or {})
    for key, vocab in (("pattern", PATTERN_VOCAB), ("surface_look", SURFACE_VOCAB),
                       ("material_appearance", MATERIAL_VOCAB)):
        _vision_vocab(v, key, vocab)
    o["vision"] = v
    return o


def verify(output: dict, field_map: dict, input_text: str) -> list[str]:
    """HARD verification — the ONLY thing that triggers a retry: the DESCRIPTION
    is present and honest (no fabrication/plumbing/restatement/bad cites).
    Everything else is handled by ``sanitize`` (drop, don't retry). [] passes."""
    if not isinstance(output, dict):
        return ["not a JSON object"]
    supplier_domain = next((v for n, v in field_map.values() if n == "supplier_domain"), None)
    if "description" not in output:
        return ["missing description"]
    if not isinstance(output["description"], list) or not output["description"]:
        return ["description must be a non-empty list"]
    if len(output["description"]) > 6:
        return ["description too long (>6 sentences)"]
    return _sentences_ok(output["description"], field_map, input_text, supplier_domain)


def extract_json(text: str) -> dict:
    """Parse model output that is *almost* JSON: strip ```json fences and trailing
    prose, then extract the first balanced {...}. Recovers the 'Extra data' /
    fence cases that responseMimeType doesn't always prevent, without a retry."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    start = t.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(t[start:i + 1])
    raise ValueError("no JSON object in model output")


def _unpack(res):
    """Accept a rich client result {output, usage} or a bare output dict (fakes)."""
    if isinstance(res, dict) and "output" in res and "usage" in res:
        return res["output"], res.get("usage") or {}
    return res, {}


_SELECT_COLS = f"id, {', '.join(_INPUT_FIELDS)}, image_url, llm_hash"
_NEEDS_ENRICH = ("llm_status IS NULL OR llm_status='stale' OR llm_hash NOT LIKE ?")


def _prepare_default(url):
    from . import image_prep
    return image_prep.prepare_image(url) if url else None


def enrich_one(conn, row, *, client, client_strong=None, model_name: str = "gemini-flash-latest",
               phase: str = "realtime", prepare=_prepare_default) -> str:
    """Enrich one product: prepare the image -> call -> verify (feedback-retry) ->
    log every attempt -> write. Returns 'enriched' | 'failed' | 'skipped'."""
    from . import llm_accounting as acct
    input_text, fmap, image_url = serialise(row)
    h = novelty_hash(input_text, image_url)
    if row["llm_hash"] == h:
        return "skipped"
    image = prepare(image_url)
    out, ok, logs = _attempt_calls(client, input_text, image, fmap, model_name, phase)
    for lg in logs:
        acct.log_call(conn, product_id=row["id"], prompt_version=PROMPT_VERSION, **lg)
    ts = now_iso()
    if ok:
        content = {**out, "_meta": {"basis": f"generated:llm:{model_name}:prompt_{PROMPT_VERSION}",
                                    "image": "used" if image else ("none" if not image_url else "unfetched"),
                                    "at": ts}}
        conn.execute("UPDATE products SET llm_content=?, llm_hash=?, llm_status='enriched', "
                     "llm_enriched_at=? WHERE id=?", (json.dumps(content), h, ts, row["id"]))
    else:
        conn.execute("UPDATE products SET llm_hash=?, llm_status='enrich_failed', "
                     "llm_enriched_at=? WHERE id=?", (h, ts, row["id"]))
    conn.commit()
    return "enriched" if ok else "failed"


def run(conn: sqlite3.Connection, *, client, client_strong=None, model_name: str = "gemini-flash-latest",
        budget_inr: float | None = None, limit: int = 100, phase: str = "realtime",
        prepare=_prepare_default) -> dict:
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
                       model_name=model_name, phase=phase, prepare=prepare)
        stats["enriched" if r == "enriched" else "failed" if r == "failed" else "skipped_novelty"] += 1
    stats["spend_inr"] = acct.spend_since(conn, 1)
    return stats


def _attempt_calls(client, input_text, image, fmap, model_name, phase, *, max_attempts: int = 2):
    """Network calls + verification, NO DB access (concurrency-friendly). ``image``
    is prepared JPEG bytes (or None). On a verifier failure the next attempt gets
    the rejection reason appended so the model FIXES it (cheaper than a blind
    re-roll). Returns (output|None, ok, [log-dicts]) for the caller to write."""
    import time
    base = build_prompt(input_text, bool(image))
    logs, out, ok, prev = [], None, False, None
    for attempt in range(max_attempts):
        prompt = base if prev is None else (
            base + f"\n\nYOUR PREVIOUS OUTPUT WAS REJECTED — reason: {prev}. "
                   "Return corrected strict JSON that fixes exactly this and nothing else.")
        t0 = time.monotonic()
        try:
            res = client(prompt, image)
        except Exception as exc:
            logs.append({"model": model_name, "phase": phase, "attempt": attempt,
                         "latency_ms": int((time.monotonic() - t0) * 1000),
                         "status": "api_error", "fail_reason": str(exc)})
            prev = None
            continue
        latency = int((time.monotonic() - t0) * 1000)
        o, usage = _unpack(res)
        o = sanitize(o, fmap, input_text)              # deterministic use-case tags + evidence style
        fails = verify(o, fmap, input_text)            # hard checks only -> retry
        logs.append({"model": model_name, "phase": phase, "attempt": attempt,
                     "input_tokens": usage.get("input_tokens", 0),
                     "output_tokens": usage.get("output_tokens", 0), "latency_ms": latency,
                     "status": "enriched" if not fails else "verifier_failed",
                     "fail_reason": (fails[0] if fails else None)})
        if not fails:
            out, ok = o, True
            break
        prev = fails[0]
    return out, ok, logs


def drain_concurrent(db_path, *, model_name: str = "gemini-flash-latest", workers: int = 16,
                     budget_inr: float | None = None, batch: int | None = None,
                     client_factory=None, prepare=_prepare_default) -> dict:
    """The production enrichment path. Image-prep + Gemini calls run concurrently
    across a thread pool; ALL DB writes go through ONE connection under a lock
    (writes are ~1ms, the network is the slow part and stays parallel) — so no
    'database is locked' thrash. Budget breaker on real ledger spend. Fully
    resumable: a killed run re-selects whatever is still un-enriched."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from . import db
    from . import llm_accounting as acct
    client = (client_factory or (lambda: gemini_client(model_name)))()
    control = db.connect(str(db_path), check_same_thread=False)
    wlock = threading.Lock()
    totals = {"enriched": 0, "failed": 0}
    chunk = batch or workers * 3

    def task(row):
        input_text, fmap, image_url = serialise(row)
        h = novelty_hash(input_text, image_url)
        image = prepare(image_url)                     # fetch+resize (cached) — concurrent
        out, ok, logs = _attempt_calls(client, input_text, image, fmap, model_name, "realtime")
        ts = now_iso()
        with wlock:                                   # writes serialized on one conn
            for lg in logs:
                acct.log_call(control, product_id=row["id"], prompt_version=PROMPT_VERSION, **lg)
            if ok:
                content = {**out, "_meta": {"basis": f"generated:llm:{model_name}:prompt_{PROMPT_VERSION}",
                                            "image": "used" if image else ("none" if not image_url else "unfetched"),
                                            "at": ts}}
                control.execute("UPDATE products SET llm_content=?, llm_hash=?, llm_status='enriched', "
                                "llm_enriched_at=? WHERE id=?", (json.dumps(content), h, ts, row["id"]))
            else:
                control.execute("UPDATE products SET llm_hash=?, llm_status='enrich_failed', "
                                "llm_enriched_at=? WHERE id=?", (h, ts, row["id"]))
            control.commit()
            totals["enriched" if ok else "failed"] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            if budget_inr is not None and acct.spend_since(control, 1) >= budget_inr:
                break
            rows = control.execute(f"SELECT {_SELECT_COLS} FROM products WHERE {_NEEDS_ENRICH} "
                                   "ORDER BY id LIMIT ?", (f"{PROMPT_VERSION}:%", chunk)).fetchall()
            if not rows:
                break
            list(pool.map(task, rows))
    remaining = control.execute(f"SELECT COUNT(*) FROM products WHERE {_NEEDS_ENRICH}",
                                (f"{PROMPT_VERSION}:%",)).fetchone()[0]
    out = {**totals, "spend_inr": acct.spend_since(control, 1), "remaining": remaining}
    control.close()
    return out


# Shared generationConfig for every Gemini call (realtime + batch).
# thinkingBudget=0 turns OFF chain-of-thought: this is deterministic structured
# extraction, not reasoning — thinking added ~10k tokens/call (billed at the OUTPUT
# rate) for no quality gain and drove the true cost ~14x over our metered estimate.
GEN_CONFIG = {"responseMimeType": "application/json",
              "thinkingConfig": {"thinkingBudget": 0}}


def usage_tokens(um: dict) -> dict:
    """Billed token counts from a Gemini usageMetadata block. Thinking tokens bill at
    the OUTPUT rate but arrive in thoughtsTokenCount — NOT candidatesTokenCount — so
    they must be folded into output or the ledger silently undercounts the real bill."""
    return {"input_tokens": um.get("promptTokenCount", 0),
            "output_tokens": um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)}


def gemini_client(model: str = "gemini-flash-latest"):
    """Default live client (realtime Gemini). Requires GEMINI_API_KEY.
    Auth via the x-goog-api-key header (works for both key formats).
    Returns a callable(prompt, image_url) -> {output, usage}."""
    import os

    def _call(prompt: str, image_jpeg: bytes | None) -> dict:
        import time

        from curl_cffi import requests

        from . import image_prep
        key = os.environ["GEMINI_API_KEY"]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        parts = [{"text": prompt}]
        if image_jpeg:
            parts.append(image_prep.as_inline_data(image_jpeg))     # the image the model sees
        payload = {"contents": [{"parts": parts}], "generationConfig": GEN_CONFIG}
        last = ""
        for i in range(4):                             # backoff-retry transient errors
            r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                              json=payload, timeout=60)
            body = r.json()
            if r.status_code == 200 and "candidates" in body:
                um = body.get("usageMetadata") or {}
                return {"output": extract_json(body["candidates"][0]["content"]["parts"][0]["text"]),
                        "usage": usage_tokens(um)}
            last = f"gemini {r.status_code}: {(body.get('error') or {}).get('status', '')}"
            if r.status_code in (429, 500, 503) and i < 3:
                time.sleep(1.5 * (2 ** i))             # 1.5s, 3s, 6s
                continue
            raise RuntimeError(last)                    # non-transient -> fail now
        raise RuntimeError(last or "gemini retries exhausted")

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
