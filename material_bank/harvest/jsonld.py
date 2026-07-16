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
MAX_CHILD_SITEMAPS = 1000  # effectively unbounded — enumerate every child sitemap


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


def _meta(html: str, prop: str) -> str | None:
    """Extract an og:/meta content value (property or name, either attr order)."""
    p = re.escape(prop)
    m = (re.search(rf'(?:property|name)=["\']{p}["\'][^>]*content=["\']([^"\']+)', html, re.I)
         or re.search(rf'content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{p}["\']', html, re.I))
    return m.group(1).strip() if m else None


def _h1(html: str) -> str | None:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    if not m:
        return None
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    return txt or None


def _looks_like_code(s: str) -> bool:
    """A SKU/part-number masquerading as a title: a single token containing a digit,
    or an all-caps token (e.g. 'RTM-06-0001', 'BL-20(EM)-0001'). Real titles have
    spaces + lowercase words ('handknotted table mats')."""
    s = (s or "").strip()
    if not s:
        return True
    if " " in s:
        return False
    return any(c.isdigit() for c in s) or s.isupper()


def _best_title(node: dict, html: str, sku: str) -> str | None:
    """The JSON-LD name is authoritative UNLESS the site dumped the SKU there (jaipur-
    rugs et al.). Then fall back to the page's <h1>, then og:title — the human name is
    right there, we just weren't reading it."""
    name = (node.get("name") or "").strip()
    if name and not _looks_like_code(name) and name.lower() != (sku or "").strip().lower():
        return name
    for cand in (_h1(html), _meta(html, "og:title")):
        cand = (cand or "").strip()
        if cand and not _looks_like_code(cand):
            return cand
    return name or None


def _html_price(html: str):
    """Price from HTML when JSON-LD carries no offer: price meta tags, microdata, then
    a conservative ₹/Rs/INR-prefixed number. Returns None if nothing credible (a truly
    unpriced page stays honestly unpriced — never fabricate)."""
    for prop in ("product:price:amount", "og:price:amount"):
        v = _meta(html, prop)
        if v:
            try:
                f = float(v.replace(",", "").replace("₹", "").strip())
                if f > 0:
                    return f
            except ValueError:
                pass
    m = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([\d.,]+)', html, re.I)
    if not m:
        m = re.search(r"(?:₹|&#8377;|\bRs\.?\s|\bINR\s)\s*([\d][\d,]{2,})", html)
    if m:
        try:
            f = float(m.group(1).replace(",", ""))
            if f > 0:
                return f
        except ValueError:
            pass
    return None


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
    sku = (node.get("sku") or node.get("productID") or node.get("mpn") or "").strip() or _slug(url)
    title = _best_title(node, html, sku)              # JSON-LD name, else <h1>/og:title
    if not title or is_placeholder_title(title):
        return None
    price, unit = _parse_price(node)
    if price is None:                                 # no JSON-LD offer -> try the HTML
        price = _html_price(html)
    product = build_product(
        brand=_brand_of(node, brand), sku=str(sku), title=title, category=category,
        source=url, image_url=_first_image(node) or _meta(html, "og:image"), price_unit=unit)
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
    # Drop asset URLs (Magento sitemaps mix product-image URLs into the set).
    locs = [u for u in locs if not u.lower().split("?")[0].endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".pdf", ".css", ".js"))]
    # Prefer product-looking URLs; if the site uses flat slugs, keep all.
    prod = [u for u in locs if any(h in u.lower() for h in _PRODUCT_HINTS)]
    return prod if len(prod) >= 10 else locs


def _already(conn: sqlite3.Connection, domain: str) -> set[str]:
    # Exact resume by the product's source_url (works for specs-only too), plus
    # priced observations' source_url as a fallback for pre-source_url rows.
    return {r[0] for r in conn.execute(
        "SELECT source_url FROM products WHERE supplier_domain=? AND source_url IS NOT NULL",
        (domain,))} | {r[0] for r in conn.execute(
        "SELECT source_url FROM price_observation WHERE source=?", (domain,))}


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
    refresh: bool = False,
    on_item=None,
) -> dict:
    host = base_host or domain
    sitemap_url = sitemap_url or f"https://{host}/sitemap.xml"
    urls = enumerate_pdp_urls(fetcher, sitemap_url)
    # refresh=True re-fetches + re-parses every PDP (upsert UPDATES title/price/image in
    # place) — used to re-apply an improved extractor to already-harvested rows.
    seen = set() if refresh else _already(conn, domain)
    # reachable = the sitemap yielded PDPs, or we've harvested this domain before.
    reachable = len(urls) > 0 or bool(seen)
    urls = [u for u in urls if u not in seen]  # exact source_url resume
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
