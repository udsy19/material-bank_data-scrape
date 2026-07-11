"""Gemini batch pipeline for LLM enrichment (the production path).

The realtime client (``llm_enrich.gemini_client``) is right for the eval/canary;
the free-tier RPM/RPD ceiling makes it useless for the 160k pass. Batch mode is
~50% cheaper with a 24h SLA — a natural fit for an async flywheel. Two phases,
both idempotent:

  submit_batch  — select novelty-gated products, build one request each, hand
                  them to the transport, mark the rows 'batched' so they aren't
                  resubmitted.
  collect_batch — pull the finished job's results, run the SAME deterministic
                  verifiers (llm_enrich.verify) on each, and write llm_content
                  (pass) or enrich_failed (reject). Generated content stays BONUS.

The transport (submit/results) is INJECTED, so the whole orchestration —
request building, result ingestion, verification, survivorship — is tested
offline with a fake. ``GeminiBatchTransport`` is the live REST implementation;
it needs ONE live validation run before the full pass (it cannot be exercised
under an exhausted quota), and is marked as such.
"""

from __future__ import annotations

import json
import os
import sqlite3

from .db import now_iso
from .llm_enrich import (
    PROMPT_VERSION,
    _INPUT_FIELDS,
    build_prompt,
    extract_json,
    novelty_hash,
    sanitize,
    serialise,
    verify,
)


def _prep_image(url):
    from .image_prep import prepare_image
    return prepare_image(url) if url else None


def build_batch_request(row, *, prepare=_prep_image) -> dict:
    """One batch line: {key, request} with the ≤384px image attached. key =
    product id (routes the result back)."""
    from .image_prep import as_inline_data
    input_text, _fmap, image_url = serialise(row)
    image = prepare(image_url)
    parts = [{"text": build_prompt(input_text, bool(image))}]
    if image:
        parts.append(as_inline_data(image))
    return {
        "key": str(row["id"]),
        "request": {"contents": [{"parts": parts}],
                    "generationConfig": {"responseMimeType": "application/json"}},
    }


def _select(conn: sqlite3.Connection, limit: int):
    return conn.execute(
        f"SELECT id, {', '.join(_INPUT_FIELDS)}, image_url, llm_hash "
        "FROM products WHERE (llm_status IS NULL OR llm_status='stale' "
        "OR llm_hash NOT LIKE ?) AND title IS NOT NULL "
        "ORDER BY id LIMIT ?", (f"{PROMPT_VERSION}:%", limit)).fetchall()


def submit_batch(conn: sqlite3.Connection, *, transport, limit: int = 5000,
                 prepare=_prep_image, model_name: str = "gemini-flash-latest",
                 workers: int = 24) -> dict:
    """Build requests for novelty-gated products and submit ONE batch job. Image
    prep (fetch+resize) runs across ``workers`` threads — it's the long pole. Records
    the job in llm_batch_jobs (first-class, resumable) and marks its products
    'batched' (version-stamped) so a resubmit never duplicates them."""
    rows = _select(conn, limit)
    if not rows:
        return {"job_name": None, "count": 0}
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            requests = list(pool.map(lambda r: build_batch_request(r, prepare=prepare), rows))
    else:
        requests = [build_batch_request(r, prepare=prepare) for r in rows]
    job_name = transport.submit(requests)
    ts = now_iso()
    conn.execute("INSERT INTO llm_batch_jobs (job_name, model, prompt_version, "
                 "product_count, status, submitted_at) VALUES (?,?,?,?,?,?)",
                 (job_name, model_name, PROMPT_VERSION, len(rows), "submitted", ts))
    conn.executemany(
        "UPDATE products SET llm_status='batched', llm_hash=?, llm_enriched_at=? WHERE id=?",
        [(novelty_hash(*serialise(r)[::2]), ts, r["id"]) for r in rows])
    conn.commit()
    return {"job_name": job_name, "count": len(rows)}


def submit_all(conn: sqlite3.Connection, *, transport, chunk: int = 5000,
               max_products: int | None = None, prepare=_prep_image,
               model_name: str = "gemini-flash-latest", workers: int = 24) -> dict:
    """Submit the whole un-enriched catalog as chunked batch jobs (parallel image
    prep). Resumable — already-batched products are skipped, so a re-run only
    submits the remainder."""
    jobs, total = [], 0
    while max_products is None or total < max_products:
        n = chunk if max_products is None else min(chunk, max_products - total)
        out = submit_batch(conn, transport=transport, limit=n, prepare=prepare,
                           model_name=model_name, workers=workers)
        if not out["count"]:
            break
        jobs.append(out["job_name"]); total += out["count"]
    return {"jobs": len(jobs), "products": total, "job_names": jobs}


