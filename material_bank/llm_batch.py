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
    novelty_hash,
    sanitize,
    serialise,
    verify,
)


def build_batch_request(row) -> dict:
    """One batch line: {key, request}. key = product id (routes the result back)."""
    input_text, _fmap, image_url = serialise(row)
    return {
        "key": str(row["id"]),
        "request": {
            "contents": [{"parts": [{"text": build_prompt(input_text, bool(image_url))}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
    }


def _select(conn: sqlite3.Connection, limit: int):
    return conn.execute(
        f"SELECT id, {', '.join(_INPUT_FIELDS)}, image_url, llm_hash "
        "FROM products WHERE (llm_status IS NULL OR llm_status='stale' "
        "OR llm_hash NOT LIKE ?) AND title IS NOT NULL "
        "ORDER BY id LIMIT ?", (f"{PROMPT_VERSION}:%", limit)).fetchall()


def submit_batch(conn: sqlite3.Connection, *, transport, limit: int = 5000) -> dict:
    """Build requests for novelty-gated products and submit one batch job.
    Marks submitted rows 'batched' (with the version-stamped hash) so a second
    submit doesn't duplicate them. Returns {job_name, count}."""
    rows = _select(conn, limit)
    if not rows:
        return {"job_name": None, "count": 0}
    requests = [build_batch_request(r) for r in rows]
    job_name = transport.submit(requests)
    ts = now_iso()
    conn.executemany(
        "UPDATE products SET llm_status='batched', llm_hash=?, llm_enriched_at=? WHERE id=?",
        [(novelty_hash(*serialise(r)[::2]), ts, r["id"]) for r in rows])
    conn.commit()
    return {"job_name": job_name, "count": len(rows)}


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
                out = json.loads(res["text"])
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
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--collect", metavar="JOB_NAME")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    transport = GeminiBatchTransport(args.model)
    if args.submit:
        print(json.dumps(submit_batch(conn, transport=transport, limit=args.limit)), file=sys.stderr)
    elif args.collect:
        print(json.dumps(collect_batch(conn, args.collect, transport=transport,
                                       model_name=args.model)), file=sys.stderr)
    else:
        ap.error("one of --submit / --collect required")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
