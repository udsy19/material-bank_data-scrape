"""Generic Shopify/Woo harvesters + common builder + registry driver (offline)."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from material_bank import db as db_mod
from material_bank.fetch import FetchResult
from material_bank.harvest.common import build_product
from material_bank.harvest.run import harvest_registry
from material_bank.harvest.shopify import harvest_shopify
from material_bank.harvest.woocommerce import _price_inr, harvest_woo
from material_bank.models import PriceUnit

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


# --- common.build_product ----------------------------------------------------

def test_build_product_non_surface_needs_no_units():
    p = build_product(brand="Nilkamal", sku="1", title="Chair", category="furniture",
                      source="x", price_unit=None)
    assert p.missing == [] and p.category == "furniture"


def test_build_product_surface_flags_missing_units():
    p = build_product(brand="X", sku="1", title="Tile", category="tiles", source="x",
                      size_mm="600x600")  # finish/price_unit/coverage absent
    assert set(p.missing) == {"price_unit", "coverage_sqft_per_box", "finish"}
    assert "size_mm" in p.provenance


# --- Shopify -----------------------------------------------------------------

def _is_first_page(url: str) -> bool:
    """True for the working_base probe (limit/per_page=1) or the first real page."""
    q = parse_qs(urlparse(url).query)
    if q.get("limit") == ["1"] or q.get("per_page") == ["1"]:
        return True
    return q.get("page", ["1"]) == ["1"]


class _PagedShopify:
    """Serves a products.json fixture on page 1, empty after."""

    def __init__(self, products):
        self._products = products

    def get(self, url):
        body = json.dumps({"products": self._products if _is_first_page(url) else []})
        return FetchResult(requested_url=url, status_code=200, text=body, final_url=url)


def test_shopify_harvest_from_fixture(conn):
    products = json.loads((FIX / "shopify_products.json").read_text())["products"]
    stats = harvest_shopify(conn, _PagedShopify(products),
                            domain="nilkamalfurniture.com", brand="Nilkamal",
                            categories="furniture")
    assert stats["reachable"] and stats["products"] >= 1
    row = conn.execute("SELECT brand, image_url, supplier_domain FROM products LIMIT 1").fetchone()
    assert row["brand"] == "Nilkamal" and row["supplier_domain"] == "nilkamalfurniture.com"
    # every stored product has a positive-price observation
    n_obs = conn.execute("SELECT COUNT(*) FROM price_observation").fetchone()[0]
    assert n_obs == stats["priced"] and stats["priced"] > 0


def test_shopify_skips_zero_price_variants(conn):
    products = [{
        "title": "Sample", "handle": "sample", "images": [],
        "variants": [{"id": 1, "sku": "S1", "price": "0.00", "title": "Default Title"},
                     {"id": 2, "sku": "S2", "price": "499.00", "title": "Default Title"}],
    }]
    stats = harvest_shopify(conn, _PagedShopify(products), domain="x.com",
                            brand="X", categories="decor")
    assert stats["skipped_zero"] == 1 and stats["priced"] == 1


def test_shopify_variant_becomes_distinct_sku(conn):
    products = [{
        "title": "Study Table", "handle": "study", "images": [{"src": "https://img/x.jpg"}],
        "variants": [{"id": 10, "sku": "RED", "price": "2370", "title": "Red"},
                     {"id": 11, "sku": "GRN", "price": "2400", "title": "Green"}],
    }]
    harvest_shopify(conn, _PagedShopify(products), domain="x.com", brand="X", categories="furniture")
    titles = sorted(r[0] for r in conn.execute("SELECT title FROM products"))
    assert titles == ["Study Table - Green", "Study Table - Red"]


# --- WooCommerce -------------------------------------------------------------

def test_woo_price_minor_units():
    assert _price_inr({"price": "250000", "currency_minor_unit": 2}) == 2500.0
    assert _price_inr({"price": "500", "currency_minor_unit": 0}) == 500.0
    assert _price_inr({}) is None


class _PagedWoo:
    def __init__(self, products):
        self._products = products

    def get(self, url):
        body = json.dumps(self._products if _is_first_page(url) else [])
        return FetchResult(requested_url=url, status_code=200, text=body, final_url=url)


def test_woo_harvest(conn):
    products = [
        {"id": 1, "name": "Ergo Chair", "sku": "EC1", "permalink": "https://w/ec1",
         "prices": {"price": "899900", "currency_minor_unit": 2},
         "images": [{"src": "https://img/ec1.jpg"}]},
        {"id": 2, "name": "No Price Item", "sku": "NP", "permalink": "https://w/np",
         "prices": {}, "images": []},
    ]
    stats = harvest_woo(conn, _PagedWoo(products), domain="ergosphere.in",
                        brand="Ergosphere", categories="office_furniture")
    assert stats["products"] == 2 and stats["priced"] == 1 and stats["no_price"] == 1
    obs = conn.execute("SELECT price_inr FROM price_observation").fetchone()
    assert obs[0] == 8999.0


# --- registry driver ---------------------------------------------------------

def test_registry_driver_dispatches_by_tier(conn, monkeypatch):
    db_mod.seed(conn, suppliers=None) if False else None
    conn.execute("INSERT INTO suppliers(brand,domain,categories,status,scrape_tier) "
                 "VALUES('Shop','shop.com','decor','active','shopify')")
    conn.execute("INSERT INTO suppliers(brand,domain,categories,status,scrape_tier) "
                 "VALUES('Woo','woo.com','decor','active','woocommerce')")
    conn.execute("INSERT INTO suppliers(brand,domain,categories,status,scrape_tier) "
                 "VALUES('Tier3','t3.com','decor','active','tier3')")  # not dispatched
    conn.commit()

    calls = []
    import material_bank.harvest.run as run
    monkeypatch.setattr(run, "harvest_shopify",
                        lambda *a, **k: calls.append(("shopify", k["domain"])) or {"products": 2, "reachable": True})
    monkeypatch.setattr(run, "harvest_woo",
                        lambda *a, **k: calls.append(("woo", k["domain"])) or {"products": 1, "reachable": True})
    run.DISPATCH["shopify"] = run.harvest_shopify
    run.DISPATCH["woocommerce"] = run.harvest_woo

    results = harvest_registry(conn, tiers=("shopify", "woocommerce"))
    assert ("shopify", "shop.com") in calls and ("woo", "woo.com") in calls
    assert not any(d == "t3.com" for _, d in calls)      # tier3 skipped
    # last_harvest recorded
    assert conn.execute("SELECT last_harvest FROM suppliers WHERE domain='shop.com'").fetchone()[0]
