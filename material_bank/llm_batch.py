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
    GEN_CONFIG,
    MODEL,
    PROMPT_VERSION,
    _INPUT_FIELDS,
    build_prompt,
    extract_json,
    novelty_hash,
    reject_suffix,
    sanitize,
    serialise,
    usage_tokens,
    verify,
)


def _prep_image(url):
    from .image_prep import prepare_image
    return prepare_image(url) if url else None


def build_batch_request(row, *, prepare=_prep_image, feedback=None) -> dict:
    """One batch line: {key, request} with the ≤384px image attached. key =
    product id (routes the result back). ``feedback`` (a prior rejection reason)
    appends the recovery-sweep suffix so the model fixes exactly that."""
    from .image_prep import as_inline_data
    input_text, _fmap, image_url = serialise(row)
    image = prepare(image_url)
    prompt = build_prompt(input_text, bool(image))
    if feedback:
        prompt += reject_suffix(feedback)
    parts = [{"text": prompt}]
    if image:
        parts.append(as_inline_data(image))
    return {
        "key": str(row["id"]),
        "request": {"contents": [{"parts": parts}], "generationConfig": GEN_CONFIG},
    }


# Value-first drain order: highest completeness (category-aware, feeds the publish
# gate) drains first. completeness is 100% populated, and the priced ~78% of the
# catalog IS the completeness-70+ band — so this puts priced, near-publishable,
# most-sellable records first for free (single-column sort, no join). The spend is
# then interruptible with maximum value captured: stop at any budget and you've
# enriched the most important records, not a random slice. id breaks ties (stable).
_VALUE_ORDER = "COALESCE(completeness,0) DESC, id"


def _select(conn: sqlite3.Connection, limit: int):
    return conn.execute(
        f"SELECT id, {', '.join(_INPUT_FIELDS)}, image_url, llm_hash "
        "FROM products WHERE (llm_status IS NULL OR llm_status='stale' "
        f"OR llm_hash NOT LIKE ?) AND title IS NOT NULL "
        f"ORDER BY {_VALUE_ORDER} LIMIT ?", (f"{PROMPT_VERSION}:%", limit)).fetchall()


