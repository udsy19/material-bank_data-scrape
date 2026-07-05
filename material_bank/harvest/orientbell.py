"""Orientbell harvester (jsonld tier, Magento PDPs).

Priced tile anchor. Reliable per-PDP signals (verified live 2026-07-03):
  - price + currency + price_unit  <- Product ld+json offers (itemOffered '/sqft')
  - brand / title / material / image <- Product ld+json
  - sku   <- ``data-sku="..."`` (Magento)
  - size  <- Magento ``"size":"300x600"`` json
  - finish<- ``>Finish</span><a ...>Glossy Finish</a>``
  - coverage_sqft_per_box: NOT published on the PDP -> honestly flagged missing.

MRP is labelled ``listed_mrp``, never "cost". The price is written as an
observation (Stage 7), the spec as a product (Stage 3); they never mix.
"""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlparse

from .. import db
from ..fetch import Fetcher
from ..models import NormalizedProduct, PriceBasis, PriceObservation, PriceUnit
from ..probe import _extract_jsonld_products  # reuse the probe's ld+json extractor
from ..sitemap import parse_sitemap
from .common import build_product

SOURCE = "orientbell.com"
DEFAULT_SITEMAP = "https://www.orientbell.com/media/ositemap.xml"
CATEGORY = "tiles"

_SKU_RE = re.compile(r'data-sku="([^"]+)"')
_SKU_JSON_RE = re.compile(r'"sku"\s*:\s*"([^"]+)"')
_SIZE_RE = re.compile(r'"size"\s*:\s*"(\d+\s*[xX]\s*\d+)"')
_SIZE_IMG_RE = re.compile(r'(\d+x\d+)_mm')
# Magento injects `<!-- -->` comments inside the anchor; capture the whole
# inner content and clean it rather than trying to skip comments inline.
_FINISH_RE = re.compile(r'>\s*Finish\s*</span>\s*<a[^>]*>(.*?)</a>', re.S)
_TAG_OR_COMMENT_RE = re.compile(r'<!--.*?-->|<[^>]+>', re.S)
_PLACEHOLDER_RE = re.compile(r"^test\s*\d*$", re.I)  # Orientbell leaves "Test33" test SKUs live
_UNIT_MAP = {
    "/sqft": PriceUnit.PER_SQFT, "/sq ft": PriceUnit.PER_SQFT,
    "/piece": PriceUnit.PER_PIECE, "/pc": PriceUnit.PER_PIECE,
    "/box": PriceUnit.PER_BOX, "/litre": PriceUnit.PER_LITRE,
}


def is_pdp_candidate(url: str) -> bool:
    """Single-segment slug (PDPs are /slug; categories are /tiles/...).

    Not all single-segment URLs are PDPs (e.g. /about-us) — the parser confirms
    by requiring a Product ld+json, returning None otherwise.
    """
    segs = [s for s in urlparse(url).path.split("/") if s]
    return len(segs) == 1


def enumerate_pdp_urls(fetcher: Fetcher, sitemap_url: str = DEFAULT_SITEMAP) -> list[str]:
    r = fetcher.get(sitemap_url)
    if not r.ok:
        return []
    _, locs = parse_sitemap(r.text)
    return [u for u in locs if is_pdp_candidate(u)]


def _extract_price_unit(item_offered: str | None) -> PriceUnit | None:
    if not item_offered:
        return None
    return _UNIT_MAP.get(item_offered.strip().lower())


def _extract_size(html: str) -> str | None:
    m = _SIZE_RE.search(html) or _SIZE_IMG_RE.search(html)
    return re.sub(r"\s*[xX]\s*", "x", m.group(1)) if m else None


def _extract_finish(html: str) -> str | None:
    m = _FINISH_RE.search(html)
    if not m:
        return None
    val = _TAG_OR_COMMENT_RE.sub(" ", m.group(1))   # strip inner tags/comments
    val = re.sub(r"\s+", " ", val).strip()
    return re.sub(r"\s*finish$", "", val, flags=re.I) or None  # "Glossy Finish" -> "Glossy"


def _extract_sku(html: str) -> str | None:
    m = _SKU_RE.search(html) or _SKU_JSON_RE.search(html)
    return m.group(1).strip() if m else None


