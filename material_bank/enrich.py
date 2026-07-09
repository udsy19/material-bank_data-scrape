"""Enrichment stage (Phase B): fill missing attributes deterministically.

Two passes, both idempotent and NULL-only (a harvested/measured value is never
overwritten by a derived one — hard rule):

  1. text_pass   — free win, no network: run the extractors over every product's
     title AND its already-harvested description (the same extraction the PDP
     path applies to fetched descriptions, minus the fetch).
  2. refetch     — queue-driven (stage='enrich', one job per supplier): re-fetch
     below-gate products' PDPs (politely, per-domain rate limit), extract from
     title + ld+json description/additionalProperty, store the description for
     later phases, derive sheet coverage. `enriched_at` is the resume marker.

Everything written carries provenance {source: 'extracted:…', basis: 'derived'}.
The planner seeds jobs from the publish-gate gap; the hourly sweep drains them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, jobs
from .extract import derive_sheet_coverage, extract_all
from .fetch import Fetcher
from .probe import _extract_jsonld_products

STAGE = "enrich"
_FIELDS = ("size_mm", "finish", "color", "color_family", "thickness_mm",
           "coverage_sqft_per_box")
_META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name="description"|property="og:description")[^>]+content="([^"]{20,800})"', re.I)

_UPDATE_SQL = """
UPDATE products SET
    size_mm=COALESCE(size_mm, ?), finish=COALESCE(finish, ?),
    color=COALESCE(color, ?), color_family=COALESCE(color_family, ?),
    thickness_mm=COALESCE(thickness_mm, ?),
    coverage_sqft_per_box=COALESCE(coverage_sqft_per_box, ?),
    description=COALESCE(description, ?),
    provenance=?, missing=?, enriched_at=COALESCE(?, enriched_at)
