"""The trust contract (Phase A): completeness scoring, contradiction checks,
publish gate.

Every product gets:
  - ``completeness`` 0–100 — weighted presence of the attributes *its category*
    requires. Surfaces are held to a stricter bar (units!) than decor.
  - ``verification_tier`` — unverified → auto_validated (passes deterministic
    contradiction checks) → reviewed → golden (human tiers, never auto-set and
    never auto-downgraded).
  - ``publish_ready`` — the gate: complete enough AND not contradictory. Only
    these records are served on the external catalog surface.

Scoring is deterministic, explainable, cheap (one SQL pass + one UPDATE batch),
and idempotent — the planner reruns it every sweep.
"""

from __future__ import annotations

import re
import sqlite3

from .db import now_iso
from .harvest.common import is_placeholder_title
from .models import is_surface

# Weighted presence. fresh_price = freshest observation ≤ STALE_DAYS old.
CORE_WEIGHTS = {
    "title": 10, "brand": 5, "image_url": 20, "category": 5,
    "fresh_price": 25, "source_url": 5,
}                                    # = 70; non-surfaces are scaled to /100
SURFACE_WEIGHTS = {                  # surfaces need units (hard rule) -> +30
    "size_mm": 10, "finish": 5, "price_unit": 10, "coverage_sqft_per_box": 5,
}
STALE_DAYS = 90
PUBLISH_THRESHOLDS = {"surface": 70, "default": 60}
_SIZE_RE = re.compile(r"^\d+(\.\d+)?x\d+(\.\d+)?$")


def _present(row, field) -> bool:
    v = row[field]
    return v is not None and str(v).strip() != ""


def score_row(row, price_age_days: float | None) -> tuple[int, bool]:
    """(completeness 0-100, is_surface) for one product row."""
    surface = is_surface(row["category"] or "")
    weights = dict(CORE_WEIGHTS)
    if surface:
        weights.update(SURFACE_WEIGHTS)
    total = sum(weights.values())
    got = 0
    for field, w in weights.items():
        if field == "fresh_price":
            ok = price_age_days is not None and price_age_days <= STALE_DAYS
        else:
            ok = _present(row, field)
        if ok:
            got += w
    return round(100 * got / total), surface


def tier_row(row) -> str:
    """Deterministic contradiction checks. Human tiers are never downgraded."""
    current = row["verification_tier"] if "verification_tier" in row.keys() else None
    if current in ("reviewed", "golden"):
        return current
    title = (row["title"] or "").strip()
    if len(title) < 3 or is_placeholder_title(title):
        return "unverified"
    if _present(row, "size_mm") and not _SIZE_RE.match(str(row["size_mm"]).strip()):
        return "unverified"          # a size that doesn't parse is a contradiction
    cov = row["coverage_sqft_per_box"]
    if cov is not None and cov <= 0:
        return "unverified"
    return "auto_validated"


def publish_gate(completeness: int, tier: str, surface: bool) -> bool:
    threshold = PUBLISH_THRESHOLDS["surface" if surface else "default"]
    return completeness >= threshold and tier != "unverified"


_SCORE_QUERY = """
SELECT p.id, p.title, p.brand, p.image_url, p.category, p.source_url,
       p.size_mm, p.finish, p.price_unit, p.coverage_sqft_per_box,
       p.verification_tier,
       CAST(julianday('now') - julianday(l.observed_at) AS REAL) AS price_age
FROM products p
LEFT JOIN (
    SELECT product_id, observed_at,
           ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY observed_at DESC) rn
    FROM price_observation
) l ON l.product_id = p.id AND l.rn = 1
"""


def score_all(conn: sqlite3.Connection, *, batch: int = 5000) -> dict:
    """Score every product; write completeness/tier/publish_ready. Idempotent."""
    ts = now_iso()
    updates, summary = [], {"scored": 0, "publish_ready": 0,
                            "tiers": {"unverified": 0, "auto_validated": 0,
                                      "reviewed": 0, "golden": 0}}
    cur = conn.execute(_SCORE_QUERY)
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            break
        for row in rows:
            completeness, surface = score_row(row, row["price_age"])
            tier = tier_row(row)
            ready = publish_gate(completeness, tier, surface)
            updates.append((completeness, tier, int(ready), ts, row["id"]))
            summary["scored"] += 1
            summary["publish_ready"] += int(ready)
            summary["tiers"][tier] = summary["tiers"].get(tier, 0) + 1
        conn.executemany(
            "UPDATE products SET completeness=?, verification_tier=?, "
            "publish_ready=?, scored_at=? WHERE id=?", updates)
        conn.commit()
        updates.clear()
    return summary


def _one(conn, sql, *params):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def quality_report(conn: sqlite3.Connection) -> dict:
    """Live quality state (the /api/quality payload + planner input)."""
    q = lambda s, *p: _one(conn, s, *p)  # noqa: E731
    total = q("SELECT COUNT(*) FROM products")
    report = {
        "products": total,
        "publish_ready": q("SELECT COUNT(*) FROM products WHERE publish_ready=1"),
        "median_completeness": q(
            "SELECT completeness FROM products WHERE completeness IS NOT NULL "
            "ORDER BY completeness LIMIT 1 OFFSET "
            "(SELECT COUNT(*) FROM products WHERE completeness IS NOT NULL)/2") or 0,
        "tiers": {r[0]: r[1] for r in conn.execute(
            "SELECT verification_tier, COUNT(*) FROM products GROUP BY verification_tier")},
        "worst_categories": [
            {"category": r[0], "products": r[1], "median": r[2], "ready": r[3]}
            for r in conn.execute("""
                SELECT category, COUNT(*) n,
                       CAST(AVG(completeness) AS INT),
                       SUM(publish_ready)
                FROM products WHERE completeness IS NOT NULL
                GROUP BY category HAVING n >= 200
                ORDER BY AVG(completeness) ASC LIMIT 8""")],
    }
    return report


def snapshot_metrics(conn: sqlite3.Connection) -> int:
    """Persist the scorecard — 'getting better' as a stored time series."""
    ts = now_iso()
    rep = quality_report(conn)
    rows = [
        (ts, "global", "products", rep["products"]),
        (ts, "global", "publish_ready", rep["publish_ready"]),
        (ts, "global", "median_completeness", rep["median_completeness"]),
    ]
    rows += [(ts, "global", f"tier_{k}", v) for k, v in rep["tiers"].items()]
    rows += [(ts, "global", "priced_fresh", conn.execute(
        "SELECT COUNT(DISTINCT product_id) FROM price_observation "
        "WHERE julianday('now') - julianday(observed_at) <= 7").fetchone()[0])]
    conn.executemany(
        "INSERT INTO metrics (captured_at, scope, key, value) VALUES (?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def metrics_trend(conn: sqlite3.Connection, key: str, limit: int = 60) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT captured_at, value FROM metrics WHERE key=? AND scope='global' "
        "ORDER BY captured_at DESC LIMIT ?", (key, limit))][::-1]