def submit_batch(conn: sqlite3.Connection, *, transport, limit: int = 5000,
                 prepare=_prep_image, model_name: str = MODEL,
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
    job_name = transport.submit(requests)          # side effect on Google BEFORE we record it
    ts = now_iso()
    marks = [(novelty_hash(*serialise(r)[::2]), ts, r["id"]) for r in rows]
    # The job now exists on Google but not locally: if this write is lost the job is
    # ORPHANED (paid, uncollectable, its rows re-submittable). busy_timeout can't help a
    # WAL snapshot deadlock (immediate SQLITE_BUSY) — only a rollback + fresh snapshot can,
    # so retry hard. adopt_orphans() is the backstop if it still fails.
    _record_submission(conn, job_name, model_name, len(rows), ts, marks)
    return {"job_name": job_name, "count": len(rows)}


def _run_locked(conn, fn, *, tries=6):
    """Run a write-then-commit closure, retrying on 'database is locked'. The tick
    shares catalog.db with mb-embed/mb-harvest; busy_timeout can't clear a WAL
    snapshot deadlock (it returns SQLITE_BUSY immediately) — only rollback + a fresh
    read can. ``fn`` MUST be idempotent under rollback (it re-runs from a clean state),
    which is why each caller commits exactly once at the end of ``fn``."""
    import time
    for attempt in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == tries - 1:
                raise
            try:
                conn.rollback()                    # drop the stale snapshot, then re-run fresh
            except sqlite3.OperationalError:
                pass
            time.sleep(0.4 * (attempt + 1))


def _record_submission(conn, job_name, model_name, count, ts, marks):
    def _apply():
        conn.execute("INSERT INTO llm_batch_jobs (job_name, model, prompt_version, "
                     "product_count, status, submitted_at) VALUES (?,?,?,?,?,?)",
                     (job_name, model_name, PROMPT_VERSION, count, "submitted", ts))
        conn.executemany("UPDATE products SET llm_status='batched', llm_hash=?, "
                         "llm_enriched_at=? WHERE id=?", marks)
        conn.commit()
    _run_locked(conn, _apply)


def submit_all(conn: sqlite3.Connection, *, transport, chunk: int = 5000,
               max_products: int | None = None, prepare=_prep_image,
               model_name: str = MODEL, workers: int = 24) -> dict:
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
            prepare=_prep_image, model_name: str = MODEL,
            workers: int = 24, max_submit_per_tick: int = 60,
            budget_inr: float | None = None) -> dict:
    """One autonomous tick of the batch flywheel, self-pacing against Gemini's
    batch *enqueued-token* quota:

      1. collect finished jobs (writes results AND frees enqueued quota), then
      2. submit fresh chunks until the catalog is exhausted OR the quota pushes
         back with a 429 — which just ends this tick; the next one retries after
         more jobs have drained.

    ``submit_batch`` calls the wire ``submit()`` before it records/marks anything,
    so a 429'd chunk leaves state untouched and its rows stay eligible. Safe to
    run on a short timer until the whole catalog is enriched — no live process to
    babysit, fully resumable.

    ``budget_inr`` is a HARD cumulative-spend cap (the enforcement layer we own —
    Google's budget *alerts* only notify). When all-time ledger spend reaches it,
    submission halts; collection still runs so in-flight work is never stranded.
    The ledger is only trustworthy as a cap once thinking tokens are counted
    (see llm_enrich.usage_tokens) — the incident that made this cap necessary."""
    from . import llm_accounting as acct
    adopt = adopt_orphans(conn, transport=transport, model_name=model_name)
    ingest = collect_pending(conn, transport=transport, model_name=model_name)
    submitted, submit_error, budget_halted = 0, None, False
    for _ in range(max_submit_per_tick):
        if budget_inr is not None and acct.spend_total(conn) >= budget_inr:
            budget_halted = True
            submit_error = f"budget cap ₹{budget_inr:.0f} reached (ledger ₹{acct.spend_total(conn):.0f})"
            break
        try:
            out = submit_batch(conn, transport=transport, limit=chunk, prepare=prepare,
                               model_name=model_name, workers=workers)
        except (RuntimeError, sqlite3.OperationalError) as e:
            # transport.submit raises RuntimeError on an HTTP non-200 (quota 429,
            # billing 403, transient 5xx); a write can raise OperationalError on a DB
            # lock. NEITHER may crash the tick: collection + adoption already ran, the
            # block self-clears, and adopt_orphans recovers any job left on Google by a
            # lost write. Record it, stop submitting, exit clean — next tick resumes.
            submit_error = str(e)
            break
        if not out["count"]:
            break                           # catalog exhausted
        submitted += 1
        import gc
        gc.collect()                        # release each chunk's decoded-image + request buffers
    remaining = conn.execute(
        "SELECT COUNT(*) FROM products WHERE (llm_status IS NULL OR llm_status='stale' "
        "OR llm_hash NOT LIKE ?) AND title IS NOT NULL", (f"{PROMPT_VERSION}:%",)).fetchone()[0]
    return {**adopt, **ingest, "jobs_submitted": submitted, "remaining_unbatched": remaining,
            "submit_error": submit_error, "budget_halted": budget_halted}


def adopt_orphans(conn: sqlite3.Connection, *, transport, model_name: str = MODEL) -> dict:
    """Self-heal orphaned jobs. A batch is created on Google BEFORE its local row is
    written (submit() must return the id first), so a crash/lock in between leaves a
    paid job on the provider that we have no record of — uncollectable, its products
    stuck 'batched' or silently re-submitted. Provider's batch list is the durable
    source of truth: record any job we don't have so collect_pending ingests it.
    No-op if the transport can't list. Idempotent (job_name is UNIQUE)."""
    lister = getattr(transport, "list_batches", None)
    if lister is None:
        return {"orphans_adopted": 0}
    recorded = {r[0] for r in conn.execute("SELECT job_name FROM llm_batch_jobs")}
    to_add = [name for name, _state, _done in lister() if name and name not in recorded]

    def _apply():
        for name in to_add:
            conn.execute("INSERT OR IGNORE INTO llm_batch_jobs (job_name, model, prompt_version, "
                         "status, submitted_at) VALUES (?,?,?,?,?)",
                         (name, model_name, PROMPT_VERSION, "submitted", now_iso()))
        conn.commit()
        return len(to_add)
    return {"orphans_adopted": _run_locked(conn, _apply)}


