"""Repair execution for drift-opened jobs (Stage 9).

Deterministic first line: re-probe the domain. A site that changed CMS often
just needs reclassification — cheap and safe. If re-probing still can't classify
it, the job is raised (→ retry → dead-letter), escalating to the LLM parser-
repair slot with the saved raw fixtures as the external check. We never silently
"fix" by guessing.
"""

from __future__ import annotations

import sqlite3

from . import db, jobs
from .fetch import Fetcher
from .probe import classify, write_result

REPAIR_STAGE = "repair"


def run_repair(conn: sqlite3.Connection, domain: str) -> dict:
    """Re-probe and persist. Raises if still unclassified (escalate to LLM)."""
    before = conn.execute("SELECT scrape_tier FROM suppliers WHERE domain=?", (domain,)).fetchone()
    result = classify(domain, Fetcher(raw_dir=None))
    write_result(conn, result)
    new_tier = result.scrape_tier.value if result.scrape_tier else None
    if new_tier is None:
        raise RuntimeError(
            f"re-probe could not classify {domain} ({result.probe_status.value}); "
            f"needs LLM parser-repair on saved fixtures")
    return {"domain": domain, "old_tier": before["scrape_tier"] if before else None,
            "new_tier": new_tier, "reclassified": (before and before["scrape_tier"] != new_tier)}


def drain_repairs(db_path=None, *, on_job=None) -> dict:
    """Process all pending repair jobs (single worker; repairs are rare)."""
    db_path = str(db_path or db.DEFAULT_DB_PATH)
    conn = db.connect(db_path, check_same_thread=False)
    jobs.requeue_stale_running(conn)
    while True:
        job = jobs.claim(conn, REPAIR_STAGE)
        if job is None:
            break
        try:
            result = run_repair(conn, job["target"])
            jobs.complete(conn, job["id"], result)
            status = "done"
        except Exception as exc:
            status = jobs.fail(conn, job["id"], f"{type(exc).__name__}: {exc}")
            result = {"error": str(exc)}
        if on_job:
            on_job(job["target"], status, result)
    out = jobs.counts(conn, REPAIR_STAGE)
    conn.close()
    return out
