"""Registry-driven harvest driver.

Reads the suppliers registry, dispatches each row to the harvester its probed
``scrape_tier`` selects, and records ``last_harvest`` / ``last_yield``. New
distributor = new registry row; no code change (CLAUDE.md).
"""

from __future__ import annotations

import sqlite3
import sys

from .. import db
from ..fetch import Fetcher
from .jsonld import harvest_jsonld
from .kajaria import harvest_kajaria
from .shopify import harvest_shopify
from .steelcase import harvest_steelcase
from .woocommerce import harvest_woo

# tier -> harvester. tier3 uses the Playwright harvester (run separately).
DISPATCH = {
    "shopify": harvest_shopify,
    "woocommerce": harvest_woo,
    "jsonld": harvest_jsonld,
}

# domain -> custom harvester (checked before tier DISPATCH) for sites with a
# bespoke structure (like Orientbell's Magento, Kajaria's static+PDF specs).
DOMAIN_HARVESTERS = {
    "kajariaceramics.com": harvest_kajaria,
    "steelcase.com": harvest_steelcase,  # full asia-en catalog (in.steelcase.com = refurb shop)
}


def _registry_brand(row: sqlite3.Row) -> str:
    return row["brand"] or row["domain"]


def harvest_registry(
    conn: sqlite3.Connection,
    *,
    tiers: tuple[str, ...] = ("shopify", "woocommerce"),
    fetcher_factory=Fetcher,
    jsonld_limit: int | None = None,
    exclude_domains: set[str] | None = None,
    on_supplier=None,
) -> list[dict]:
    exclude = exclude_domains or set()
    placeholders = ",".join("?" for _ in tiers)
    rows = [r for r in conn.execute(
        f"SELECT * FROM suppliers WHERE status='active' AND scrape_tier IN ({placeholders}) "
        f"ORDER BY scrape_tier, domain", tiers) if r["domain"] not in exclude]
    results = []
    for row in rows:
        harvester = DISPATCH[row["scrape_tier"]]
        fetcher = fetcher_factory()
        kwargs = dict(domain=row["domain"], brand=_registry_brand(row),
                      categories=row["categories"] or "")
        if row["scrape_tier"] == "jsonld":  # jsonld needs sitemap/host + a cap
            kwargs["sitemap_url"] = row["sitemap_url"]
            kwargs["base_host"] = row["final_host"] or row["domain"]
            kwargs["limit"] = jsonld_limit
        before = conn.execute("SELECT COUNT(*) FROM products WHERE supplier_domain=?",
                              (row["domain"],)).fetchone()[0]
        try:
            stats = harvester(conn, fetcher, **kwargs)
        except Exception as exc:  # one bad supplier must not kill the sweep
            db.quarantine(conn, stage="harvest", source_url=row["domain"],
                          reason=f"harvester crash {type(exc).__name__}: {exc}", raw_ref=None)
            stats = {"domain": row["domain"], "error": str(exc), "products": 0}
        after = conn.execute("SELECT COUNT(*) FROM products WHERE supplier_domain=?",
                             (row["domain"],)).fetchone()[0]
        conn.execute("UPDATE suppliers SET last_harvest=?, last_yield=? WHERE domain=?",
                     (db.now_iso(), after, row["domain"]))
        conn.commit()
        stats["net_new"] = after - before
        results.append(stats)
        if on_supplier:
            on_supplier(row["domain"], stats)
    return results


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="mb-harvest")
    ap.add_argument("--tiers", default="shopify,woocommerce")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.migrate(conn)
    tiers = tuple(t.strip() for t in args.tiers.split(",") if t.strip())

    def prog(domain, stats):
        print(f"  {domain:<32} tier-yield: products={stats.get('products',0)} "
              f"priced={stats.get('priced',0)} pages={stats.get('pages',0)} "
              f"reachable={stats.get('reachable')}", file=sys.stderr, flush=True)

    results = harvest_registry(conn, tiers=tiers, on_supplier=prog)
    total = sum(r.get("products", 0) for r in results)
    reachable = sum(1 for r in results if r.get("reachable"))
    print(f"\n=== harvested {len(results)} suppliers ({reachable} reachable); "
          f"{total} product rows ===", file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