def sweep_failed(conn: sqlite3.Connection, *, transport, prepare=_prep_image,
                 model_name: str = MODEL, workers: int = 8, chunk: int = 500,
                 max_products: int | None = None) -> dict:
    """End-of-drain recovery: re-submit enrich_failed products ONCE with their last
    rejection reason fed back into the prompt (the realtime feedback-retry pattern,
    batched). Reuses submit + collect machinery: it marks the rows 'batched', and the
    normal collect path re-verifies — a swept product that now passes flips to
    'enriched', one that fails again returns to 'enrich_failed' (so this is safely
    re-runnable and self-limiting: in-flight rows aren't re-selected)."""
    cols = ", ".join("p." + f for f in _INPUT_FIELDS)
    submitted, jobs, err = 0, 0, None
    while True:
        rows = conn.execute(
            f"SELECT p.id, {cols}, p.image_url, "
            "  (SELECT fail_reason FROM llm_calls WHERE product_id=p.id "
            "   AND fail_reason IS NOT NULL ORDER BY id DESC LIMIT 1) AS feedback "
            "FROM products p WHERE p.llm_status='enrich_failed' AND p.title IS NOT NULL "
            f"ORDER BY {_VALUE_ORDER} LIMIT ?", (chunk,)).fetchall()
        if not rows:
            break
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as pool:
                reqs = list(pool.map(
                    lambda r: build_batch_request(r, prepare=prepare, feedback=r["feedback"]), rows))
        else:
            reqs = [build_batch_request(r, prepare=prepare, feedback=r["feedback"]) for r in rows]
        try:
            job_name = transport.submit(reqs)
        except (RuntimeError, sqlite3.OperationalError) as e:
            err = str(e)                            # quota/billing/lock — stop, resume on a re-run
            break
        ts = now_iso()
        marks = [(novelty_hash(*serialise(r)[::2]), ts, r["id"]) for r in rows]
        _record_submission(conn, job_name, model_name, len(rows), ts, marks)
        submitted += len(rows)
        jobs += 1
        import gc
        gc.collect()
        if max_products and submitted >= max_products:
            break
    return {"products": submitted, "jobs": jobs, "submit_error": err}


def collect_pending(conn: sqlite3.Connection, *, transport,
                    model_name: str = MODEL) -> dict:
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
        try:
            collect_batch(conn, j["job_name"], transport=_Given(results), model_name=model_name)
        except sqlite3.OperationalError:               # lock persisted past all retries
            conn.rollback()                            # leave the job 'submitted' — next tick retries
            still += 1
            continue
        ingested += 1                              # collect_batch marks 'ingested' atomically
    return {"jobs_ingested": ingested, "jobs_pending": still}


class _Given:
    """Adapt an already-fetched results list to the transport interface."""
    def __init__(self, results): self._results = results
    def results(self, job_name): return self._results


def collect_batch(conn: sqlite3.Connection, job_name: str, *, transport,
                  model_name: str = MODEL) -> dict:
    """Ingest a finished batch: verify each result, write llm_content (pass) or
    mark enrich_failed (reject). Re-serialises each product to verify against its
    own field map — the verifier is the same one the realtime path uses."""
    from . import llm_accounting as acct

    results = transport.results(job_name)          # [{key, text, usage}|{key, error}]
    ts = now_iso()
    # PHASE 1 — compute (reads + verify only, no writes, no lock risk). Build a plan
    # of ledger + product-update ops so PHASE 2 can be a single atomic, retriable
    # transaction. Keeping verify/sanitize out of the retry means a lock never re-runs
    # the expensive work.
    plan, missing = [], 0
    for res in results:
        pid = res.get("key")
        row = conn.execute(
            f"SELECT id, {', '.join(_INPUT_FIELDS)}, image_url FROM products WHERE id=?",
            (pid,)).fetchone()
        if row is None:
            missing += 1
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
        ledger_kw = dict(product_id=pid, model=res.get("model_version") or model_name,
                         phase="batch", attempt=0, input_tokens=usage.get("input_tokens", 0),
                         output_tokens=usage.get("output_tokens", 0), status=status,
                         fail_reason=reason, batch=True, prompt_version=PROMPT_VERSION,
                         batch_job=job_name)
        if status == "enriched":
            content = {**out, "_meta": {"basis": f"generated:llm:{model_name}:prompt_{PROMPT_VERSION}",
                                        "at": ts}}
            upd = ("UPDATE products SET llm_content=?, llm_hash=?, llm_status='enriched', "
                   "llm_enriched_at=? WHERE id=?", (json.dumps(content), h, ts, pid))
            bucket = "enriched"
        else:
            upd = ("UPDATE products SET llm_hash=?, llm_status='enrich_failed', "
                   "llm_enriched_at=? WHERE id=?", (h, ts, pid))
            bucket = "failed"
        plan.append((ledger_kw, upd, bucket))

    # PHASE 2 — apply everything for this job in ONE transaction: ledger rows + product
    # updates + mark the job 'ingested'. Atomic so a lock mid-way rolls the WHOLE job
    # back and _run_locked retries it — never a partial write, and never a re-collect
    # that double-logs spend (the job flips to 'ingested' in the same commit).
    def _apply():
        stats = {"results": len(results), "enriched": 0, "failed": 0, "missing": missing,
                 "spend_inr": 0.0}
        for ledger_kw, (sql, params), bucket in plan:
            stats["spend_inr"] += acct.log_call(conn, **ledger_kw)
            conn.execute(sql, params)
            stats[bucket] += 1
        stats["spend_inr"] = round(stats["spend_inr"], 4)
        conn.execute("UPDATE llm_batch_jobs SET status='ingested', ingested_at=?, result=? "
                     "WHERE job_name=?", (now_iso(), json.dumps(stats), job_name))
        conn.commit()
        return stats
    stats = _run_locked(conn, _apply)
    return stats


