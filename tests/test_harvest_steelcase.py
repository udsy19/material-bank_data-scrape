"""Steelcase asia-en catalog harvester — static og-metadata, specs-only."""

import pytest

from material_bank import db as db_mod
from material_bank.fetch import FetchResult
from material_bank.harvest import steelcase


def test_slug_category_product_filter():
    u = "https://www.steelcase.com/asia-en/products/sofas/montholon/"
    assert steelcase.slug_of(u) == "montholon"
    assert steelcase.category_of(u) == "office_furniture|sofas"
    assert steelcase.is_product(u)
    assert not steelcase.is_product("https://www.steelcase.com/asia-en/products/sofas/")  # category page


def test_clean_name_strips_suffix_and_entities():
    assert steelcase._clean_name("Sinum | Steelcase") == "Sinum"
    assert steelcase._clean_name("Montholon - Steelcase") == "Montholon"
    assert steelcase._clean_name("Case Sofa &amp; Combi") == "Case Sofa & Combi"


PAGE = (
    '<html><head>'
    '<meta property="og:title" content="Sinum | Steelcase">'
    '<meta property="og:image" content="https://images.steelcase.com/x/sinum.jpg">'
    '</head><body>...</body></html>'
)


def test_parse_product():
    p = steelcase.parse_product(PAGE, "https://www.steelcase.com/asia-en/products/lounge-chairs/sinum/")
    assert p.brand == "Steelcase" and p.title == "Sinum" and p.sku == "sinum"
    assert p.category == "office_furniture|lounge-chairs"
    assert p.image_url.endswith("sinum.jpg")
    assert p.source_url.endswith("/sinum/")
    # furniture (non-surface) -> no unit requirement, nothing flagged missing
    assert p.missing == []


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


def test_harvest_end_to_end(conn):
    sitemap = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<url><loc>https://www.steelcase.com/asia-en/products/lounge-chairs/sinum/</loc></url>'
               '<url><loc>https://www.steelcase.com/asia-en/products/sofas/</loc></url>'  # category, skipped
               '</urlset>')

    class _F:
        def get(self, url):
            body = sitemap if "sitemap" in url else PAGE
            return FetchResult(requested_url=url, status_code=200, text=body, final_url=url)

    stats = steelcase.harvest_steelcase(conn, _F())
    assert stats["candidates"] == 1 and stats["products"] == 1
    row = conn.execute("SELECT brand, category, source_url FROM products WHERE sku='sinum'").fetchone()
    assert row["brand"] == "Steelcase" and row["category"] == "office_furniture|lounge-chairs"
    # resumable: second run harvests nothing new
    assert steelcase.harvest_steelcase(conn, _F())["candidates"] == 0
