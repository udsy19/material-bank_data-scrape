"""Shared offline test doubles. No test in this suite touches the network."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from material_bank import db as db_mod
from material_bank.fetch import FetchResult


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").split(":")[0].lower().lstrip("www.")


class FakeFetcher:
    """Routes a URL to a canned response by its shape. ``get`` mirrors Fetcher."""

    def __init__(self, *, landing=None, robots=None, products_json=None,
                 woo=None, sitemap=None, product_page=None, default=None, pages=None):
        self._routes = {
            "landing": landing,
            "robots": robots,
            "products_json": products_json,
            "woo": woo,
            "sitemap": sitemap,
            "product_page": product_page,
        }
        self._pages = pages or {}   # exact-URL overrides, checked first
        self._default = default
        self.calls: list[str] = []

    def _kind(self, url: str) -> str:
        if "/products.json" in url:
            return "products_json"
        if "/wp-json" in url:
            return "woo"
        if "/robots.txt" in url:
            return "robots"
        if "sitemap" in url.lower():
            return "sitemap"
        if "/product" in url:  # a PDP url (from sitemap loc)
            return "product_page"
        return "landing"

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        spec = self._pages.get(url) or self._routes.get(self._kind(url)) or self._default
        if spec is None:
            return FetchResult(requested_url=url, status_code=404, final_url=url,
                               final_host=_host(url))
        if spec.get("error"):
            return FetchResult(requested_url=url, error=spec["error"])
        text = spec.get("text", "")
        final = spec.get("final_url", url)
        return FetchResult(
            requested_url=url,
            status_code=spec.get("status", 200),
            text=text,
            content=text.encode(),
            final_url=final,
            final_host=_host(final),
            headers=spec.get("headers", {}),
        )


def shopify_json(price="1999.00"):
    return json.dumps({"products": [{"title": "Tile", "variants": [{"price": price}]}]})


def woo_json(price=2500):
    return json.dumps([{"name": "Tap", "prices": {"price": str(price)}}])


def jsonld_html(price="35.00", with_offer=True):
    node = {"@context": "https://schema.org", "@type": "Product", "name": "Vitrified Tile"}
    if with_offer:
        node["offers"] = {"@type": "Offer", "price": price, "priceCurrency": "INR"}
    return f'<html><head><script type="application/ld+json">{json.dumps(node)}</script></head></html>'


def urlset(urls):
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def sitemapindex(urls):
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()
