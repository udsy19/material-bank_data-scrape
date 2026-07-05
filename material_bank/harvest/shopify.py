"""Generic Shopify harvester — one parser for every shopify-tier supplier.

Registry-driven: the probe tags a domain ``shopify``; this reads its public
``/products.json`` (paginated), emitting one product per purchasable variant
(distinct SKU, distinct price). Images and prices come straight from the API,
so no per-PDP fetch is needed. ₹0 variants are treated as samples and skipped.
"""

from __future__ import annotations

import json
import sqlite3

from .. import db
from ..fetch import Fetcher
from ..models import PriceBasis, PriceObservation
from .common import build_product, is_placeholder_title

PER_PAGE = 250
MAX_PAGES = 400  # safety bound (~100k products)


def working_base(fetcher: Fetcher, domain: str, probe_path: str) -> str | None:
    """Return the https base (bare or www) where ``probe_path`` returns JSON."""
    hosts = [domain] if domain.startswith("www.") else [domain, f"www.{domain}"]
    for host in hosts:
        r = fetcher.get(f"https://{host}{probe_path}")
        if r.ok and r.text.strip().startswith(("{", "[")):
            return f"https://{host}"
    return None


def _variant_products(product: dict, *, brand: str, category: str, source: str):
    prod_img = None
    images = product.get("images") or []
    if images and isinstance(images[0], dict):
        prod_img = images[0].get("src")
    title = (product.get("title") or "").strip()
    if is_placeholder_title(title):
        return  # vendor demo product ("Example product"), not a real SKU

    for v in product.get("variants") or []:
        try:
            price = float(v.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            yield None, None  # ₹0 sample variant — signal skip
            continue
        sku = (v.get("sku") or "").strip() or f"shopify-{v.get('id')}"
        vtitle = (v.get("title") or "").strip()
        full_title = title if vtitle in ("", "Default Title") else f"{title} - {vtitle}"
        img = None
        feat = v.get("featured_image")
        if isinstance(feat, dict):
            img = feat.get("src")
        product_obj = build_product(
            brand=brand, sku=sku, title=full_title, category=category,
            source=source, image_url=img or prod_img,
        )
        obs = PriceObservation(
            source=source, price_inr=price, price_unit=None,
            basis=PriceBasis.LISTED_MRP, observed_at=db.now_iso(),
            source_url=f"https://{source}/products/{product.get('handle', '')}",
        )
        yield product_obj, obs


def harvest_shopify(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    *,
    domain: str,
    brand: str,
    categories: str,
    max_pages: int = MAX_PAGES,
    on_item=None,
) -> dict:
    base = working_base(fetcher, domain, "/products.json?limit=1")
    stats = {"domain": domain, "pages": 0, "products": 0, "priced": 0,
             "skipped_zero": 0, "quarantined": 0, "reachable": base is not None}
    if not base:
        db.quarantine(conn, stage="harvest", source_url=f"https://{domain}/products.json",
                      reason="products.json not reachable", raw_ref=None)
        return stats

    for page in range(1, max_pages + 1):
        r = fetcher.get(f"{base}/products.json?limit={PER_PAGE}&page={page}")
        if not r.ok:
            break
        try:
            products = json.loads(r.text).get("products", [])
        except (ValueError, AttributeError):
            db.quarantine(conn, stage="parse", source_url=r.final_url or "",
                          reason="products.json not JSON", raw_ref=r.raw_path)
            stats["quarantined"] += 1
            break
        if not products:
            break
        stats["pages"] += 1
        for product in products:
            for prod, obs in _variant_products(product, brand=brand,
                                               category=categories, source=domain):
                if prod is None:
                    stats["skipped_zero"] += 1
                    continue
                try:
                    pid = db.upsert_product(conn, prod, supplier_domain=domain)
                    db.add_price_observation(conn, pid, obs)
                except Exception as exc:
                    db.quarantine(conn, stage="normalize", source_url=obs.source_url,
                                  reason=f"{type(exc).__name__}: {exc}", raw_ref=None)
                    stats["quarantined"] += 1
                    continue
                stats["products"] += 1
                stats["priced"] += 1
                if on_item:
                    on_item(prod, obs)
    return stats