def advance(conn: sqlite3.Connection, *, transport, chunk: int = 1000,
            prepare=_prep_image, model_name: str = "gemini-flash-latest",
            workers: int = 24, max_submit_per_tick: int = 60) -> dict:
    """One autonomous tick of the batch flywheel, self-pacing against Gemini's
    batch *enqueued-token* quota:

      1. collect finished jobs (writes results AND frees enqueued quota), then
      2. submit fresh chunks until the catalog is exhausted OR the quota pushes
         back with a 429 — which just ends this tick; the next one retries after
         more jobs have drained.

    ``submit_batch`` calls the wire ``submit()`` before it records/marks anything,
    so a 429'd chunk leaves state untouched and its rows stay eligible. Safe to
    run on a short timer until the whole catalog is enriched — no live process to
    babysit, fully resumable."""
    ingest = collect_pending(conn, transport=transport, model_name=model_name)
    submitted, submit_error = 0, None
    for _ in range(max_submit_per_tick):
        try:
            out = submit_batch(conn, transport=transport, limit=chunk, prepare=prepare,
                               model_name=model_name, workers=workers)
        except RuntimeError as e:
            # transport.submit only raises on an HTTP non-200 (quota 429, billing/perm
            # 403, transient 5xx). NONE should crash the tick: collection already ran,
            # and a submit block clears itself (quota drains, billing gets fixed) — so
            # the next tick auto-resumes. Record it, stop submitting, exit clean.
            submit_error = str(e)
            break
        if not out["count"]:
            break                           # catalog exhausted
        submitted += 1
    remaining = conn.execute(
        "SELECT COUNT(*) FROM products WHERE (llm_status IS NULL OR llm_status='stale' "
        "OR llm_hash NOT LIKE ?) AND title IS NOT NULL", (f"{PROMPT_VERSION}:%",)).fetchone()[0]
    return {**ingest, "jobs_submitted": submitted, "remaining_unbatched": remaining,
            "submit_error": submit_error}


def collect_pending(conn: sqlite3.Connection, *, transport,
                    model_name: str = "gemini-flash-latest") -> dict:
    """Poll every still-submitted job; ingest the finished ones (verify + write +
    ledger), mark them 'ingested'. Safe to run repeatedly on a timer until dry."""
    pending = conn.execute("SELECT job_name FROM llm_batch_jobs WHERE status='submitted'").fetchall()
    ingested, still = 0, 0
    for j in pending:
        try:
            results = transport.results(j["job_name"])   # raises if not done yet
        except Exception:
            still += 1
            continue
        stats = collect_batch(conn, j["job_name"], transport=_Given(results), model_name=model_name)
        conn.execute("UPDATE llm_batch_jobs SET status='ingested', ingested_at=?, result=? "
                     "WHERE job_name=?", (now_iso(), json.dumps(stats), j["job_name"]))
        conn.commit()
        ingested += 1
    return {"jobs_ingested": ingested, "jobs_pending": still}


class _Given:
    """Adapt an already-fetched results list to the transport interface."""
    def __init__(self, results): self._results = results
    def results(self, job_name): return self._results


def collect_batch(conn: sqlite3.Connection, job_name: str, *, transport,
                  model_name: str = "gemini-2.5-flash") -> dict:
    """Ingest a finished batch: verify each result, write llm_content (pass) or
    mark enrich_failed (reject). Re-serialises each product to verify against its
    own field map — the verifier is the same one the realtime path uses."""
    from . import llm_accounting as acct

    results = transport.results(job_name)          # [{key, text, usage}|{key, error}]
    stats = {"results": len(results), "enriched": 0, "failed": 0, "missing": 0, "spend_inr": 0.0}
    ts = now_iso()
    for res in results:
        pid = res.get("key")
        row = conn.execute(
            f"SELECT id, {', '.join(_INPUT_FIELDS)}, image_url FROM products WHERE id=?",
            (pid,)).fetchone()
        if row is None:
            stats["missing"] += 1
            continue
        input_text, fmap, image_url = serialise(row)
        h = novelty_hash(input_text, image_url)
        usage = res.get("usage") or {}
        out = None
        if res.get("text"):
            try:
                out = extract_json(res["text"])            # same robust parser as the realtime path
            except (ValueError, TypeError):
                out = None
        if out is None:
            status, reason = "api_error", str(res.get("error") or "no/invalid response text")
        else:
            out = sanitize(out, fmap, input_text)      # deterministic tags (match realtime path)
            fails = verify(out, fmap, input_text)
            status = "enriched" if not fails else "verifier_failed"
            reason = fails[0] if fails else None
        # ledger row per batch result (batch billed at 50%)
        stats["spend_inr"] += acct.log_call(
            conn, product_id=pid, model=model_name, phase="batch", attempt=0,
            input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
            status=status, fail_reason=reason, batch=True, prompt_version=PROMPT_VERSION,
            batch_job=job_name)
        if status == "enriched":
            content = {**out, "_meta": {"basis": f"generated:llm:{model_name}:prompt_{PROMPT_VERSION}",
                                        "at": ts}}
            conn.execute("UPDATE products SET llm_content=?, llm_hash=?, llm_status='enriched', "
                         "llm_enriched_at=? WHERE id=?", (json.dumps(content), h, ts, pid))
            stats["enriched"] += 1
        else:
            conn.execute("UPDATE products SET llm_hash=?, llm_status='enrich_failed', "
                         "llm_enriched_at=? WHERE id=?", (h, ts, pid))
            stats["failed"] += 1
    conn.commit()
    stats["spend_inr"] = round(stats["spend_inr"], 4)
    return stats


