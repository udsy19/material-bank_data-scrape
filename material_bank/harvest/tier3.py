"""Generic Playwright tier3 harvester — for JS-rendered sites with no API/JSON-LD.

Registry-driven: the probe tags a domain ``tier3``; this renders each PDP in a
headless browser and extracts, in order of preference:
  1. post-render JSON-LD Product (some sites inject it via JS) -> full spec,
  2. an honest specs-only fallback (title + product image), with size/finish/
     price flagged missing — never fabricated.

Respects the pipeline's tier3 rule: a page that won't render / is WAF-blocked is
quarantined and the crawl moves on. Prices are not invented here (specs-only).
Playwright is 10x the cost of the static tiers, so callers cap the run.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

from .. import db
from ..fetch import Fetcher
from ..models import NormalizedProduct
from ..probe import _extract_jsonld_products
from ..sitemap import looks_like_product_sitemap, parse_sitemap
from .common import build_product, is_placeholder_title

# Finds the largest non-icon product image on the rendered page.
_BEST_IMAGE_JS = """() => {
  const imgs = [...document.querySelectorAll('img')]
    .filter(i => i.naturalWidth > 250 && !/\\.svg($|\\?)/.test(i.src)
                 && !/logo|icon|banner|menu|sprite/i.test(i.src));
  imgs.sort((a,b) => b.naturalWidth*b.naturalHeight - a.naturalWidth*a.naturalHeight);
  return imgs.length ? imgs[0].src : null;
}"""


def slug_of(url: str) -> str:
    path = [s for s in urlparse(url).path.split("/") if s]
    return path[-1] if path else url


_PRODUCT_HINTS = ("/product", "/products/", "/p/", "/item", "/shop/", "/buy/", "/collections/")
_ASSET_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".pdf", ".css", ".js")
_NON_PRODUCT = ("/blog", "/news", "/about", "/contact", "/policy", "/policies", "/cart",
                "/account", "/login", "/register", "/search", "/faq", "/terms", "/privacy",
                "sitemap", ".xml", "/pages/", "/collections?")


def is_pdp_url(url: str, pattern: str) -> bool:
    u = url.lower()
    return pattern in u and not any(x in u for x in ("/blog", "/news", "sitemap", ".xml"))


def enumerate_pdp_urls(fetcher: Fetcher, sitemap_url: str, *, pattern: str = "/products/") -> list[str]:
    """Candidate PDP URLs from a sitemap, robust to arbitrary site structures. A fixed
    ``/products/`` pattern missed sites that don't use it (godrejinterio, lladro ->
    0 candidates), so: prefer the pattern, else product-hint paths, else ALL non-asset/
    non-boilerplate URLs — the render+extract step drops whatever isn't a real Product."""
    r = fetcher.get(sitemap_url)
    if not r.ok:
        return []
    kind, locs = parse_sitemap(r.text)
    if kind == "index":  # follow children (prefer product-looking child sitemaps)
        children = [l for l in locs if looks_like_product_sitemap(l)] or locs
        out: list[str] = []
        for child in children[:50]:
            rc = fetcher.get(child)
            if rc.ok:
                out += parse_sitemap(rc.text)[1]
        locs = out
    locs = [u for u in locs
            if not u.lower().split("?")[0].endswith(_ASSET_EXT)
            and not any(x in u.lower() for x in _NON_PRODUCT)]
    hit = [u for u in locs if pattern in u.lower()]
    if len(hit) >= 10:
        return hit
    hints = [u for u in locs if any(h in u.lower() for h in _PRODUCT_HINTS)]
    return hints if len(hints) >= 10 else locs


