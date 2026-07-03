import json

from material_bank import db as db_mod
from material_bank.cli import probe_domains, select_domains
from material_bank.models import Supplier
from material_bank.probe import run_probe

from .conftest import FakeFetcher, shopify_json, urlset

ROBOTS_OK = {"status": 200, "text": "User-agent: *\nAllow: /\nSitemap: https://d.com/sitemap.xml"}


def _shopify_fetcher():
    return FakeFetcher(
        landing={"status": 200, "text": "x"},
        robots=ROBOTS_OK,
        products_json={"status": 200, "text": shopify_json("1999.00")},
        sitemap={"status": 200, "text": urlset(["https://d.com/products/a", "https://d.com/products/b"])},
    )


def test_write_result_persists_all_probe_fields(conn):
    db_mod.seed(conn, [Supplier(brand="D", domain="d.com", categories="tiles")])
    run_probe(conn, "d.com", _shopify_fetcher())
    row = conn.execute("SELECT * FROM suppliers WHERE domain='d.com'").fetchone()
    assert row["scrape_tier"] == "shopify"
    assert row["price_published"] == "yes"
    assert row["probe_status"] == "ok"
    assert row["probed_at"] is not None
    assert row["sku_estimate"] == 2
    log = json.loads(row["probe_log"])
    assert any(e["step"] == "shopify" for e in log)  # decision trail persisted


def test_resumability_skips_probed_rows(conn):
    db_mod.seed(conn, [
        Supplier(brand="A", domain="a.com"),
        Supplier(brand="B", domain="b.com"),
    ])
    # Pretend a.com already probed.
    conn.execute("UPDATE suppliers SET probed_at='2026-07-02T00:00:00Z' WHERE domain='a.com'")
    conn.commit()

    remaining = select_domains(conn, force=False)
    assert remaining == ["b.com"]                 # a.com skipped (resume)
    assert select_domains(conn, force=True) == ["a.com", "b.com"]  # --force re-probes both


def test_category_filter(conn):
    db_mod.seed(conn, [
        Supplier(brand="T", domain="t.com", categories="tiles|sanitaryware"),
        Supplier(brand="P", domain="p.com", categories="paint"),
    ])
    assert select_domains(conn, category="tiles") == ["t.com"]


def test_probe_domains_writes_from_main_thread(conn):
    db_mod.seed(conn, [Supplier(brand="D", domain="d.com", categories="tiles")])
    results = probe_domains(
        conn, ["d.com"], workers=2,
        fetcher_factory=_shopify_fetcher,
    )
    assert len(results) == 1
    row = conn.execute("SELECT scrape_tier FROM suppliers WHERE domain='d.com'").fetchone()
    assert row["scrape_tier"] == "shopify"
