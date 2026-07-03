from material_bank.models import PricePublished, ProbeStatus, ScrapeTier
from material_bank.probe import classify

from .conftest import (
    FakeFetcher,
    jsonld_html,
    shopify_json,
    urlset,
    woo_json,
)

ROBOTS_OK = {"status": 200, "text": "User-agent: *\nAllow: /\nSitemap: https://d.com/sitemap.xml"}


def test_shopify_with_price():
    f = FakeFetcher(
        landing={"status": 200, "text": "<html>shop</html>"},
        robots=ROBOTS_OK,
        products_json={"status": 200, "text": shopify_json("1999.00")},
        sitemap={"status": 200, "text": urlset(["https://d.com/products/a"])},
    )
    r = classify("d.com", f)
    assert r.scrape_tier is ScrapeTier.SHOPIFY
    assert r.price_published is PricePublished.YES
    assert r.probe_status is ProbeStatus.OK
    assert any(e["step"] == "shopify" and e.get("price_detected") for e in r.log)


def test_shopify_zero_variant_not_marked_priced():
    f = FakeFetcher(
        landing={"status": 200, "text": "x"},
        robots=ROBOTS_OK,
        products_json={"status": 200, "text": shopify_json("0.00")},
    )
    r = classify("d.com", f)
    assert r.scrape_tier is ScrapeTier.SHOPIFY
    assert r.price_published is PricePublished.UNKNOWN  # ₹0 sample guard


def test_woocommerce():
    f = FakeFetcher(
        landing={"status": 200, "text": "x"},
        robots=ROBOTS_OK,
        products_json={"status": 404},
        woo={"status": 200, "text": woo_json(2500)},
    )
    r = classify("d.com", f)
    assert r.scrape_tier is ScrapeTier.WOOCOMMERCE
    assert r.price_published is PricePublished.YES


def test_jsonld_from_landing_with_offer():
    f = FakeFetcher(
        landing={"status": 200, "text": jsonld_html("35.00", with_offer=True)},
        robots=ROBOTS_OK,
        products_json={"status": 404},
        woo={"status": 404},
    )
    r = classify("d.com", f)
    assert r.scrape_tier is ScrapeTier.JSONLD
    assert r.price_published is PricePublished.YES


def test_jsonld_product_without_offer_is_unknown_price():
    f = FakeFetcher(
        landing={"status": 200, "text": jsonld_html(with_offer=False)},
        robots=ROBOTS_OK,
        products_json={"status": 404},
        woo={"status": 404},
    )
    r = classify("d.com", f)
    assert r.scrape_tier is ScrapeTier.JSONLD
    assert r.price_published is PricePublished.UNKNOWN


def test_none_match_falls_to_tier3():
    f = FakeFetcher(
        landing={"status": 200, "text": "<html>plain marketing site</html>"},
        robots=ROBOTS_OK,
        products_json={"status": 404},
        woo={"status": 404},
    )
    r = classify("d.com", f)
    assert r.scrape_tier is ScrapeTier.TIER3
    assert r.probe_status is ProbeStatus.OK


def test_mixed_signals_flagged_ambiguous_not_guessed():
    # Shopify markers in HTML but /products.json blocked -> do not guess a tier.
    f = FakeFetcher(
        landing={"status": 200, "text": "<script src='//cdn.shopify.com/x.js'></script>"},
        robots=ROBOTS_OK,
        products_json={"status": 403},
        woo={"status": 404},
    )
    r = classify("d.com", f)
    assert r.probe_status is ProbeStatus.AMBIGUOUS
    assert r.scrape_tier is None  # flagged for subagent, not auto-classified


def test_blocked_landing():
    f = FakeFetcher(landing={"status": 403, "text": ""})
    r = classify("d.com", f)
    assert r.probe_status is ProbeStatus.BLOCKED
    assert r.scrape_tier is None


def test_unreachable_domain():
    f = FakeFetcher(landing={"error": "ConnectionError: dns fail"})
    r = classify("nope.invalid", f)
    assert r.probe_status is ProbeStatus.UNREACHABLE
    assert r.http_status is None


def test_robots_disallow_root_stops_probe():
    f = FakeFetcher(
        landing={"status": 200, "text": "x"},
        robots={"status": 200, "text": "User-agent: *\nDisallow: /"},
    )
    r = classify("d.com", f)
    assert r.robots_ok is False
    assert r.scrape_tier is None
    assert "/products.json" not in "".join(f.calls)  # never probed paths


def test_sku_estimate_from_urlset():
    f = FakeFetcher(
        landing={"status": 200, "text": "x"},
        robots=ROBOTS_OK,
        products_json={"status": 200, "text": shopify_json()},
        sitemap={"status": 200, "text": urlset([f"https://d.com/p/{i}" for i in range(12)])},
    )
    r = classify("d.com", f)
    assert r.sku_estimate == 12


def test_redirect_final_host_recorded():
    f = FakeFetcher(
        landing={"status": 200, "text": "x", "final_url": "https://in.roca.com/"},
        robots=ROBOTS_OK,
        products_json={"status": 404},
        woo={"status": 404},
    )
    r = classify("roca.in", f)
    assert r.final_host == "in.roca.com"
    assert any(e["step"] == "landing" and e["result"] == "redirected" for e in r.log)
