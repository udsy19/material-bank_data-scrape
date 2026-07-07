"""Steelcase (asia-en) catalog harvester — static og-metadata.

The real Steelcase catalog (seating systems, sofas, desks, storage) is JS-
rendered with no API/JSON-LD, but each product page carries clean Open Graph
metadata (og:title, og:image) and its category in the URL. We harvest that
statically. Specs-only: Steelcase is B2B/quote-based and publishes no price, so
none is invented. (Separate from in.steelcase.com, which is the refurb shop.)
"""

from __future__ import annotations

import html
import re
import sqlite3
from urllib.parse import urlparse

from .. import db
from ..fetch import Fetcher
from ..sitemap import parse_sitemap
from .common import build_product, is_placeholder_title

SITEMAP = "https://www.steelcase.com/asia-en/product-sitemap.xml"
BRAND = "Steelcase"
_PRODUCT_RE = re.compile(r"/products/[a-z0-9-]+/[a-z0-9-]+/?$", re.I)
_OG_TITLE_RE = re.compile(r'og:title[^>]*content=["\']([^"\']+)', re.I)
_OG_IMAGE_RE = re.compile(r'og:image[^>]*content=["\']([^"\']+)', re.I)
_TITLE_RE = re.compile(r"<title>([^<]+)</title>", re.I)
_SUFFIX_RE = re.compile(r"\s*[-|]\s*Steelcase\s*$", re.I)


def slug_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def category_of(url: str) -> str:
    segs = [s for s in urlparse(url).path.split("/") if s]
    sub = segs[-2] if len(segs) >= 2 else ""
    return f"office_furniture|{sub}" if sub else "office_furniture"


def is_product(url: str) -> bool:
    return bool(_PRODUCT_RE.search(url))


def enumerate_products(fetcher: Fetcher) -> list[str]:
    r = fetcher.get(SITEMAP)
    if not r.ok:
        return []
    _, locs = parse_sitemap(r.text)
    return [u for u in locs if is_product(u)]


def _clean_name(raw: str) -> str:
    return _SUFFIX_RE.sub("", html.unescape(raw or "")).strip()


def parse_product(page_html: str, url: str):
    m = _OG_TITLE_RE.search(page_html) or _TITLE_RE.search(page_html)
    name = _clean_name(m.group(1)) if m else ""
    if not name or is_placeholder_title(name):
        return None
    img = _OG_IMAGE_RE.search(page_html)
    return build_product(
        brand=BRAND, sku=slug_of(url), title=name, category=category_of(url),
        source=url, image_url=img.group(1) if img else None)


def _already(conn: sqlite3.Connection, domain: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT source_url FROM products WHERE supplier_domain=? AND source_url IS NOT NULL",
        (domain,))}


def harvest_steelcase(conn: sqlite3.Connection, fetcher: Fetcher, *,
                      domain: str = "steelcase.com", brand: str = BRAND,
                      categories: str = "office_furniture", limit: int | None = None,
                      on_item=None, **_) -> dict:
    urls = enumerate_products(fetcher)
    seen = _already(conn, domain)
    reachable = len(urls) > 0 or bool(seen)
    urls = [u for u in urls if u not in seen]
    if limit is not None:
        urls = urls[:limit]

    stats = {"domain": domain, "candidates": len(urls), "products": 0,
             "quarantined": 0, "reachable": reachable}
    for url in urls:
        r = fetcher.get(url)
        if not r.ok:
            stats["quarantined"] += 1
            continue
        try:
            product = parse_product(r.text, url)
        except Exception as exc:
            db.quarantine(conn, stage="parse", source_url=url,
                          reason=f"{type(exc).__name__}: {exc}", raw_ref=None)
            stats["quarantined"] += 1
            continue
        if product is None:
            continue
        db.upsert_product(conn, product, supplier_domain=domain)
        stats["products"] += 1
        if on_item:
            on_item(product)
    return stats
