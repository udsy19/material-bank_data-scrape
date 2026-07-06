"""tier3 extraction logic — offline (render() is exercised in the live smoke)."""

import json

import pytest

from material_bank.harvest.tier3 import (
    enumerate_pdp_urls,
    extract,
    is_pdp_url,
    slug_of,
)


def test_slug_and_pdp_filter():
    assert slug_of("https://www.kajariaceramics.com/products/terrazo-brown") == "terrazo-brown"
    assert is_pdp_url("https://x.com/products/abc", "/products/")
    assert not is_pdp_url("https://x.com/blog/products-guide", "/products/")
    assert not is_pdp_url("https://x.com/tiles-sitemap.xml", "/products/")


def test_extract_jsonld_when_present():
    node = {"@type": "Product", "brand": "Somany", "name": "Rustic Wood Plank",
            "image": ["https://img/x.jpg"]}
    rendered = {"html": f'<script type="application/ld+json">{json.dumps(node)}</script>',
                "title": "ignored", "image": "https://fallback/y.jpg"}
    p = extract(rendered, "https://somany.com/products/rustic-wood", brand="Somany", category="tiles")
    assert p.brand == "Somany" and p.title == "Rustic Wood Plank"
    assert p.image_url == "https://img/x.jpg" and p.sku == "rustic-wood"


def test_extract_specs_only_fallback():
    # no JSON-LD -> honest specs-only: title + image, surface fields flagged missing
    rendered = {"html": "<html><h1>Terrazo Brown</h1></html>",
                "title": "Terrazo Brown", "image": "https://k/storage/product/KPVT00131_b.jpg"}
    p = extract(rendered, "https://kajaria.com/products/terrazo-brown", brand="Kajaria", category="tiles")
    assert p.title == "Terrazo Brown" and p.brand == "Kajaria"
    assert p.image_url.endswith("KPVT00131_b.jpg")
    # surface with no specs -> all four flagged missing, none fabricated
    assert set(p.missing) == {"price_unit", "coverage_sqft_per_box", "size_mm", "finish"}


def test_extract_returns_none_without_title():
    assert extract({"html": "", "title": "", "image": None},
                   "https://x/products/z", brand="X", category="tiles") is None


def test_extract_skips_placeholder_title():
    assert extract({"html": "", "title": "Test", "image": None},
                   "https://x/products/test", brand="X", category="tiles") is None


class _SitemapFetcher:
    def __init__(self, xml):
        self._xml = xml

    def get(self, url):
        from material_bank.fetch import FetchResult
        return FetchResult(requested_url=url, status_code=200, text=self._xml, final_url=url)


def test_drop_shared_default_images():
    from material_bank import db as db_mod
    from material_bank.harvest.common import build_product
    from material_bank.harvest.tier3 import drop_shared_default_images
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    c = db_mod.connect(path); db_mod.migrate(c)
    # 4 products share a default image; 1 has a distinct image
    for i in range(4):
        db_mod.upsert_product(c, build_product(brand="Kajaria", sku=f"d{i}", title=f"Tile {i}",
            category="tiles", source="u", image_url="https://k/default.jpg"), supplier_domain="k.com")
    db_mod.upsert_product(c, build_product(brand="Kajaria", sku="real", title="Real",
        category="tiles", source="u", image_url="https://k/real.jpg"), supplier_domain="k.com")
    dropped = drop_shared_default_images(c, "k.com", threshold=3)
    assert dropped == 1
    assert c.execute("SELECT COUNT(*) FROM products WHERE image_url IS NULL").fetchone()[0] == 4
    assert c.execute("SELECT image_url FROM products WHERE sku='real'").fetchone()[0] == "https://k/real.jpg"
    c.close(); os.remove(path)


def test_enumerate_filters_to_pdps():
    xml = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>https://k.com/products/a</loc></url>'
           '<url><loc>https://k.com/blog/x</loc></url>'
           '<url><loc>https://k.com/products/b</loc></url></urlset>')
    urls = enumerate_pdp_urls(_SitemapFetcher(xml), "https://k.com/sitemap.xml")
    assert urls == ["https://k.com/products/a", "https://k.com/products/b"]
