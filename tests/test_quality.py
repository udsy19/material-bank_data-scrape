"""Phase A trust contract: scoring, contradiction checks, publish gate, planner."""

from datetime import datetime, timedelta, timezone

import pytest

from material_bank import db as db_mod
from material_bank import quality
from material_bank.harvest.common import build_product
from material_bank.models import PriceBasis, PriceObservation, PriceUnit
from material_bank.planner import run_planner


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    yield c
    c.close()


def _add(conn, *, sku, title="Emperador Marble", category="tiles", image="https://i/x.jpg",
         size="600x600", finish="Glossy", price_unit=PriceUnit.PER_SQFT, coverage=15.0,
         price=84.0, days_ago=1, supplier="orientbell.com"):
    p = build_product(brand="Orientbell", sku=sku, title=title, category=category,
                      source=f"https://o/{sku}", image_url=image, size_mm=size,
                      finish=finish, price_unit=price_unit, coverage_sqft_per_box=coverage)
    pid = db_mod.upsert_product(conn, p, supplier_domain=supplier)
    if price is not None:
        at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        db_mod.add_price_observation(conn, pid, PriceObservation(
            source=supplier, price_inr=price, basis=PriceBasis.LISTED_MRP,
            observed_at=at, source_url=f"https://o/{sku}"))
    return pid


def _row(conn, pid):
    return conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()


def test_fully_specified_surface_scores_100_and_publishes(conn):
    pid = _add(conn, sku="perfect")
    quality.score_all(conn)
    r = _row(conn, pid)
    assert r["completeness"] == 100
    assert r["verification_tier"] == "auto_validated"
    assert r["publish_ready"] == 1


def test_surface_missing_units_scores_below_gate(conn):
    pid = _add(conn, sku="bare", size=None, finish=None, price_unit=None, coverage=None)
    quality.score_all(conn)
    r = _row(conn, pid)
    assert r["completeness"] == 70            # core only, held to the surface bar
    assert r["publish_ready"] == 1            # 70 == surface threshold exactly
    pid2 = _add(conn, sku="bare2", size=None, finish=None, price_unit=None,
                coverage=None, image=None)    # no image either -> 50
    quality.score_all(conn)
    assert _row(conn, pid2)["publish_ready"] == 0


def test_stale_price_costs_freshness(conn):
    pid = _add(conn, sku="stale", days_ago=120)   # >90d -> fresh_price weight lost
    quality.score_all(conn)
    assert _row(conn, pid)["completeness"] == 75  # 100 - 25


def test_specs_only_nonsurface_publishes_at_lower_bar(conn):
    # furniture (non-surface): core weights scaled; no price -> 45/70 -> 64 -> below 60?
    pid = _add(conn, sku="chair", category="furniture", size=None, finish=None,
               price_unit=None, coverage=None, price=None)
    quality.score_all(conn)
    r = _row(conn, pid)
    assert r["completeness"] == 64            # (70-25)/70 -> 64
    assert r["publish_ready"] == 1            # default threshold 60


def test_placeholder_title_is_unverified_never_published(conn):
    pid = _add(conn, sku="junk", title="Test")
    quality.score_all(conn)
    r = _row(conn, pid)
    assert r["verification_tier"] == "unverified" and r["publish_ready"] == 0


def test_contradictions_block_publication(conn):
    bad_size = _add(conn, sku="badsize", size="600x600")
    conn.execute("UPDATE products SET size_mm='sixteen inch' WHERE id=?", (bad_size,))
    bad_cov = _add(conn, sku="badcov")
    conn.execute("UPDATE products SET coverage_sqft_per_box=-3 WHERE id=?", (bad_cov,))
    conn.commit()
    quality.score_all(conn)
    assert _row(conn, bad_size)["verification_tier"] == "unverified"
    assert _row(conn, bad_cov)["verification_tier"] == "unverified"


def test_human_tiers_never_downgraded(conn):
    pid = _add(conn, sku="golden")
    conn.execute("UPDATE products SET verification_tier='golden' WHERE id=?", (pid,))
    conn.commit()
    quality.score_all(conn)
    assert _row(conn, pid)["verification_tier"] == "golden"


def test_snapshot_and_trend(conn):
    _add(conn, sku="a"); _add(conn, sku="b", image=None)
    quality.score_all(conn)
    n = quality.snapshot_metrics(conn)
    assert n >= 5
    trend = quality.metrics_trend(conn, "publish_ready")
    assert len(trend) == 1 and trend[0]["value"] >= 1


def test_planner_end_to_end(tmp_path, conn):
    _add(conn, sku="x"); _add(conn, sku="y", title="Test")  # one good, one junk
    conn.close()
    rep = run_planner(tmp_path / "catalog.db")
    assert rep["scored"] == 2
    assert rep["publish_ready"] == 1
    assert rep["tiers"]["unverified"] == 1
    assert rep["snapshot_rows"] >= 5
