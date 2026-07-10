"""Supplier procurement enrichment (the "who supplies it / where to buy" stage).

For each supplier we fetch a handful of pages on ITS OWN registered domain —
home, contact, about — extract company/contact/address/GSTIN with
``company_extract``, and store them provenance-tagged on the ``suppliers`` row.
A store-locator link, if present, is recorded (dealer harvesting itself is
``dealers.py``).

Legal guardrails, encoded (from the legal read):
  * Own-domain only. We only ever fetch the supplier's own domain — never a
    directory (IndiaMART/Justdial) or a third-party aggregator.
  * robots.txt honored per candidate path; politeness via the Fetcher's
    per-domain rate limit.
  * No individual persons' names are ever extracted (see company_extract).
  * Every field carries {source, basis:'observed', confidence, observed_at}.

Durable, idempotent, resumable — same queue shape as the product enrich stage.
"""

from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, jobs
from .company_extract import extract_company, merge_company
from .fetch import Fetcher
from .robots import parse_robots

STAGE = "supplier"

# tried in order; Shopify puts these under /pages/. We stop once enough pages
# succeed — politeness first, this is not a crawl.
_CANDIDATE_PATHS = (
    "", "/contact-us", "/contact", "/pages/contact-us", "/about-us", "/about",
    "/pages/about-us", "/company", "/reach-us",
)
_MAX_PAGES = 5
_LOCATOR_RE = re.compile(
    r'href=["\']([^"\']*(?:store-locator|where-to-buy|dealer-locator|find-a-dealer|'
    r'dealers|dealer-network)[^"\']*)["\']', re.I)

_COLUMNS = ("legal_name", "phones", "emails", "address", "city", "state", "pincode",
            "gstin", "cin", "dealer_locator_url", "social", "logo_url", "year_established")
_JSON_COLS = {"phones", "emails", "social"}


def _working_base(fetcher: Fetcher, domain: str) -> tuple[str, str] | None:
    """(base_url, html) for the host that answers — bare or www."""
    hosts = [domain] if domain.startswith("www.") else [domain, f"www.{domain}"]
    for host in hosts:
        r = fetcher.get(f"https://{host}/")
        if r.ok and r.text:
            return f"https://{host}", r.text
    return None


def _absolute(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")


def enrich_supplier(conn: sqlite3.Connection, domain: str, fetcher: Fetcher) -> dict:
    """Fetch a supplier's own pages, extract company info, store it. Returns stats."""
    stats = {"domain": domain, "pages_fetched": 0, "fields": 0, "reachable": True}
    base_html = _working_base(fetcher, domain)
    if base_html is None:
        stats["reachable"] = False
        return stats
    base, home_html = base_html

    robots = parse_robots(base, fetcher.get(f"{base}/robots.txt").text, fetched=True)

    pages, locator = [], None
    for path in _CANDIDATE_PATHS:
        if len(pages) >= _MAX_PAGES:
            break
        if not robots.can_fetch(path or "/"):
            continue
        html = home_html if path == "" else None
        if html is None:
            r = fetcher.get(f"{base}{path}")
            if not r.ok or not r.text:
                continue
            html = r.text
        stats["pages_fetched"] += 1
        pages.append(extract_company(html, f"{base}{path}"))
        if locator is None:
            m = _LOCATOR_RE.search(html)
            if m:
                locator = _absolute(base, m.group(1))

    record = merge_company(pages)
    if locator and "dealer_locator_url" not in record:
        record["dealer_locator_url"] = locator
        record.setdefault("_provenance", {})["dealer_locator_url"] = {
            "source": base, "basis": "observed", "confidence": 0.9}
    stats["fields"] = len([k for k in record if k != "_provenance" and record[k]])
    _store(conn, domain, record)
    return stats


def _store(conn: sqlite3.Connection, domain: str, record: dict) -> None:
    sets, params = [], []
    for col in _COLUMNS:
        if col in record and record[col]:
            v = record[col]
            sets.append(f"{col}=?")
            params.append(json.dumps(v) if col in _JSON_COLS else v)
    prov = record.get("_provenance") or {}
    sets.append("supplier_provenance=?"); params.append(json.dumps(prov))
    sets.append("supplier_enriched_at=?"); params.append(db.now_iso())
    params.append(domain)
    conn.execute(f"UPDATE suppliers SET {', '.join(sets)} WHERE domain=?", params)
    conn.commit()


def seed_supplier_jobs(conn: sqlite3.Connection, *, only_missing: bool = False) -> int:
    """One job per active registry supplier (optionally only un-enriched ones)."""
    sql = "SELECT domain FROM suppliers WHERE status='active'"
    if only_missing:
        sql += " AND supplier_enriched_at IS NULL"
    rows = conn.execute(sql).fetchall()
    for r in rows:
        jobs.enqueue(conn, STAGE, r["domain"], reset=True)
    return len(rows)


def _worker(db_path: str, *, min_interval: float) -> int:
    conn = db.connect(db_path, check_same_thread=False)
    done = 0
    while True:
        job = jobs.claim(conn, STAGE)
        if job is None:
            break
        try:
            stats = enrich_supplier(conn, job["target"],
                                    Fetcher(min_interval=min_interval, raw_dir=None))
            if not stats["reachable"]:
                raise RuntimeError(f"{job['target']}: own domain unreachable")
            jobs.complete(conn, job["id"], stats)
            done += 1
        except Exception as exc:
            jobs.fail(conn, job["id"], f"{type(exc).__name__}: {exc}")
    conn.close()
    return done


def drain(db_path=None, *, workers: int = 4, min_interval: float = 2.0) -> dict:
    db_path = str(db_path or db.DEFAULT_DB_PATH)
    control = db.connect(db_path)
    jobs.requeue_stale_running(control)
    control.close()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_worker, db_path, min_interval=min_interval)
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

    ap = argparse.ArgumentParser(prog="mb-supplier-enrich")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--drain", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.migrate(conn)
    if args.seed:
        print(f"[supplier] seeded {seed_supplier_jobs(conn, only_missing=args.only_missing)} jobs",
              file=sys.stderr)
    conn.close()
    if args.drain:
        print(f"[supplier] drain: {drain(args.db, workers=args.workers)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
