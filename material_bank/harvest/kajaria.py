"""Kajaria harvester — static PDPs + pdfplumber technical-PDF specs.

Kajaria has no e-commerce catalog/API and renders PDP specs as icon SVGs, but
the static HTML carries the product name, its image, and (for many products) a
link to a technical PDF. Those PDFs hold the real surface data — size, thickness
and coverage sq.ft/box — the exact field even Orientbell omits. We fetch PDPs
statically (no browser) and parse the PDFs with pdfplumber (MIT). Specs-only:
Kajaria publishes no price, so none is invented.
"""

from __future__ import annotations

import io
import re
import sqlite3

from .. import db
from ..fetch import Fetcher
from ..sitemap import parse_sitemap
from .common import build_product, is_placeholder_title

SITEMAP = "https://www.kajariaceramics.com/sitemap.xml"
CATEGORY = "tiles"
BRAND = "Kajaria"

_H1_RE = re.compile(r"<h1[^>]*>\s*([^<]+?)\s*</h1>", re.I)
_TITLE_RE = re.compile(r"<title>\s*([^<]+?)\s*</title>", re.I)
_PDF_RE = re.compile(r"(https://www\.kajariaceramics\.com/storage/pdf/technical-[^\"'\s]+\.pdf)", re.I)
_IMG_RE = re.compile(r"(https://www\.kajariaceramics\.com/storage/product/[^\"'\s]+\.(?:jpg|jpeg|png|webp))", re.I)
# "800x2400mm (15mm) 1 pc. 1.92 sq. mtr. / 20.666 sq.ft. 68 Kg"
_SPEC_RE = re.compile(r"(\d+\s*[xX]\s*\d+)\s*mm\s*\(([\d.]+)\s*mm\)[^\n]*?([\d.]+)\s*sq\.?\s*ft", re.I)


def slug_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def is_pdp(url: str) -> bool:
    u = url.lower()
    return "/products/" in u and not u.endswith((".xml", ".pdf"))


def enumerate_pdps(fetcher: Fetcher) -> list[str]:
    r = fetcher.get(SITEMAP)
    if not r.ok:
        return []
    _, locs = parse_sitemap(r.text)
    return [u for u in locs if is_pdp(u)]


def specs_from_text(txt: str) -> dict:
    """Pure: pull size_mm / thickness_mm / coverage_sqft_per_box from PDF text."""
    out: dict = {}
    m = _SPEC_RE.search(txt or "")
    if m:
        out["size_mm"] = re.sub(r"\s", "", m.group(1))
        try:
            out["thickness_mm"] = float(m.group(2))
        except ValueError:
            pass
        try:
            out["coverage_sqft_per_box"] = float(m.group(3))
        except ValueError:
            pass
    return out


def parse_technical_pdf(content: bytes) -> dict:
    """size/thickness/coverage from a Kajaria technical PDF via pdfplumber."""
    import pdfplumber
    if not content or content[:4] != b"%PDF":
        return {}
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            txt = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return {}
    return specs_from_text(txt)


def parse_pdp(html: str, url: str, fetcher: Fetcher, pdf_cache: dict):
    m = _H1_RE.search(html) or _TITLE_RE.search(html)
    title = m.group(1).strip() if m else ""
    if not title or is_placeholder_title(title):
        return None
    img = _IMG_RE.search(html)
    pdf_m = _PDF_RE.search(html)

    size = coverage = None
    if pdf_m:
        pdf_url = pdf_m.group(1)
        if pdf_url not in pdf_cache:
            r = fetcher.get(pdf_url)
            pdf_cache[pdf_url] = parse_technical_pdf(r.content) if r.ok else {}
        specs = pdf_cache[pdf_url]
        size = specs.get("size_mm")
        coverage = specs.get("coverage_sqft_per_box")

    return build_product(
        brand=BRAND, sku=slug_of(url), title=title, category=CATEGORY, source=url,
        image_url=img.group(1) if img else None,
        size_mm=size, coverage_sqft_per_box=coverage)  # price_unit/finish flagged missing


def _already(conn: sqlite3.Connection, domain: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT source_url FROM products WHERE supplier_domain=? AND source_url IS NOT NULL",
        (domain,))}


def harvest_kajaria(conn: sqlite3.Connection, fetcher: Fetcher, *, domain: str = "kajariaceramics.com",
                    brand: str = BRAND, categories: str = CATEGORY,
                    limit: int | None = None, on_item=None, **_) -> dict:
    urls = enumerate_pdps(fetcher)
    seen = _already(conn, domain)
    reachable = len(urls) > 0 or bool(seen)
    urls = [u for u in urls if u not in seen]
    if limit is not None:
        urls = urls[:limit]

    stats = {"domain": domain, "candidates": len(urls), "products": 0,
             "with_coverage": 0, "quarantined": 0, "reachable": reachable}
    pdf_cache: dict = {}
    for url in urls:
        r = fetcher.get(url)
        if not r.ok:
            stats["quarantined"] += 1
            continue
        try:
            product = parse_pdp(r.text, url, fetcher, pdf_cache)
        except Exception as exc:
            db.quarantine(conn, stage="parse", source_url=url,
                          reason=f"{type(exc).__name__}: {exc}", raw_ref=None)
            stats["quarantined"] += 1
            continue
        if product is None:
            continue
        db.upsert_product(conn, product, supplier_domain=domain)
        stats["products"] += 1
        if product.coverage_sqft_per_box is not None:
            stats["with_coverage"] += 1
        if on_item:
            on_item(product)
    return stats