def _extract_image_url(product_jsonld: dict) -> str | None:
    """First product image from ld+json; prefer a real photo (.jpg) over .webp."""
    img = product_jsonld.get("image")
    urls = img if isinstance(img, list) else ([img] if isinstance(img, str) else [])
    urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
    if not urls:
        return None
    jpgs = [u for u in urls if u.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png"))]
    return (jpgs or urls)[0]


def parse_pdp(html: str, url: str) -> tuple[NormalizedProduct, PriceObservation | None] | None:
    """Parse one PDP. Returns (product, price_obs|None), or None if not a product."""
    products = _extract_jsonld_products(html)
    if not products:
        return None  # info page, not a PDP
    p = products[0]
    offers = p.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    title = (p.get("name") or "").strip()
    if _PLACEHOLDER_RE.match(title):
        return None  # vendor test/placeholder SKU (e.g. "Test33"), not a real product

    sku = _extract_sku(html)
    if not sku:
        return None  # cannot key a product without a SKU

    # coverage_sqft_per_box is never on an Orientbell PDP -> build_product flags
    # it missing (surface category), honestly and structurally.
    product = build_product(
        brand=(p.get("brand") or "Orientbell"),
        sku=sku,
        title=title,
        category=CATEGORY,
        source=url,
        image_url=_extract_image_url(p),
        size_mm=_extract_size(html),
        finish=_extract_finish(html),
        price_unit=_extract_price_unit(offers.get("itemOffered")),
        coverage_sqft_per_box=None,
    )
    price_unit = product.price_unit

    price = offers.get("price")
    obs = None
    try:
        if price is not None and float(price) > 0:
            obs = PriceObservation(
                source=SOURCE,
                price_inr=float(price),
                price_unit=price_unit,
                basis=PriceBasis.LISTED_MRP,   # published MRP, labelled as MRP
                observed_at=db.now_iso(),
                source_url=url,
            )
    except (TypeError, ValueError):
        obs = None
    return product, obs


def _already_harvested(conn: sqlite3.Connection) -> set[str]:
    return {
        r["source_url"]
        for r in conn.execute(
            "SELECT DISTINCT source_url FROM price_observation WHERE source=?", (SOURCE,)
        )
    }


def harvest(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    *,
    sitemap_url: str = DEFAULT_SITEMAP,
    limit: int | None = None,
    force: bool = False,
    urls: list[str] | None = None,
    on_item=None,
) -> dict:
    """Enumerate PDPs, parse, and store products + price observations.

    Resumable: skips URLs already observed unless ``force``. Failures are
    quarantined, never dropped. Pass ``urls`` to harvest an explicit list
    (targeted re-harvest) instead of enumerating the sitemap.
    """
    urls = list(urls) if urls is not None else enumerate_pdp_urls(fetcher, sitemap_url)
    if not force:
        seen = _already_harvested(conn)
        urls = [u for u in urls if u not in seen]
    if limit is not None:
        urls = urls[:limit]

    stats = {"candidates": len(urls), "products": 0, "priced": 0,
             "skipped_non_product": 0, "quarantined": 0}
    for url in urls:
        r = fetcher.get(url)
        if not r.ok:
            db.quarantine(conn, stage="harvest", source_url=url,
                          reason=f"fetch status {r.status_code or r.error}", raw_ref=None)
            stats["quarantined"] += 1
            continue
        try:
            parsed = parse_pdp(r.text, url)
        except Exception as exc:  # parser rot must quarantine, not crash the run
            db.quarantine(conn, stage="parse", source_url=url,
                          reason=f"{type(exc).__name__}: {exc}", raw_ref=r.raw_path)
            stats["quarantined"] += 1
            continue
        if parsed is None:
            stats["skipped_non_product"] += 1
            continue
        product, obs = parsed
        pid = db.upsert_product(conn, product, supplier_domain=SOURCE)
        stats["products"] += 1
        if obs is not None:
            db.add_price_observation(conn, pid, obs)
            stats["priced"] += 1
        if on_item:
            on_item(url, product, obs)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="mb-harvest-orientbell")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    ap.add_argument("--sitemap", default=DEFAULT_SITEMAP)
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.migrate(conn)
    fetcher = Fetcher()
    done = 0

    def progress(url, product, obs):
        nonlocal done
        done += 1
        price = f"₹{obs.price_inr:g}/{obs.price_unit.value}" if obs else "no-price"
        print(f"[{done}] {product.sku} {product.size_mm or '?':<9} {product.finish or '?':<8} "
              f"{price}  {product.title[:40]}", file=sys.stderr)

    stats = harvest(conn, fetcher, sitemap_url=args.sitemap, limit=args.limit,
                    force=args.force, on_item=progress)
    print(f"\nharvest stats: {stats}", file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
