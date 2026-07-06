"""Queue-driven harvest worker: retries transient failures, dead-letters the
permanently broken, isolates crashes — all through the durable queue."""

import material_bank.harvest.run as run
import material_bank.harvest.worker as worker
from material_bank import db as db_mod
from material_bank import jobs
from material_bank.harvest.common import build_product


def _seed_suppliers(path, rows):
    c = db_mod.connect(path)
    db_mod.migrate(c)
    for brand, domain, tier in rows:
        c.execute("INSERT INTO suppliers(brand,domain,categories,status,scrape_tier) "
                  "VALUES(?,?,?,?,?)", (brand, domain, "decor", "active", tier))
    c.commit(); c.close()


def test_seed_and_drain_all_success(tmp_path, monkeypatch):
    path = tmp_path / "catalog.db"
    _seed_suppliers(path, [("A", "a.com", "shopify"), ("B", "b.com", "shopify")])

    def ok(conn, fetcher, *, domain, brand, categories, **kw):
        db_mod.upsert_product(conn, build_product(brand=brand, sku=domain, title=domain,
                              category=categories, source=domain), supplier_domain=domain)
        return {"domain": domain, "products": 1, "reachable": True}
    monkeypatch.setitem(run.DISPATCH, "shopify", ok)

    c = db_mod.connect(path)
    assert worker.seed_harvest_jobs(c, tiers=("shopify",)) == 2
    c.close()

    final = worker.run_workers(path, workers=2, min_interval=0.0)
    assert final == {"pending": 0, "running": 0, "done": 2, "failed": 0}
    v = db_mod.connect(path)
    assert v.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2
    assert v.execute("SELECT last_yield FROM suppliers WHERE domain='a.com'").fetchone()[0] == 1
    v.close()


def test_transient_failure_is_retried_then_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "catalog.db"
    _seed_suppliers(path, [("Flaky", "flaky.com", "shopify")])
    calls = {"n": 0}

    def flaky(conn, fetcher, *, domain, brand, categories, **kw):
        calls["n"] += 1
        if calls["n"] < 3:               # first two attempts: unreachable
            return {"domain": domain, "products": 0, "reachable": False}
        return {"domain": domain, "products": 7, "reachable": True}
    monkeypatch.setitem(run.DISPATCH, "shopify", flaky)

    c = db_mod.connect(path); worker.seed_harvest_jobs(c, tiers=("shopify",)); c.close()
    # backoff_base=0 makes retries immediately claimable within one drain
    final = worker.run_workers(path, workers=1, min_interval=0.0, backoff_base=0)
    assert final["done"] == 1 and final["failed"] == 0
    assert calls["n"] == 3               # retried until it succeeded


def test_permanent_failure_dead_letters(tmp_path, monkeypatch):
    path = tmp_path / "catalog.db"
    _seed_suppliers(path, [("Dead", "dead.com", "shopify"), ("Good", "good.com", "shopify")])

    def handler(conn, fetcher, *, domain, brand, categories, **kw):
        if domain == "dead.com":
            raise RuntimeError("DNS gone")
        return {"domain": domain, "products": 3, "reachable": True}
    monkeypatch.setitem(run.DISPATCH, "shopify", handler)

    c = db_mod.connect(path); worker.seed_harvest_jobs(c, tiers=("shopify",)); c.close()
    final = worker.run_workers(path, workers=2, min_interval=0.0, backoff_base=0)
    # good one done; dead one exhausted its 4 attempts and dead-lettered
    assert final["done"] == 1 and final["failed"] == 1
    v = db_mod.connect(path)
    dl = jobs.dead_letters(v, "harvest")
    assert dl[0]["target"] == "dead.com" and dl[0]["attempts"] == 4
    assert "DNS gone" in dl[0]["last_error"]
    v.close()
