from material_bank.robots import parse_robots

BASE = "https://example.com/"

ROBOTS = """
User-agent: *
Disallow: /cart
Disallow: /checkout
Allow: /

Sitemap: https://example.com/sitemap.xml
Sitemap: https://example.com/product-sitemap.xml
"""


def test_parse_extracts_sitemaps():
    r = parse_robots(BASE, ROBOTS, fetched=True)
    assert r.sitemaps == [
        "https://example.com/sitemap.xml",
        "https://example.com/product-sitemap.xml",
    ]


def test_can_fetch_respects_disallow():
    r = parse_robots(BASE, ROBOTS, fetched=True)
    assert r.can_fetch("/products.json") is True
    assert r.can_fetch("/cart") is False
    assert r.can_fetch("/checkout/pay") is False


def test_absent_robots_is_permissive():
    r = parse_robots(BASE, "", fetched=False)
    assert r.can_fetch("/anything") is True
    assert r.sitemaps == []


def test_disallow_all_blocks_root():
    r = parse_robots(BASE, "User-agent: *\nDisallow: /", fetched=True)
    assert r.can_fetch("/") is False
    assert r.can_fetch("/products.json") is False
