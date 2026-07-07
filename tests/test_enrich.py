"""Enrich stage: NULL-only writes, provenance, refetch jobs, planner seeding."""

import json

import pytest

from material_bank import db as db_mod
from material_bank import enrich, jobs
from material_bank.fetch import FetchResult
from material_bank.harvest.common import build_product
from material_bank.quality import score_all


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    yield c
    c.close()


def _add(conn, sku, title, category="tiles", supplier="somanyceramics.com", **kw):
    p = build_product(brand="Somany", sku=sku, title=title, category=category,
                      source=f"https://s.com/products/{sku}", **kw)
    return db_mod.upsert_product(conn, p, supplier_domain=supplier)


def test_title_pass_fills_null_only_with_provenance(conn):
    pid = _add(conn, "t1", "Emperador Marble 600x600 Glossy Grey tile")
    already = _add(conn, "t2", "Rustic 300x300 tile", size_mm="605x605", finish="Sugar")
    out = enrich.title_pass(conn)
    assert out["products_updated"] >= 1

    r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    assert r["size_mm"] == "600x600" and r["finish"] == "Glossy"
    assert r["color"] == "Grey" and r["color_family"] == "Grey"
    prov = json.loads(r["provenance"])
    assert prov["size_mm"]["basis"] == "derived"
    assert prov["size_mm"]["source"] == "extracted:title"
    assert "size_mm" not in json.loads(r["missing"])   # honest gap closed

    r2 = conn.execute("SELECT * FROM products WHERE id=?", (already,)).fetchone()
    assert r2["size_mm"] == "605x605" and r2["finish"] == "Sugar"  # harvested wins


def test_title_pass_derives_sheet_coverage_for_laminates(conn):
    pid = _add(conn, "lam1", "Walnut High Gloss Laminate 8 ft x 4 ft - 1mm",
               category="laminates|acrylic")
    enrich.title_pass(conn)
    r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    assert r["size_mm"] == "2438x1219"
    assert r["coverage_sqft_per_box"] == pytest.approx(32.0, abs=0.05)
    assert r["thickness_mm"] == 1.0


PDP = """
<html><head><script type="application/ld+json">
{"@type":"Product","name":"Rustic Wood Plank",
 "description":"Vitrified floor tile, Size 600x1200 mm, Matt finish, brown wood look.",
 "additionalProperty":[{"@type":"PropertyValue","name":"Finish","value":"Matt"}]}
</script></head><body></body></html>
"""


class _F:
    def __init__(self, fail=False):
        self.fail = fail

    def get(self, url):
        if self.fail:
            return FetchResult(requested_url=url, status_code=503, final_url=url)
        return FetchResult(requested_url=url, status_code=200, text=PDP, final_url=url)


def test_refetch_job_extracts_and_stores_description(conn):
    pid = _add(conn, "rw1", "Rustic Wood Plank")   # bare title: nothing extractable
    score_all(conn)                                 # below gate -> candidate
    stats = enrich.run_enrich_job(conn, "somanyceramics.com", _F(), limit=10)
    assert stats["candidates"] == 1 and stats["products_updated"] == 1
    r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    assert r["size_mm"] == "600x1200" and r["finish"] == "Matte"
    assert r["color_family"] == "Brown"
    assert "Vitrified floor tile" in r["description"]
    assert r["enriched_at"] is not None            # resume marker set
    prov = json.loads(r["provenance"])
    assert prov["size_mm"]["source"] == "extracted:pdp"
    # second run: nothing left to do
    assert enrich.run_enrich_job(conn, "somanyceramics.com", _F(), limit=10)["candidates"] == 0


def test_refetch_all_failed_marks_unreachable(conn):
    _add(conn, "x1", "Plain tile")
    score_all(conn)
    stats = enrich.run_enrich_job(conn, "somanyceramics.com", _F(fail=True), limit=10)
    assert stats["reachable"] is False and stats["fetch_failed"] == 1


def test_seed_enrich_jobs_from_gap(conn):
    _add(conn, "g1", "Plain tile one")
    _add(conn, "g2", "Plain tile two")
    _add(conn, "ok", "Perfect 600x600 Glossy tile", size_mm="600x600", finish="Glossy",
         image_url="https://i/x.jpg")
    score_all(conn)
    n = enrich.seed_enrich_jobs(conn)
    assert n == 1
    assert jobs.counts(conn, "enrich")["pending"] == 1


def test_planner_seeds_enrich(tmp_path, conn):
    _add(conn, "p1", "Plain tile")
    conn.close()
    from material_bank.planner import run_planner
    rep = run_planner(tmp_path / "catalog.db")
    assert rep["enrich_jobs_seeded"] == 1
