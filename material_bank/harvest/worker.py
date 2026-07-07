"""Queue-driven harvest workers with retry.

Seeds one ``harvest`` job per active supplier, then runs a pool of workers that
each claim jobs atomically, dispatch the tier-appropriate harvester, and report
success/failure back to the queue. Transient failures (unreachable endpoint,
crash) are retried with backoff; exhausted ones dead-letter — all tracked.
Idempotent + resumable: re-running skips already-harvested URLs.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import db, jobs
from ..fetch import Fetcher
from .run import DISPATCH, DOMAIN_HARVESTERS, _registry_brand

STAGE = "harvest"


class TransientHarvestError(RuntimeError):
    """Raised when a supplier is unreachable — the queue will retry it."""


def seed_harvest_jobs(conn, *, tiers=("shopify", "woocommerce", "jsonld"),
                      exclude=None, reset=False) -> int:
    exclude = exclude or set()
    ph = ",".join("?" for _ in tiers)
    rows = conn.execute(
        f"SELECT domain FROM suppliers WHERE status='active' AND scrape_tier IN ({ph})", tiers)
    n = 0
    for r in rows:
        if r["domain"] in exclude:
            continue
        jobs.enqueue(conn, STAGE, r["domain"], reset=reset)
        n += 1
    return n


def run_harvest_job(conn: sqlite3.Connection, domain: str, *,
                    jsonld_limit: int | None, min_interval: float) -> dict:
    row = conn.execute("SELECT * FROM suppliers WHERE domain=?", (domain,)).fetchone()
    if row is None:
        raise ValueError(f"no supplier row for {domain}")
    tier = row["scrape_tier"]
    harvester = DOMAIN_HARVESTERS.get(domain) or DISPATCH.get(tier)
    if harvester is None:
        raise ValueError(f"no harvester for {domain} (tier {tier!r})")
    fetcher = Fetcher(min_interval=min_interval, raw_dir=None)
    kwargs = dict(domain=domain, brand=_registry_brand(row), categories=row["categories"] or "")
    if tier == "jsonld":
        kwargs["sitemap_url"] = row["sitemap_url"]
        kwargs["base_host"] = row["final_host"] or domain
        kwargs["limit"] = jsonld_limit
    stats = harvester(conn, fetcher, **kwargs)
    if stats.get("reachable") is False:  # transient -> raise so the queue retries
        raise TransientHarvestError(f"{domain} unreachable (attempt will retry)")
    after = conn.execute("SELECT COUNT(*) FROM products WHERE supplier_domain=?",
                         (domain,)).fetchone()[0]
    priced = conn.execute(
        "SELECT COUNT(DISTINCT po.product_id) FROM price_observation po "
        "JOIN products p ON p.id=po.product_id WHERE p.supplier_domain=?", (domain,)).fetchone()[0]
    conn.execute("UPDATE suppliers SET last_harvest=?, last_yield=? WHERE domain=?",
                 (db.now_iso(), after, domain))
    db.record_harvest(conn, domain, products=after, priced=priced,
                      quarantined=stats.get("quarantined", 0))
    return stats


def _worker(db_path, *, jsonld_limit, min_interval, backoff_base, on_job) -> int:
    conn = db.connect(db_path, check_same_thread=False)
    processed = 0
    while True:
        job = jobs.claim(conn, STAGE)
        if job is None:
            break
        try:
            result = run_harvest_job(conn, job["target"],
                                     jsonld_limit=jsonld_limit, min_interval=min_interval)
            jobs.complete(conn, job["id"], result)
            status = "done"
        except Exception as exc:
            status = jobs.fail(conn, job["id"], f"{type(exc).__name__}: {exc}", base=backoff_base)
            result = {"error": str(exc)}
        processed += 1
        if on_job:
            on_job(job["target"], status, result)
    conn.close()
    return processed


def run_workers(db_path=None, *, workers: int = 8, jsonld_limit: int | None = None,
                min_interval: float = 2.0, backoff_base: int = jobs.BACKOFF_BASE_S,
                on_job=None) -> dict:
    """Drain the harvest queue with a pool of workers. Requeues stale-running
    jobs first (recover from a prior crash)."""
    db_path = str(db_path or db.DEFAULT_DB_PATH)
    control = db.connect(db_path)
    jobs.requeue_stale_running(control)
    control.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_worker, db_path, jsonld_limit=jsonld_limit,
                            min_interval=min_interval, backoff_base=backoff_base,
                            on_job=on_job) for _ in range(workers)]
        for f in as_completed(futs):
            f.result()

    control = db.connect(db_path)
    c = jobs.counts(control, STAGE)
    control.close()
    return c


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="mb-harvest-queue")
    ap.add_argument("--tiers", default="shopify,woocommerce,jsonld")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--jsonld-limit", type=int, default=0,
                    help="max PDPs per jsonld supplier; 0 = unlimited (collect everything)")
    ap.add_argument("--min-interval", type=float, default=2.0)
    ap.add_argument("--exclude", default="ikea.com,lxhausys.com")
    ap.add_argument("--reset", action="store_true", help="re-arm all jobs (fresh cycle)")
    ap.add_argument("--retry-dead", action="store_true", help="re-arm dead-lettered jobs and exit")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.migrate(conn)
    if args.retry_dead:
        n = jobs.retry_dead(conn, STAGE)
        print(f"re-armed {n} dead-lettered jobs", file=sys.stderr)
        return 0
    tiers = tuple(t.strip() for t in args.tiers.split(",") if t.strip())
    exclude = {d.strip() for d in args.exclude.split(",") if d.strip()}
    jsonld_limit = None if args.jsonld_limit <= 0 else args.jsonld_limit
    n = seed_harvest_jobs(conn, tiers=tiers, exclude=exclude, reset=args.reset)
    print(f"seeded {n} harvest jobs; queue={jobs.counts(conn, STAGE)}", file=sys.stderr)
    conn.close()

    def prog(target, status, result):
        p = result.get("products", 0) if isinstance(result, dict) else 0
        print(f"  {target:<24} {status:<8} products={p}", file=sys.stderr, flush=True)

    final = run_workers(args.db, workers=args.workers, jsonld_limit=jsonld_limit,
                        min_interval=args.min_interval, on_job=prog)
    print(f"\n=== queue final: {final} ===", file=sys.stderr)
    dead = jobs.dead_letters(db.connect(args.db), STAGE)
    if dead:
        print("dead-letters:", file=sys.stderr)
        for d in dead:
            print(f"  {d['target']}: {d['last_error'][:80]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
