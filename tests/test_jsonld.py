"""Regression tests for JSON-LD detection fixes found on live Orientbell.

1. Strict json.loads drops ld+json blocks with raw control chars — must parse
   leniently.
2. Category/listing pages carry ItemList -> ListItem links to PDPs; the priced
   Product lives on the PDP, so the probe must hop once from listing to PDP.
"""

import json

from material_bank.models import PricePublished, ScrapeTier
from material_bank.probe import (
    _extract_jsonld_products,
    _itemlist_urls,
    classify,
)

from .conftest import FakeFetcher, urlset

ROBOTS_OK = {"status": 200, "text": "User-agent: *\nAllow: /\nSitemap: https://d.com/sitemap.xml"}


def test_lenient_parse_recovers_control_char_block():
    # A raw newline inside a JSON string value breaks strict json.loads.
    html = (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Line1\nLine2",'
        '"offers":{"@type":"Offer","price":"84","priceCurrency":"INR"}}'
        "</script>"
    )
    prods = _extract_jsonld_products(html)
    assert len(prods) == 1
    assert prods[0]["offers"]["price"] == "84"


def test_itemlist_urls_extracted():
    node = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "url": "https://d.com/tile-a"},
            {"@type": "ListItem", "position": 2, "item": {"url": "https://d.com/tile-b"}},
        ],
    }
    html = f'<script type="application/ld+json">{json.dumps(node)}</script>'
    assert _itemlist_urls(html) == ["https://d.com/tile-a", "https://d.com/tile-b"]


def _product_html(price="84"):
    node = {"@context": "https://schema.org", "@type": "Product", "name": "Tile",
            "offers": {"@type": "Offer", "price": price, "priceCurrency": "INR"}}
    return f'<script type="application/ld+json">{json.dumps(node)}</script>'


def _itemlist_html(pdp_url):
    node = {"@type": "ItemList",
            "itemListElement": [{"@type": "ListItem", "url": pdp_url}]}
    return f'<script type="application/ld+json">{json.dumps(node)}</script>'


def test_jsonld_hops_from_listing_to_pdp():
    # Mirrors Orientbell: sitemap -> category(ItemList) -> PDP(Product w/ price).
    f = FakeFetcher(
        landing={"status": 200, "text": "<html>home</html>"},
        robots=ROBOTS_OK,
        products_json={"status": 404},
        woo={"status": 404},
        sitemap={"status": 200, "text": urlset(["https://d.com/tiles/wall"])},
        pages={
            # the sitemap's first loc is a listing page carrying an ItemList...
            "https://d.com/tiles/wall": {"status": 200, "text": _itemlist_html("https://d.com/pdp-x")},
            # ...which links to the PDP that carries the priced Product.
            "https://d.com/pdp-x": {"status": 200, "text": _product_html("84")},
        },
    )
    r = classify("d.com", f)
    assert r.scrape_tier is ScrapeTier.JSONLD
    assert r.price_published is PricePublished.YES
    assert any(e.get("source") == "itemlist-pdp" for e in r.log if e["step"] == "jsonld")
