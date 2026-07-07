"""Durable job queue with retry — the pipeline's orchestration spine.

One row per (stage, target). Workers claim atomically (only one wins a job),
run it, then either ``complete`` it or ``fail`` it. A failed job is retried with
exponential backoff until ``max_attempts``, after which it dead-letters to
status='failed' with its last error preserved — nothing is silently lost.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .db import now_iso

BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 3600
STALE_RUNNING_S = 1800  # a 'running' job older than this is presumed crashed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(conn: sqlite3.Connection, stage: str, target: str, *,
            priority: int = 0, max_attempts: int = 4, reset: bool = False) -> None:
    """Add a job (idempotent per stage+target). ``reset`` re-arms a done/failed
    job back to pending for a fresh attempt cycle."""
    ts = now_iso()
    if reset:
        conn.execute(
            "INSERT INTO pipeline_jobs (stage,target,status,attempts,max_attempts,priority,"
            "next_run_at,created_at,updated_at) VALUES (?,?,'pending',0,?,?,?,?,?) "
            "ON CONFLICT(stage,target) DO UPDATE SET status='pending', attempts=0, "
            "last_error=NULL, next_run_at=excluded.next_run_at, max_attempts=excluded.max_attempts, "
            "priority=excluded.priority, updated_at=excluded.updated_at",
            (stage, target, max_attempts, priority, ts, ts, ts))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO pipeline_jobs (stage,target,status,attempts,max_attempts,"
            "priority,next_run_at,created_at,updated_at) VALUES (?,?,'pending',0,?,?,?,?,?)",
            (stage, target, max_attempts, priority, ts, ts, ts))
    conn.commit()


def enqueue_many(conn, stage, targets, *, reset=False, **kw) -> int:
    for t in targets:
        enqueue(conn, stage, t, reset=reset, **kw)
    return len(list(targets)) if not hasattr(targets, "__len__") else len(targets)


def requeue_stale_running(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Reclaim jobs stuck 'running' past the stale window (worker crashed)."""
    cutoff = ((now or _now()) - timedelta(seconds=STALE_RUNNING_S)).isoformat()
    cur = conn.execute(
        "UPDATE pipeline_jobs SET status='pending', updated_at=? "
        "WHERE status='running' AND updated_at < ?", (now_iso(), cutoff))
    conn.commit()
    return cur.rowcount


def claim(conn: sqlite3.Connection, stage: str, *, now: datetime | None = None) -> sqlite3.Row | None:
    """Atomically claim one eligible pending job, or None. Safe under concurrency:
    two workers may read the same id, but only one UPDATE flips it to running."""
    now_s = (now or _now()).isoformat()
    for _ in range(8):  # brief contention retries
        row = conn.execute(
            "SELECT id FROM pipeline_jobs WHERE stage=? AND status='pending' "
            "AND (next_run_at IS NULL OR next_run_at <= ?) ORDER BY priority DESC, id LIMIT 1",
            (stage, now_s)).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE pipeline_jobs SET status='running', updated_at=? WHERE id=? AND status='pending'",
            (now_iso(), row[0]))
        conn.commit()
        if cur.rowcount == 1:
            return conn.execute("SELECT * FROM pipeline_jobs WHERE id=?", (row[0],)).fetchone()
    return None


def complete(conn: sqlite3.Connection, job_id: int, result: dict | None = None) -> None:
    conn.execute(
        "UPDATE pipeline_jobs SET status='done', result=?, last_error=NULL, updated_at=? WHERE id=?",
        (json.dumps(result or {}), now_iso(), job_id))
    conn.commit()


def fail(conn: sqlite3.Connection, job_id: int, error: str, *,
         base: int = BACKOFF_BASE_S, cap: int = BACKOFF_CAP_S,
         now: datetime | None = None) -> str:
    """Record a failure. Reschedules with exponential backoff, or dead-letters
    to 'failed' once attempts are exhausted. Returns the new status."""
    row = conn.execute("SELECT attempts, max_attempts FROM pipeline_jobs WHERE id=?",
                       (job_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    max_attempts = row["max_attempts"] if row else 1
    if attempts >= max_attempts:
        status, next_run = "failed", None
    else:
        delay = min(base * (2 ** (attempts - 1)), cap)
        status = "pending"
        next_run = ((now or _now()) + timedelta(seconds=delay)).isoformat()
    conn.execute(
        "UPDATE pipeline_jobs SET status=?, attempts=?, last_error=?, next_run_at=?, updated_at=? "
        "WHERE id=?", (status, attempts, (error or "")[:2000], next_run, now_iso(), job_id))
    conn.commit()
    return status


def enqueue_due_refreshes(conn: sqlite3.Connection, *, fast_days: float = 1,
                          jsonld_days: float = 7, slow_days: float = 30,
                          now: datetime | None = None) -> int:
    """Re-arm harvest jobs for suppliers due a refresh, on a TIER-AWARE cadence so
    the hourly sweep re-fetches only what's both stale and cheap:
      * shopify/woo (bulk /products.json)  -> every ``fast_days`` (default daily)
      * jsonld + priced (per-PDP, pricier) -> every ``jsonld_days`` (default weekly)
      * everything else / spec-only        -> every ``slow_days`` (default monthly)
    Re-fetching a 15k-PDP giant hourly would be impolite (ban risk) and pointless;
    a Shopify catalog is one cheap API call, so it can refresh daily.
    """
    ref = now or _now()
    fast_cut = (ref - timedelta(days=fast_days)).isoformat()
    jsonld_cut = (ref - timedelta(days=jsonld_days)).isoformat()
    slow_cut = (ref - timedelta(days=slow_days)).isoformat()
    due = conn.execute(
        """
        SELECT j.target FROM pipeline_jobs j
        JOIN suppliers s ON s.domain = j.target
        WHERE j.stage='harvest' AND j.status='done' AND s.last_harvest IS NOT NULL
          AND s.last_harvest < CASE
                WHEN s.scrape_tier IN ('shopify','woocommerce')          THEN ?
                WHEN s.scrape_tier = 'jsonld' AND s.price_published='yes' THEN ?
                ELSE ? END
        """, (fast_cut, jsonld_cut, slow_cut)).fetchall()
    for r in due:
        enqueue(conn, "harvest", r[0], reset=True)
    return len(due)


def retry_dead(conn: sqlite3.Connection, stage: str | None = None) -> int:
    """Re-arm dead-lettered (failed) jobs for another attempt cycle."""
    ts = now_iso()
    q = ("UPDATE pipeline_jobs SET status='pending', attempts=0, next_run_at=?, updated_at=? "
         "WHERE status='failed'")
    params = [ts, ts]
    if stage:
        q += " AND stage=?"
        params.append(stage)
    cur = conn.execute(q, params)
    conn.commit()
    return cur.rowcount


def counts(conn: sqlite3.Connection, stage: str | None = None) -> dict:
    q = "SELECT status, COUNT(*) n FROM pipeline_jobs"
    params: list = []
    if stage:
        q += " WHERE stage=?"
        params.append(stage)
    q += " GROUP BY status"
    out = {r["status"]: r["n"] for r in conn.execute(q, params)}
    for s in ("pending", "running", "done", "failed"):
        out.setdefault(s, 0)
    return out


def dead_letters(conn: sqlite3.Connection, stage: str | None = None) -> list[dict]:
    q = "SELECT stage, target, attempts, last_error FROM pipeline_jobs WHERE status='failed'"
    params: list = []
    if stage:
        q += " AND stage=?"
        params.append(stage)
    return [dict(r) for r in conn.execute(q + " ORDER BY updated_at DESC", params)]
