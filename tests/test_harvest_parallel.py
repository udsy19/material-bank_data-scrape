"""Parallel harvest orchestrator — real threads, own connections, no lock errors."""

import material_bank.harvest.parallel as par
from material_bank import db as db_mod
from material_bank.harvest.common import build_product


def _fake_harvester(conn, fetcher, *, domain, brand, categories, **kw):
    # each worker inserts a few products on its own connection
    for i in range(5):
        db_mod.upsert_product(conn, build_product(
            brand=brand, sku=f"{domain}-{i}", title=f"{brand} item {i}",
            category=categories, source=domain), supplier_domain=domain)
    return {"domain": domain, "products": 5, "priced": 5}


def test_parallel_harvest_runs_all_suppliers(tmp_path, monkeypatch):
    path = tmp_path / "catalog.db"
    c = db_mod.connect(path)
    db_mod.migrate(c)
    for i in range(6):
        c.execute("INSERT INTO suppliers(brand,domain,categories,status,scrape_tier) "
                  "VALUES(?,?,?,?,?)", (f"B{i}", f"d{i}.com", "decor", "active", "shopify"))
    c.commit()
    c.close()

    monkeypatch.setitem(par.DISPATCH, "shopify", _fake_harvester)

    results = par.harvest_parallel(path, tiers=("shopify",), workers=4, min_interval=0.0)
    assert len(results) == 6
    assert sum(r["products"] for r in results) == 30

    v = db_mod.connect(path)
    assert v.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 30  # no lost writes
    assert v.execute("SELECT COUNT(DISTINCT supplier_domain) FROM products").fetchone()[0] == 6
    # last_harvest / last_yield recorded per supplier
    assert v.execute("SELECT COUNT(*) FROM suppliers WHERE last_harvest IS NOT NULL").fetchone()[0] == 6
    assert v.execute("SELECT last_yield FROM suppliers WHERE domain='d0.com'").fetchone()[0] == 5
    v.close()


def test_parallel_isolates_supplier_crash(tmp_path, monkeypatch):
    path = tmp_path / "catalog.db"
    c = db_mod.connect(path); db_mod.migrate(c)
    c.execute("INSERT INTO suppliers(brand,domain,categories,status,scrape_tier) "
              "VALUES('Good','good.com','x','active','shopify')")
    c.execute("INSERT INTO suppliers(brand,domain,categories,status,scrape_tier) "
              "VALUES('Bad','bad.com','x','active','shopify')")
    c.commit(); c.close()

    def maybe_crash(conn, fetcher, *, domain, brand, categories, **kw):
        if domain == "bad.com":
            raise RuntimeError("boom")
        return _fake_harvester(conn, fetcher, domain=domain, brand=brand, categories=categories)

    monkeypatch.setitem(par.DISPATCH, "shopify", maybe_crash)
    results = par.harvest_parallel(path, tiers=("shopify",), workers=2, min_interval=0.0)
    assert len(results) == 2
    v = db_mod.connect(path)
    # good supplier still harvested; bad one quarantined, sweep survived
    assert v.execute("SELECT COUNT(*) FROM products WHERE supplier_domain='good.com'").fetchone()[0] == 5
    assert v.execute("SELECT COUNT(*) FROM quarantine WHERE source_url='bad.com'").fetchone()[0] == 1
    v.close()