def extract(rendered: dict, url: str, *, brand: str, category: str) -> NormalizedProduct | None:
    """rendered = {html, title, image}. Returns a product or None (quarantine)."""
    html = rendered.get("html", "")
    products = _extract_jsonld_products(html)
    if products:
        p = products[0]
        img = p.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        title = (p.get("name") or rendered.get("title") or "").strip()
        if title and not is_placeholder_title(title):
            return build_product(
                brand=(p.get("brand") if isinstance(p.get("brand"), str) else None) or brand,
                sku=slug_of(url), title=title, category=category, source=url,
                image_url=(img if isinstance(img, str) else None) or rendered.get("image"))

    # Honest specs-only fallback: title + image, everything else flagged missing.
    title = (rendered.get("title") or "").strip()
    if not title or is_placeholder_title(title):
        return None
    return build_product(brand=brand, sku=slug_of(url), title=title,
                         category=category, source=url, image_url=rendered.get("image"))


def render_pdp(page, url: str, *, wait_ms: int = 1500) -> dict:
    page.goto(url, wait_until="networkidle", timeout=30000)
    if wait_ms:
        page.wait_for_timeout(wait_ms)
    title = ""
    try:
        if page.locator("h1").count():
            title = page.locator("h1").first.inner_text().strip()
    except Exception:
        pass
    if not title:
        title = (page.title() or "").strip()
    try:
        image = page.evaluate(_BEST_IMAGE_JS)
    except Exception:
        image = None
    return {"html": page.content(), "title": title, "image": image}


def _already(conn: sqlite3.Connection, domain: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT sku FROM products WHERE supplier_domain=?", (domain,))}


def drop_shared_default_images(conn: sqlite3.Connection, domain: str, *, threshold: int = 3) -> int:
    """Null image_urls shared by many products of a domain — the tier3 image
    heuristic sometimes grabs a site's default/placeholder. A wrong image
    association is a form of fabrication, so we drop it rather than assert it."""
    dupes = [r[0] for r in conn.execute(
        "SELECT image_url FROM products WHERE supplier_domain=? AND image_url IS NOT NULL "
        "GROUP BY image_url HAVING COUNT(*) >= ?", (domain, threshold))]
    for img in dupes:
        conn.execute("UPDATE products SET image_url=NULL WHERE supplier_domain=? AND image_url=?",
                     (domain, img))
    conn.commit()
    return len(dupes)


def harvest_tier3(
    conn: sqlite3.Connection,
    *,
    domain: str,
    brand: str,
    categories: str,
    sitemap_url: str,
    pattern: str = "/products/",
    limit: int | None = None,
    wait_ms: int = 1500,
    on_item=None,
) -> dict:
    from playwright.sync_api import sync_playwright

    fetcher = Fetcher(raw_dir=None)
    urls = enumerate_pdp_urls(fetcher, sitemap_url, pattern=pattern)
    seen = _already(conn, domain)
    urls = [u for u in urls if slug_of(u) not in seen]
    if limit is not None:
        urls = urls[:limit]

    stats = {"domain": domain, "candidates": len(urls), "products": 0,
             "specs_only": 0, "quarantined": 0}
    if not urls:
        return stats

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"))
        for url in urls:
            try:
                rendered = render_pdp(page, url, wait_ms=wait_ms)
            except Exception as exc:  # WAF / timeout -> respect, move on
                db.quarantine(conn, stage="tier3-render", source_url=url,
                              reason=f"{type(exc).__name__}: {exc}", raw_ref=None)
                stats["quarantined"] += 1
                continue
            prod = extract(rendered, url, brand=brand, category=categories)
            if prod is None:
                db.quarantine(conn, stage="tier3-extract", source_url=url,
                              reason="no title/product extractable", raw_ref=None)
                stats["quarantined"] += 1
                continue
            try:
                db.upsert_product(conn, prod, supplier_domain=domain)
            except Exception as exc:
                db.quarantine(conn, stage="normalize", source_url=url,
                              reason=f"{type(exc).__name__}: {exc}", raw_ref=None)
                stats["quarantined"] += 1
                continue
            stats["products"] += 1
            stats["specs_only"] += 1  # tier3 harvest is spec-only (no price)
            if on_item:
                on_item(prod)
        browser.close()
    stats["dropped_default_images"] = drop_shared_default_images(conn, domain)
    return stats
