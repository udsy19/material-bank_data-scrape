"""Generic JSON-LD harvester — one parser for every jsonld-tier supplier.

Generalizes the Orientbell path (sitemap -> PDP -> schema.org Product) minus the
Magento-specific spec scraping: it trusts only the structured Product ld+json
(name, brand, sku, image, offers.price, category). Priced when offers carry a
positive price; otherwise stored specs-only (no fabricated price).
"""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlparse

from .. import db
from ..fetch import Fetcher
from ..models import NormalizedProduct, PriceBasis, PriceObservation, PriceUnit
from ..probe import _extract_jsonld_products, _itemlist_urls
from ..sitemap import looks_like_product_sitemap, parse_sitemap
from .common import build_product, is_placeholder_title

_UNIT_MAP = {
    "/sqft": PriceUnit.PER_SQFT, "sqft": PriceUnit.PER_SQFT,
    "/piece": PriceUnit.PER_PIECE, "/pc": PriceUnit.PER_PIECE,
    "/box": PriceUnit.PER_BOX, "/litre": PriceUnit.PER_LITRE,
}
_PRODUCT_HINTS = ("/product", "/products/", "/p/", "/item", "/shop/", "/buy/")
MAX_CHILD_SITEMAPS = 25


def _slug(url: str) -> str:
    parts = [s for s in urlparse(url).path.split("/") if s]
    return parts[-1] if parts else url


def _brand_of(node: dict, fallback: str) -> str:
    b = node.get("brand")
    if isinstance(b, dict):
        return (b.get("name") or fallback).strip()
    if isinstance(b, str) and b.strip():
        return b.strip()
    return fallback


def _first_image(node: dict) -> str | None:
    img = node.get("image")
    urls = img if isinstance(img, list) else ([img] if isinstance(img, str) else [])
    urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
    if not urls:
        return None
    jpgs = [u for u in urls if u.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp"))]
    return (jpgs or urls)[0]


def _parse_price(node: dict):
    offers = node.get("offers")
    for off in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(off, dict):
            continue
        raw = off.get("price") or off.get("lowPrice")
        unit = _UNIT_MAP.get(str(off.get("itemOffered", "")).strip().lower())
        if raw in (None, ""):
            continue
        try:
            val = float(str(raw).replace(",", "").replace("₹", "").strip())
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val, unit
    return None, None


def parse_pdp(html: str, url: str, *, brand: str, category: str):
    """Returns (product, price_obs|None) or None if not a Product page."""
    products = _extract_jsonld_products(html)
    if not products:
        return None
    node = products[0]
    title = (node.get("name") or "").strip()
    if not title or is_placeholder_title(title):
        return None
    sku = (node.get("sku") or node.get("productID") or node.get("mpn") or "").strip() or _slug(url)
    price, unit = _parse_price(node)
    product = build_product(
        brand=_brand_of(node, brand), sku=str(sku), title=title, category=category,
        source=url, image_url=_first_image(node), price_unit=unit)
    obs = None
    if price is not None:
        obs = PriceObservation(source=urlparse(url).netloc.replace("www.", ""),
                               price_inr=price, price_unit=unit, basis=PriceBasis.LISTED_MRP,
                               observed_at=db.now_iso(), source_url=url)
    return product, obs


def enumerate_pdp_urls(fetcher: Fetcher, sitemap_url: str) -> list[str]:
    r = fetcher.get(sitemap_url)
    if not r.ok:
        return []
    kind, locs = parse_sitemap(r.text)
    if kind == "index":
        children = [l for l in locs if looks_like_product_sitemap(l)] or locs
        pages: list[str] = []
        for child in children[:MAX_CHILD_SITEMAPS]:
            rc = fetcher.get(child)
            if rc.ok:
                pages += parse_sitemap(rc.text)[1]
        locs = pages
    # Prefer product-looking URLs; if the site uses flat slugs, keep all.
    prod = [u for u in locs if any(h in u.lower() for h in _PRODUCT_HINTS)]
    return prod if len(prod) >= 10 else locs


def _already(conn: sqlite3.Connection, domain: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT source_url FROM price_observation WHERE source=?", (domain,))} | \
           {r[0] for r in conn.execute(
        "SELECT sku FROM products WHERE supplier_domain=?", (domain,))}


def harvest_jsonld(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    *,
    domain: str,
    brand: str,
    categories: str,
    sitemap_url: str | None = None,
    base_host: str | None = None,
    limit: int | None = None,
    on_item=None,
) -> dict:
    host = base_host or domain
    sitemap_url = sitemap_url or f"https://{host}/sitemap.xml"
    urls = enumerate_pdp_urls(fetcher, sitemap_url)
    seen = _already(conn, domain)
    # reachable = the sitemap yielded PDPs, or we've harvested this domain before.
    reachable = len(urls) > 0 or bool(seen)
    urls = [u for u in urls if _slug(u) not in seen and u not in seen]
    if limit is not None:
        urls = urls[:limit]

    stats = {"domain": domain, "candidates": len(urls), "products": 0, "priced": 0,
             "skipped_non_product": 0, "quarantined": 0, "reachable": reachable}
    for url in urls:
        r = fetcher.get(url)
        if not r.ok:
            stats["skipped_non_product"] += 1
            continue
        try:
            parsed = parse_pdp(r.text, url, brand=brand, category=categories)
        except Exception as exc:
            db.quarantine(conn, stage="parse", source_url=url,
                          reason=f"{type(exc).__name__}: {exc}", raw_ref=r.raw_path)
            stats["quarantined"] += 1
            continue
        if parsed is None:
            stats["skipped_non_product"] += 1
            continue
        product, obs = parsed
        try:
            pid = db.upsert_product(conn, product, supplier_domain=domain)
        except Exception as exc:
            db.quarantine(conn, stage="normalize", source_url=url,
                          reason=f"{type(exc).__name__}: {exc}", raw_ref=None)
            stats["quarantined"] += 1
            continue
        stats["products"] += 1
        if obs is not None:
            db.add_price_observation(conn, pid, obs)
            stats["priced"] += 1
        if on_item:
            on_item(product, obs)
    return stats
