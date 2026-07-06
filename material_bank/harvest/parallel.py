"""Parallel registry harvest — many domains at once, each still polite.

The ~1 req/2s budget is PER DOMAIN, so distinct domains are independent: we run
one worker per supplier, each with its own Fetcher (own per-domain limiter) and
its own SQLite connection (WAL + busy_timeout absorb concurrent writes). Wall
clock drops ~N×; each individual domain is never hit faster than its interval.

Single huge domains stay serial by the rule — cap them, don't rush them.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .. import db
from ..fetch import Fetcher
from .run import DISPATCH, _registry_brand


def _supplier_rows(conn, tiers, exclude):
    ph = ",".join("?" for _ in tiers)
    rows = conn.execute(
        f"SELECT * FROM suppliers WHERE status='active' AND scrape_tier IN ({ph}) "
        f"ORDER BY scrape_tier, domain", tiers)
    return [dict(r) for r in rows if r["domain"] not in exclude]


def _harvest_one(db_path, row, *, jsonld_limit, min_interval):
    conn = db.connect(db_path, check_same_thread=False)
    fetcher = Fetcher(min_interval=min_interval, raw_dir=None)  # skip raw: many domains, save disk
    harvester = DISPATCH[row["scrape_tier"]]
    kwargs = dict(domain=row["domain"], brand=_registry_brand(row),
                  categories=row["categories"] or "")
    if row["scrape_tier"] == "jsonld":
        kwargs["sitemap_url"] = row["sitemap_url"]
        kwargs["base_host"] = row["final_host"] or row["domain"]
        kwargs["limit"] = jsonld_limit
    try:
        stats = harvester(conn, fetcher, **kwargs)
    except Exception as exc:
        db.quarantine(conn, stage="harvest", source_url=row["domain"],
                      reason=f"crash {type(exc).__name__}: {exc}", raw_ref=None)
        stats = {"domain": row["domain"], "products": 0, "error": str(exc)}
    after = conn.execute("SELECT COUNT(*) FROM products WHERE supplier_domain=?",
                         (row["domain"],)).fetchone()[0]
    conn.execute("UPDATE suppliers SET last_harvest=?, last_yield=? WHERE domain=?",
                 (db.now_iso(), after, row["domain"]))
    conn.commit()
    conn.close()
    return stats


def harvest_parallel(
    db_path: str | Path = None,
    *,
    tiers: tuple[str, ...] = ("jsonld",),
    workers: int = 8,
    jsonld_limit: int | None = 2000,
    min_interval: float = 2.0,
    exclude_domains: set[str] | None = None,
    on_supplier=None,
) -> list[dict]:
    db_path = str(db_path or db.DEFAULT_DB_PATH)
    control = db.connect(db_path)
    rows = _supplier_rows(control, tiers, exclude_domains or set())
    control.close()

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_harvest_one, db_path, r, jsonld_limit=jsonld_limit,
                            min_interval=min_interval): r for r in rows}
        for fut in as_completed(futs):
            stats = fut.result()
            results.append(stats)
            if on_supplier:
                on_supplier(stats)
    return results


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="mb-harvest-parallel")
    ap.add_argument("--tiers", default="jsonld")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--jsonld-limit", type=int, default=2000)
    ap.add_argument("--min-interval", type=float, default=2.0)
    ap.add_argument("--exclude", default="ikea.com,lxhausys.com,orientbell.com")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    tiers = tuple(t.strip() for t in args.tiers.split(",") if t.strip())
    exclude = {d.strip() for d in args.exclude.split(",") if d.strip()}

    def prog(st):
        print(f"  {st['domain']:<24} products={st.get('products',0)} "
              f"priced={st.get('priced',0)} cand={st.get('candidates','-')} "
              f"quar={st.get('quarantined',0)}", file=sys.stderr, flush=True)

    print(f"parallel harvest: tiers={tiers} workers={args.workers} "
          f"cap={args.jsonld_limit}/supplier", file=sys.stderr)
    results = harvest_parallel(args.db, tiers=tiers, workers=args.workers,
                               jsonld_limit=args.jsonld_limit, min_interval=args.min_interval,
                               exclude_domains=exclude, on_supplier=prog)
    total = sum(r.get("products", 0) for r in results)
    priced = sum(r.get("priced", 0) for r in results)
    print(f"\n=== {len(results)} suppliers · {total} products · {priced} priced ===",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