class GeminiBatchTransport:
    """Live Gemini batch transport over REST (inlined-requests form).

    NOT YET LIVE-VALIDATED — the exact field names of Google's batch response are
    reconstructed from docs and must be confirmed with ONE real job before the
    full 160k pass (a batch is async + 24h SLA, so it can't be smoke-tested under
    an exhausted quota). The orchestration that uses it is fully tested via a
    fake transport; only this class's two methods touch the unverified wire shape.
    """

    def __init__(self, model: str = "gemini-flash-latest"):
        self.model = model
        self.base = "https://generativelanguage.googleapis.com/v1beta"

    def _headers(self):
        return {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"}

    def submit(self, requests: list[dict]) -> str:
        from curl_cffi import requests as http
        inlined = [{"request": r["request"], "metadata": {"key": r["key"]}} for r in requests]
        r = http.post(f"{self.base}/models/{self.model}:batchGenerateContent",
                      headers=self._headers(),
                      json={"batch": {"display_name": "mb-enrich",
                                      "input_config": {"requests": {"requests": inlined}}}},
                      timeout=120)
        body = r.json()
        if r.status_code != 200 or "name" not in body:
            raise RuntimeError(f"batch submit {r.status_code}: {body}")
        return body["name"]                             # operation name to poll

    def results(self, job_name: str) -> list[dict]:
        from curl_cffi import requests as http
        r = http.get(f"{self.base}/{job_name}", headers=self._headers(), timeout=60)
        op = r.json()
        if not op.get("done"):
            raise RuntimeError(f"batch {job_name} not done yet")
        out = []
        inlined = (((op.get("response") or {}).get("inlinedResponses") or {})
                   .get("inlinedResponses") or [])
        for item in inlined:
            key = (item.get("metadata") or {}).get("key")
            try:
                resp = item["response"]
                txt = resp["candidates"][0]["content"]["parts"][0]["text"]
                um = resp.get("usageMetadata") or {}
                out.append({"key": key, "text": txt,
                            "usage": {"input_tokens": um.get("promptTokenCount", 0),
                                      "output_tokens": um.get("candidatesTokenCount", 0)}})
            except (KeyError, IndexError, TypeError):
                out.append({"key": key, "error": item.get("error") or "no candidates"})
        return out


def main(argv=None) -> int:
    import argparse
    import sys

    from . import db

    ap = argparse.ArgumentParser(prog="mb-llm-batch")
    ap.add_argument("--submit-all", action="store_true", help="submit the whole catalog in chunks")
    ap.add_argument("--submit", action="store_true", help="submit one chunk")
    ap.add_argument("--collect", action="store_true", help="poll + ingest all finished jobs")
    ap.add_argument("--advance", action="store_true",
                    help="one self-pacing tick: collect finished + submit until quota/exhausted (the timer entrypoint)")
    ap.add_argument("--chunk", type=int, default=5000)
    ap.add_argument("--workers", type=int, default=24, help="parallel image-prep threads")
    ap.add_argument("--max", type=int, default=None, help="cap products for --submit-all")
    ap.add_argument("--model", default="gemini-flash-latest")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    transport = GeminiBatchTransport(args.model)
    if args.submit_all:
        out = submit_all(conn, transport=transport, chunk=args.chunk, max_products=args.max,
                         model_name=args.model, workers=args.workers)
    elif args.submit:
        out = submit_batch(conn, transport=transport, limit=args.chunk, model_name=args.model)
    elif args.advance:
        out = advance(conn, transport=transport, chunk=args.chunk, model_name=args.model,
                      workers=args.workers)
    elif args.collect:
        out = collect_pending(conn, transport=transport, model_name=args.model)
    else:
        ap.error("one of --submit-all / --submit / --collect required")
    print(json.dumps(out), file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
