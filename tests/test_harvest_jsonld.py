import json

import pytest

from material_bank import db as db_mod
from material_bank.fetch import FetchResult
from material_bank.harvest.jsonld import (
    _brand_of,
    _parse_price,
    enumerate_pdp_urls,
    harvest_jsonld,
    parse_pdp,
)
from material_bank.models import PriceUnit


def _ld(node):
    return f'<script type="application/ld+json">{json.dumps(node)}</script>'


def test_parse_price_formats():
    assert _parse_price({"offers": {"price": "1,999.00"}})[0] == 1999.0
    assert _parse_price({"offers": {"price": "₹ 84", "itemOffered": "/sqft"}}) == (84.0, PriceUnit.PER_SQFT)
    assert _parse_price({"offers": {"price": "0"}}) == (None, None)      # zero skipped
    assert _parse_price({"offers": [{"price": "50"}]})[0] == 50.0        # list offers
    assert _parse_price({}) == (None, None)


def test_brand_of():
    assert _brand_of({"brand": {"name": "Somany"}}, "X") == "Somany"
    assert _brand_of({"brand": "Wakefit"}, "X") == "Wakefit"
    assert _brand_of({}, "Fallback") == "Fallback"


def test_parse_pdp_priced():
    node = {"@type": "Product", "name": "Rustic Wood Plank", "sku": "RWP1",
            "image": ["https://img/x.jpg"], "brand": "Somany",
            "offers": {"price": "63", "itemOffered": "/sqft"}}
    prod, obs = parse_pdp(_ld(node), "https://somanyceramics.com/products/rustic", brand="Somany", category="tiles")
    assert prod.sku == "RWP1" and prod.title == "Rustic Wood Plank"
    assert prod.image_url == "https://img/x.jpg" and prod.price_unit is PriceUnit.PER_SQFT
    assert obs.price_inr == 63.0 and obs.basis.value == "listed_mrp"


def test_parse_pdp_specs_only_when_no_price():
    node = {"@type": "Product", "name": "Jaipur Rug RE-1332", "sku": "RE1332",
            "image": "https://img/r.jpg"}  # no offers
    prod, obs = parse_pdp(_ld(node), "https://jaipurrugs.com/products/re-1332", brand="Jaipur Rugs", category="rugs")
    assert prod.title == "Jaipur Rug RE-1332" and obs is None  # stored, unpriced (honest)


def test_parse_pdp_uses_slug_when_no_sku():
    node = {"@type": "Product", "name": "Mirage Desk", "offers": {"price": "5000"}}
    prod, _ = parse_pdp(_ld(node), "https://modifurniture.com/products/mirage", brand="Modi", category="furniture")
    assert prod.sku == "mirage"


def test_parse_pdp_non_product_and_placeholder():
    assert parse_pdp("<html>no ld+json</html>", "https://x/p/1", brand="X", category="x") is None
    node = {"@type": "Product", "name": "Test", "offers": {"price": "1"}}
    assert parse_pdp(_ld(node), "https://x/p/test", brand="X", category="x") is None


class _F:
    def __init__(self, urls):
        self._sm = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>")

    def get(self, url):
        return FetchResult(requested_url=url, status_code=200, text=self._sm, final_url=url)


def test_enumerate_prefers_product_urls():
    urls = [f"https://s.com/products/p{i}" for i in range(12)] + ["https://s.com/about", "https://s.com/blog/x"]
    got = enumerate_pdp_urls(_F(urls), "https://s.com/sitemap.xml")
    assert all("/products/" in u for u in got) and len(got) == 12


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


class _SiteFetcher:
    """sitemap + two Product PDPs (one priced, one specs-only)."""

    def __init__(self):
        self.sm = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                   '<url><loc>https://s.com/products/a</loc></url>'
                   '<url><loc>https://s.com/products/b</loc></url></urlset>')

    def get(self, url):
        if "sitemap" in url:
            body = self.sm
        elif url.endswith("/a"):
            body = _ld({"@type": "Product", "name": "Priced Tile", "sku": "A", "offers": {"price": "99"}})
        else:
            body = _ld({"@type": "Product", "name": "Specs Tile", "sku": "B"})
        return FetchResult(requested_url=url, status_code=200, text=body, final_url=url)


def test_harvest_jsonld_end_to_end(conn):
    stats = harvest_jsonld(conn, _SiteFetcher(), domain="s.com", brand="S",
                           categories="tiles", sitemap_url="https://s.com/sitemap.xml")
    assert stats["products"] == 2 and stats["priced"] == 1
    assert conn.execute("SELECT COUNT(*) FROM price_observation").fetchone()[0] == 1


def test_specs_only_supplier_resumes_by_source_url(conn):
    """Regression: specs-only PDPs (no price obs) must still resume, not re-fetch.
    The 'Specs Tile' at /products/b has no price -> only source_url makes it skippable."""
    f = _SiteFetcher()
    harvest_jsonld(conn, f, domain="s.com", brand="S", categories="tiles",
                   sitemap_url="https://s.com/sitemap.xml")
    # source_url was stored on the specs-only product
    b = conn.execute("SELECT source_url FROM products WHERE sku='B'").fetchone()
    assert b["source_url"] == "https://s.com/products/b"
    # second run skips both (0 new candidates) — no wasteful re-fetch
    stats2 = harvest_jsonld(conn, f, domain="s.com", brand="S", categories="tiles",
                            sitemap_url="https://s.com/sitemap.xml")
    assert stats2["candidates"] == 0 and stats2["reachable"] is True