WHERE id=?
"""


def _apply_extraction(row, text: str, *, source: str, description: str | None = None,
                      enriched_at: str | None = None):
    """Compute the NULL-only update tuple for one row, or None if nothing new."""
    found = extract_all(text)
    size = row["size_mm"] or found.get("size_mm")
    if row["coverage_sqft_per_box"] is None and "coverage_sqft_per_box" not in found:
        cov = derive_sheet_coverage(size, row["category"] or "")
        if cov:
            found["coverage_sqft_per_box"] = cov
    new_fields = {f: v for f, v in found.items() if row[f] is None}
    if not new_fields and not description and not enriched_at:
        return None
    prov = json.loads(row["provenance"] or "{}")
    missing = [m for m in json.loads(row["missing"] or "[]") if m not in new_fields]
    for f in new_fields:
        prov[f] = {"confidence": 0.85, "source": source, "basis": "derived"}
    return (
        new_fields.get("size_mm"), new_fields.get("finish"), new_fields.get("color"),
        new_fields.get("color_family"), new_fields.get("thickness_mm"),
        new_fields.get("coverage_sqft_per_box"), description,
        json.dumps(prov), json.dumps(missing), enriched_at, row["id"],
    ), len(new_fields)


_ROW_COLS = ("id, title, category, size_mm, finish, color, color_family, "
             "thickness_mm, coverage_sqft_per_box, provenance, missing")


def text_pass(conn: sqlite3.Connection, *, batch: int = 20000) -> dict:
    """Extract from title + stored description for every product still missing a
    target field. Offline, idempotent, NULL-only — mines the descriptions we've
    already harvested (title-first so a title value wins ties)."""
    rows = conn.execute(  # read fully BEFORE writing (WAL BUSY_SNAPSHOT rule)
        f"SELECT {_ROW_COLS}, description FROM products WHERE size_mm IS NULL "
        f"OR finish IS NULL OR color IS NULL OR thickness_mm IS NULL").fetchall()
    updates, fields_filled = [], 0
    for row in rows:
        text = row["title"] or ""
        if row["description"]:
            text = f"{text} | {row['description']}"
        result = _apply_extraction(row, text, source="extracted:text")
        if result:
            updates.append(result[0])
            fields_filled += result[1]
    for i in range(0, len(updates), batch):
        conn.executemany(_UPDATE_SQL, updates[i:i + batch])
        conn.commit()
    return {"scanned": len(rows), "products_updated": len(updates),
            "fields_filled": fields_filled}


def _pdp_text(html: str) -> tuple[str, str | None]:
    """(extraction text, description) from a PDP: ld+json first, meta fallback."""
    parts, description = [], None
    for node in _extract_jsonld_products(html):
        if isinstance(node.get("description"), str):
            description = description or node["description"].strip()[:2000]
            parts.append(node["description"])
        for ap in node.get("additionalProperty") or []:
            if isinstance(ap, dict):
                parts.append(f"{ap.get('name', '')} {ap.get('value', '')}")
    if description is None:
        m = _META_DESC_RE.search(html)
        if m:
            description = m.group(1).strip()
            parts.append(description)
    return " | ".join(p for p in parts if p), description


def candidates(conn: sqlite3.Connection, domain: str, limit: int | None):
    sql = (f"SELECT {_ROW_COLS}, source_url FROM products "
           f"WHERE supplier_domain=? AND publish_ready=0 AND enriched_at IS NULL "
           f"AND source_url IS NOT NULL ORDER BY id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (domain,)).fetchall()


def run_enrich_job(conn: sqlite3.Connection, domain: str, fetcher: Fetcher,
                   *, limit: int | None = 400) -> dict:
    rows = candidates(conn, domain, limit)
    stats = {"domain": domain, "candidates": len(rows), "products_updated": 0,
             "fields_filled": 0, "fetch_failed": 0, "reachable": True}
    updates = []
    for row in rows:
        r = fetcher.get(row["source_url"])
        ts = db.now_iso()
        if not r.ok:
            stats["fetch_failed"] += 1
            updates.append((None, None, None, None, None, None, None,
                            row["provenance"] or "{}", row["missing"] or "[]", ts, row["id"]))
            continue
        text, description = _pdp_text(r.text)
        result = _apply_extraction(row, f"{row['title'] or ''} | {text}",
                                   source="extracted:pdp", description=description,
                                   enriched_at=ts)
        if result:
            updates.append(result[0])
            stats["products_updated"] += 1
            stats["fields_filled"] += result[1]
    if updates:
        conn.executemany(_UPDATE_SQL, updates)
        conn.commit()
    if rows and stats["fetch_failed"] == len(rows):
        stats["reachable"] = False   # whole batch failed -> let the queue retry
    return stats


def seed_enrich_jobs(conn: sqlite3.Connection) -> int:
    """One job per supplier that still has un-enriched, below-gate products."""
    rows = conn.execute(
        "SELECT supplier_domain, COUNT(*) n FROM products "
        "WHERE publish_ready=0 AND enriched_at IS NULL AND source_url IS NOT NULL "
        "GROUP BY supplier_domain ORDER BY n DESC").fetchall()
    for r in rows:
        jobs.enqueue(conn, STAGE, r["supplier_domain"], priority=min(r["n"], 100), reset=True)
    return len(rows)


def _worker(db_path: str, *, limit: int, min_interval: float) -> int:
    conn = db.connect(db_path, check_same_thread=False)
    done = 0
    while True:
        job = jobs.claim(conn, STAGE)
        if job is None:
            break
        try:
            stats = run_enrich_job(conn, job["target"],
                                   Fetcher(min_interval=min_interval, raw_dir=None),
                                   limit=limit)
            if not stats["reachable"]:
                raise RuntimeError(f"{job['target']}: all {stats['candidates']} fetches failed")
            jobs.complete(conn, job["id"], stats)
            done += 1
        except Exception as exc:
            jobs.fail(conn, job["id"], f"{type(exc).__name__}: {exc}")
    conn.close()
    return done


def drain(db_path=None, *, workers: int = 4, limit: int = 400,
          min_interval: float = 2.0) -> dict:
    db_path = str(db_path or db.DEFAULT_DB_PATH)
    control = db.connect(db_path)
    jobs.requeue_stale_running(control)
    control.close()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_worker, db_path, limit=limit, min_interval=min_interval)
                for _ in range(workers)]
        for f in as_completed(futs):
            f.result()
    control = db.connect(db_path)
    out = jobs.counts(control, STAGE)
    control.close()
    return out


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="mb-enrich")
    ap.add_argument("--text-pass", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--drain", action="store_true")
    ap.add_argument("--limit", type=int, default=400, help="PDP refetches per job per drain")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.migrate(conn)
    if args.text_pass:
        print(f"[enrich] text-pass: {text_pass(conn)}", file=sys.stderr)
    if args.seed:
        print(f"[enrich] seeded {seed_enrich_jobs(conn)} supplier jobs", file=sys.stderr)
    conn.close()
    if args.drain:
        print(f"[enrich] drain: {drain(args.db, workers=args.workers, limit=args.limit)}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