class GeminiBatchTransport:
    """Live Gemini batch transport over REST (inlined-requests form).

    NOT YET LIVE-VALIDATED — the exact field names of Google's batch response are
    reconstructed from docs and must be confirmed with ONE real job before the
    full 160k pass (a batch is async + 24h SLA, so it can't be smoke-tested under
    an exhausted quota). The orchestration that uses it is fully tested via a
    fake transport; only this class's two methods touch the unverified wire shape.
    """

    def __init__(self, model: str = MODEL):
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

    def list_batches(self) -> list[tuple]:
        """Every batch on the account, paged — (name, state, done). The durable
        record adopt_orphans reconciles the local job table against."""
        from curl_cffi import requests as http
        out, page = [], None
        for _ in range(50):                             # page guard
            url = f"{self.base}/batches?pageSize=50" + (f"&pageToken={page}" if page else "")
            b = http.get(url, headers=self._headers(), timeout=60).json()
            # the list endpoint returns items under "operations" (verified live 2026-07),
            # NOT "batches" — tolerate both so a future rename can't silently blind adoption.
            for op in (b.get("operations") or b.get("batches") or []):
                out.append((op.get("name"), (op.get("metadata") or {}).get("state"), op.get("done")))
            page = b.get("nextPageToken")
            if not page:
                break
        return out

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
                out.append({"key": key, "text": txt,
                            "usage": usage_tokens(resp.get("usageMetadata") or {}),
                            "model_version": resp.get("modelVersion")})
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
    ap.add_argument("--sweep", action="store_true",
                    help="end-of-drain recovery: re-submit enrich_failed with rejection reasons fed back")
    ap.add_argument("--advance", action="store_true",
                    help="one self-pacing tick: collect finished + submit until quota/exhausted (the timer entrypoint)")
    ap.add_argument("--chunk", type=int, default=5000)
    ap.add_argument("--workers", type=int, default=24, help="parallel image-prep threads")
    ap.add_argument("--max", type=int, default=None, help="cap products for --submit-all")
    ap.add_argument("--budget-inr", type=float, default=None,
                    help="hard cumulative ₹ spend cap for --advance (halts submission when reached)")
    ap.add_argument("--max-submit", type=int, default=60,
                    help="chunks to submit per --advance process before exiting (bounds peak "
                         "RAM: image prep accumulates, so a long-lived process OOMs — exit and "
                         "let the timer's next tick resume)")
    ap.add_argument("--model", default=MODEL)
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
                      workers=args.workers, budget_inr=args.budget_inr,
                      max_submit_per_tick=args.max_submit)
    elif args.collect:
        out = collect_pending(conn, transport=transport, model_name=args.model)
    elif args.sweep:
        out = sweep_failed(conn, transport=transport, chunk=args.chunk, model_name=args.model,
                           workers=args.workers, max_products=args.max)
    else:
        ap.error("one of --submit-all / --submit / --collect / --sweep / --advance required")
    print(json.dumps(out), file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
