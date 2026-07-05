"""Generic WooCommerce harvester — one parser for every woo-tier supplier.

Reads the public Store API (``/wp-json/wc/store/v1/products``, paginated). Woo
quotes prices in minor units (e.g. "250000" at currency_minor_unit=2 = ₹2500.00).
"""

from __future__ import annotations

import json
import sqlite3

from .. import db
from ..fetch import Fetcher
from ..models import PriceBasis, PriceObservation
from .common import build_product, is_placeholder_title
from .shopify import working_base

PER_PAGE = 100
MAX_PAGES = 400
API_PATHS = ("/wp-json/wc/store/v1/products", "/wp-json/wc/store/products")


def _price_inr(prices: dict) -> float | None:
    if not isinstance(prices, dict):
        return None
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    try:
        minor = int(prices.get("currency_minor_unit", 2))
        return int(raw) / (10 ** minor)
    except (TypeError, ValueError):
        return None


def _parse_products(payload: str):
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    return data if isinstance(data, list) else data.get("data") if isinstance(data, dict) else None


def harvest_woo(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    *,
    domain: str,
    brand: str,
    categories: str,
    max_pages: int = MAX_PAGES,
    on_item=None,
) -> dict:
    base, api = None, None
    for path in API_PATHS:
        base = working_base(fetcher, domain, f"{path}?per_page=1")
        if base:
            api = path
            break
    stats = {"domain": domain, "pages": 0, "products": 0, "priced": 0,
             "no_price": 0, "quarantined": 0, "reachable": base is not None}
    if not base:
        db.quarantine(conn, stage="harvest", source_url=f"https://{domain}{API_PATHS[0]}",
                      reason="woo store API not reachable", raw_ref=None)
        return stats

    for page in range(1, max_pages + 1):
        r = fetcher.get(f"{base}{api}?per_page={PER_PAGE}&page={page}")
        if not r.ok:
            break
        products = _parse_products(r.text)
        if products is None:
            db.quarantine(conn, stage="parse", source_url=r.final_url or "",
                          reason="store API not JSON list", raw_ref=r.raw_path)
            stats["quarantined"] += 1
            break
        if not products:
            break
        stats["pages"] += 1
        for prod in products:
            if not isinstance(prod, dict):
                continue
            if is_placeholder_title(prod.get("name")):
                continue  # vendor demo product, not a real SKU
            sku = (prod.get("sku") or "").strip() or f"woo-{prod.get('id')}"
            images = prod.get("images") or []
            img = images[0].get("src") if images and isinstance(images[0], dict) else None
            product_obj = build_product(
                brand=brand, sku=sku, title=(prod.get("name") or "").strip(),
                category=categories, source=domain, image_url=img,
            )
            price = _price_inr(prod.get("prices") or {})
            try:
                pid = db.upsert_product(conn, product_obj, supplier_domain=domain)
            except Exception as exc:
                db.quarantine(conn, stage="normalize", source_url=prod.get("permalink", ""),
                              reason=f"{type(exc).__name__}: {exc}", raw_ref=None)
                stats["quarantined"] += 1
                continue
            stats["products"] += 1
            if price and price > 0:
                db.add_price_observation(conn, pid, PriceObservation(
                    source=domain, price_inr=price, price_unit=None,
                    basis=PriceBasis.LISTED_MRP, observed_at=db.now_iso(),
                    source_url=prod.get("permalink", "") or ""))
                stats["priced"] += 1
            else:
                stats["no_price"] += 1
            if on_item:
                on_item(product_obj, price)
    return stats
