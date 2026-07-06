"""Yield-drift self-healing (Stage 9).

Parsers rot: a site redesign silently breaks extraction and a supplier's harvest
yield collapses while everything still "succeeds". We watch per-domain yield
across harvests and, when it drops past a threshold (or quarantine spikes),
auto-open a ``repair`` job — the one place an LLM agent clearly beats
deterministic code, with the saved raw fixtures as the external check.
"""

from __future__ import annotations

import sqlite3

from . import jobs

DRIFT_THRESHOLD = 0.30   # a >30% yield drop vs the prior harvest = suspicious
MIN_PREV = 20            # ignore tiny suppliers (noise)
QUARANTINE_SPIKE = 0.50  # >50% of attempts quarantined = parser likely broken
REPAIR_STAGE = "repair"


def _last_two(conn: sqlite3.Connection, domain: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT products, priced, quarantined FROM harvest_history WHERE domain=? "
        "ORDER BY observed_at DESC, id DESC LIMIT 2", (domain,)).fetchall()


def detect_drift(conn: sqlite3.Connection, *, threshold: float = DRIFT_THRESHOLD,
                 min_prev: int = MIN_PREV) -> list[dict]:
    """Domains whose latest yield collapsed vs the prior harvest."""
    drifted = []
    domains = [r[0] for r in conn.execute("SELECT DISTINCT domain FROM harvest_history")]
    for domain in domains:
        rows = _last_two(conn, domain)
        if len(rows) < 2:
            continue
        latest, prev = rows[0], rows[1]
        prev_n = prev["products"] or 0
        latest_n = latest["products"] or 0
        if prev_n >= min_prev and latest_n < prev_n * (1 - threshold):
            drifted.append({
                "domain": domain, "prev": prev_n, "latest": latest_n,
                "drop_pct": round(100 * (1 - latest_n / prev_n), 1),
                "reason": "yield_drop",
            })
    return drifted


def detect_quarantine_spikes(conn: sqlite3.Connection, *,
                             ratio: float = QUARANTINE_SPIKE, min_prev: int = MIN_PREV) -> list[dict]:
    out = []
    for domain in [r[0] for r in conn.execute("SELECT DISTINCT domain FROM harvest_history")]:
        row = conn.execute(
            "SELECT products, quarantined FROM harvest_history WHERE domain=? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1", (domain,)).fetchone()
        if not row:
            continue
        total = (row["products"] or 0) + (row["quarantined"] or 0)
        if total >= min_prev and (row["quarantined"] or 0) > total * ratio:
            out.append({"domain": domain, "quarantined": row["quarantined"],
                        "products": row["products"], "reason": "quarantine_spike"})
    return out


def open_repair_jobs(conn: sqlite3.Connection, drifted: list[dict]) -> int:
    """Enqueue a high-priority repair job per drifted domain (idempotent)."""
    for d in drifted:
        jobs.enqueue(conn, REPAIR_STAGE, d["domain"], priority=10, max_attempts=2)
    return len(drifted)


def scan_and_open(conn: sqlite3.Connection) -> dict:
    """Full self-healing tick: detect drift + quarantine spikes, open repairs."""
    drifted = detect_drift(conn)
    spikes = detect_quarantine_spikes(conn)
    # dedupe by domain (a domain can trip both signals)
    by_domain = {d["domain"]: d for d in (spikes + drifted)}
    opened = open_repair_jobs(conn, list(by_domain.values()))
    return {"drifted": drifted, "quarantine_spikes": spikes, "repairs_opened": opened}
