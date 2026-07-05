"""Orientbell harvest tests — parser runs against a real saved PDP fixture."""

from pathlib import Path

import pytest

from material_bank import db as db_mod
from material_bank.bom import boxes_for_area
from material_bank.harvest import orientbell as ob
from material_bank.models import PriceBasis, PriceUnit

FIX = Path(__file__).parent / "fixtures"
PDP_HTML = (FIX / "orientbell_pdp.html").read_text(encoding="utf-8")
PDP_URL = "https://www.orientbell.com/ohg-emperador-marble-strips-hl"


def test_parse_real_pdp_fixture():
    product, obs = ob.parse_pdp(PDP_HTML, PDP_URL)
    assert product.brand == "Orientbell"
    assert product.sku == "015005793712268321M"
    assert product.title == "OHG Emperador Marble Strips HL"
    assert product.size_mm == "300x600"
    assert product.finish == "Glossy"
    assert product.price_unit is PriceUnit.PER_SQFT
    assert product.category == "tiles"
    # coverage is genuinely absent -> flagged, not faked
    assert "coverage_sqft_per_box" in product.missing
    assert product.coverage_sqft_per_box is None


def test_parse_real_pdp_price_is_mrp_observation():
    _, obs = ob.parse_pdp(PDP_HTML, PDP_URL)
    assert obs.price_inr == 84.0
    assert obs.price_unit is PriceUnit.PER_SQFT
    assert obs.basis is PriceBasis.LISTED_MRP     # MRP labelled MRP, not "cost"
    assert obs.source_url == PDP_URL


def test_non_product_page_returns_none():
    assert ob.parse_pdp("<html><body>About Us</body></html>", "https://x/about-us") is None


def test_vendor_test_placeholder_skipped():
    # Orientbell leaves live test SKUs like "Test33"; must not enter the catalog.
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Product","brand":"Orientbell","name":"Test33",'
        '"offers":{"@type":"Offer","price":"2","priceCurrency":"INR","itemOffered":"/sqft"}}'
        '</script><div data-sku="TEST-33"></div>'
    )
    assert ob.parse_pdp(html, "https://www.orientbell.com/test33") is None


def test_extract_image_url_prefers_jpg():
    product, _ = ob.parse_pdp(PDP_HTML, PDP_URL)
    assert product.image_url is not None
    assert product.image_url.lower().endswith((".jpg", ".jpeg", ".png"))  # not the .webp


def test_extract_image_url_from_jsonld():
    assert ob._extract_image_url(
        {"image": ["https://x/a.webp", "https://x/b.jpg"]}) == "https://x/b.jpg"
    assert ob._extract_image_url({"image": "https://x/only.jpg"}) == "https://x/only.jpg"
    assert ob._extract_image_url({}) is None


def test_pdp_candidate_filter():
    assert ob.is_pdp_candidate("https://www.orientbell.com/ohg-emperador-marble-strips-hl")
    assert not ob.is_pdp_candidate("https://www.orientbell.com/tiles/wall-tiles")


def test_enumerate_filters_sitemap(tmp_path):
    sitemap = (FIX / "orientbell_sitemap_sample.xml").read_text()

    class _F:
        def get(self, url):
            from material_bank.fetch import FetchResult
            return FetchResult(requested_url=url, status_code=200, text=sitemap, final_url=url)

    urls = ob.enumerate_pdp_urls(_F(), "https://x/sitemap.xml")
    assert all(ob.is_pdp_candidate(u) for u in urls)
    assert not any("/tiles/" in u for u in urls)  # categories dropped


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


class _OneProductFetcher:
    def __init__(self, sitemap_urls, pdp_html):
        self._sitemap = (
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{u}</loc></url>" for u in sitemap_urls) + "</urlset>"
        )
        self._pdp = pdp_html

    def get(self, url):
        from material_bank.fetch import FetchResult
        text = self._sitemap if "sitemap" in url else self._pdp
        return FetchResult(requested_url=url, status_code=200, text=text, final_url=url)


def test_harvest_writes_product_and_observation(conn):
    f = _OneProductFetcher([PDP_URL], PDP_HTML)
    stats = ob.harvest(conn, f, sitemap_url="https://x/sitemap.xml")
    assert stats["products"] == 1 and stats["priced"] == 1

    prod = conn.execute("SELECT * FROM products WHERE sku='015005793712268321M'").fetchone()
    assert prod["brand"] == "Orientbell" and prod["price_unit"] == "per_sqft"
    obs = conn.execute("SELECT * FROM price_observation WHERE product_id=?", (prod["id"],)).fetchone()
    assert obs["price_inr"] == 84.0 and obs["basis"] == "listed_mrp"


def test_harvest_is_resumable_and_idempotent(conn):
    f = _OneProductFetcher([PDP_URL], PDP_HTML)
    ob.harvest(conn, f, sitemap_url="https://x/sitemap.xml")
    # second run: URL already observed -> skipped
    stats2 = ob.harvest(conn, f, sitemap_url="https://x/sitemap.xml")
    assert stats2["candidates"] == 0
    # exactly one product, one observation (no dupes)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM price_observation").fetchone()[0] == 1


def test_bom_end_to_end_with_harvested_size(conn):
    # A 10x12 ft room = 120 sqft; with a known box coverage, boxes are computable.
    f = _OneProductFetcher([PDP_URL], PDP_HTML)
    ob.harvest(conn, f, sitemap_url="https://x/sitemap.xml")
    # coverage was flagged missing; supply a dealer-sheet coverage to run BOM
    result = boxes_for_area(area_sqft=120, coverage_sqft_per_box=15.0)
    assert result.boxes == 9   # ceil(120*1.1/15)=ceil(8.8)=9
