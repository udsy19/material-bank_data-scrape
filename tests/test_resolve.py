"""Entity resolution: non-destructive variant grouping + collapsed designs."""

import pytest

from material_bank import db as db_mod
from material_bank import resolve
from material_bank.harvest.common import build_product
from material_bank.models import PriceBasis, PriceObservation
from material_bank.quality import score_all
from material_bank.retrieval import list_designs


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    yield c
    c.close()


def _add(conn, sku, title, *, brand="Wakefit", supplier="wakefit.co", price=None,
         size_mm=None, finish=None):
    pid = db_mod.upsert_product(conn, build_product(
        brand=brand, sku=sku, title=title, category="mattresses",
        source=f"https://{supplier}/p/{sku}", size_mm=size_mm, finish=finish),
        supplier_domain=supplier)
    if price is not None:
        db_mod.add_price_observation(conn, pid, PriceObservation(
            source=supplier, price_inr=price, basis=PriceBasis.LISTED_MRP,
            observed_at=db_mod.now_iso(), source_url=f"https://{supplier}/p/{sku}"))
    return pid


def test_group_key_normalizes_and_guards_generic():
    a = resolve.group_key("wakefit.co", "Wakefit", "Dual Comfort Mattress")
    b = resolve.group_key("wakefit.co", "wakefit", "dual-comfort   mattress!")
    assert a == b and a is not None          # punctuation/spacing invariant
    assert resolve.group_key("x.com", "X", "Sofa") is None   # 1 token: too generic
    # different design -> different key
    assert a != resolve.group_key("wakefit.co", "Wakefit", "Ortho Plus Mattress")


def test_assign_groups_links_variants_keeps_singletons_null(conn):
    v1 = _add(conn, "dc-single", "Dual Comfort Mattress", size_mm="1830x910")
    v2 = _add(conn, "dc-queen", "Dual Comfort Mattress", size_mm="1980x1520")
    solo = _add(conn, "ortho-1", "Ortho Plus Mattress")
    out = resolve.assign_variant_groups(conn)
    assert out["groups"] == 1 and out["grouped_products"] == 2

    g1 = conn.execute("SELECT variant_group_id FROM products WHERE id=?", (v1,)).fetchone()[0]
    g2 = conn.execute("SELECT variant_group_id FROM products WHERE id=?", (v2,)).fetchone()[0]
    gsolo = conn.execute("SELECT variant_group_id FROM products WHERE id=?", (solo,)).fetchone()[0]
    assert g1 is not None and g1 == g2         # variants share a group
    assert gsolo is None                        # singleton = its own canonical


def test_variants_of_returns_siblings_with_prices(conn):
    v1 = _add(conn, "dc-single", "Dual Comfort Mattress", size_mm="1830x910", price=8999)
    _add(conn, "dc-queen", "Dual Comfort Mattress", size_mm="1980x1520", price=14999)
    resolve.assign_variant_groups(conn)
    sibs = resolve.variants_of(conn, v1)
    assert len(sibs) == 2
    sizes = {s["attrs"].get("size_mm") for s in sibs}
    assert sizes == {"1830x910", "1980x1520"}
    assert any(s["price"] and s["price"]["price_inr"] == 14999 for s in sibs)
    # a singleton has no variant group
    solo = _add(conn, "ortho-1", "Ortho Plus Mattress")
    resolve.assign_variant_groups(conn)
    assert resolve.variants_of(conn, solo) == []


def test_list_designs_collapses_to_one_card_with_band(conn):
    _add(conn, "dc-single", "Dual Comfort Mattress", size_mm="1830x910", price=8999)
    _add(conn, "dc-queen", "Dual Comfort Mattress", size_mm="1980x1520", price=14999)
    _add(conn, "ortho-1", "Ortho Plus Mattress", price=11999)   # singleton design
    resolve.assign_variant_groups(conn)
    d = list_designs(conn)
    assert d["total"] == 2                       # 2 designs, not 3 SKUs
    top = d["items"][0]                           # most-variant design first
    assert top["title"] == "Dual Comfort Mattress"
    assert top["variant_count"] == 2
    assert top["min_price"] == 8999 and top["max_price"] == 14999
    # price-band filter matches on band overlap
    assert list_designs(conn, min_price=12000)["total"] == 1   # only dual reaches 12k
    assert list_designs(conn, max_price=9000)["total"] == 1    # only dual's low end


def test_list_designs_rolls_up_variant_axes(conn):
    _add(conn, "dc-s", "Dual Comfort Mattress", size_mm="1830x910", finish="Firm", price=8999)
    _add(conn, "dc-q", "Dual Comfort Mattress", size_mm="1980x1520", finish="Soft", price=14999)
    resolve.assign_variant_groups(conn)
    card = list_designs(conn)["items"][0]
    assert sorted(card["size_set"]) == ["1830x910", "1980x1520"]
    assert sorted(card["finish_set"]) == ["Firm", "Soft"]


def test_audit_flags_suspect_grouping(conn):
    # a generic title colliding a ₹500 item and a ₹50,000 item = >20x spread
    from material_bank.db import upsert_product
    for sku, price in [("g1", 500), ("g2", 50000)]:
        pid = _add(conn, sku, "Designer Wall Panel", price=price)
    # force them into one group id to simulate a mis-group
    conn.execute("UPDATE products SET variant_group_id='grp-suspect' "
                 "WHERE title='Designer Wall Panel'")
    conn.commit()
    audit = resolve.audit_variant_groups(conn)
    assert audit["suspect_count"] == 1


def test_list_designs_respects_publish_gate(conn):
    # unpriced -> below gate; priced+complete -> above. collapsed catalog is gated.
    _add(conn, "dc-single", "Dual Comfort Mattress", size_mm="1830x910", price=8999,
         finish="Firm")
    _add(conn, "dc-queen", "Dual Comfort Mattress", size_mm="1980x1520")   # no price
    score_all(conn)
    resolve.assign_variant_groups(conn)
    gated = list_designs(conn, publish_ready=True)
    # the design appears, but its variant_count reflects only publishable members
    assert gated["total"] == 1
    assert gated["items"][0]["variant_count"] == 1
