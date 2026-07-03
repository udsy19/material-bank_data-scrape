from material_bank.sitemap import (
    looks_like_product_sitemap,
    parse_sitemap,
)

from .conftest import sitemapindex, urlset


def test_parse_urlset():
    kind, locs = parse_sitemap(urlset(["https://x.com/a", "https://x.com/b"]))
    assert kind == "urlset"
    assert locs == ["https://x.com/a", "https://x.com/b"]


def test_parse_index():
    kind, locs = parse_sitemap(sitemapindex(["https://x.com/product-sitemap1.xml"]))
    assert kind == "index"
    assert locs == ["https://x.com/product-sitemap1.xml"]


def test_parse_garbage():
    kind, locs = parse_sitemap("<html>not a sitemap</html>")
    assert kind == "unknown"
    assert locs == []


def test_parse_handles_namespaced_and_empty():
    assert parse_sitemap("")[0] == "unknown"
    kind, locs = parse_sitemap(urlset([]))
    assert kind == "urlset" and locs == []


def test_product_sitemap_heuristic():
    assert looks_like_product_sitemap("https://x.com/product-sitemap.xml")
    assert looks_like_product_sitemap("https://x.com/sitemap_products_1.xml")
    assert not looks_like_product_sitemap("https://x.com/page-sitemap.xml")
