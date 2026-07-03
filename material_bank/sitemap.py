"""Pure sitemap XML parsing + product-URL heuristics (offline-testable)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

# NB: must not be a substring of "sitemap" (e.g. "item" is) or every sitemap matches.
PRODUCT_HINTS = ("product", "/pdp")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()  # strip XML namespace


def parse_sitemap(text: str) -> tuple[str, list[str]]:
    """Return ``(kind, locs)`` where kind is 'index' | 'urlset' | 'unknown'.

    'index' -> ``locs`` are child sitemap URLs; 'urlset' -> page URLs.
    """
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        return ("unknown", [])

    root_name = _localname(root.tag)
    locs = [
        (el.text or "").strip()
        for el in root.iter()
        if _localname(el.tag) == "loc" and el.text and el.text.strip()
    ]
    if root_name == "sitemapindex":
        return ("index", locs)
    if root_name == "urlset":
        return ("urlset", locs)
    return ("unknown", locs)


def looks_like_product_sitemap(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in PRODUCT_HINTS)
