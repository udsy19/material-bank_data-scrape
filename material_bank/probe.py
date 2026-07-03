"""Stage-1 probe: classify a domain's harvest tier before any scraping.

Deterministic ladder (first positive hit wins), every step gated by robots and
logged into ``ProbeResult.log``:

    robots.txt -> /products.json (Shopify) -> Woo Store API -> JSON-LD -> tier3

The probe is the registry's verifier: it trusts nothing in the seed CSVs. When
signals contradict (platform markers present but the matching API missing) it
records ``ambiguous`` and refuses to guess a tier — those rows are handed to the
probe-adjudicator subagent (LLM slot #1), never auto-classified.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import now_iso
from .fetch import Fetcher
from .models import PricePublished, ProbeResult, ProbeStatus, ScrapeTier
from .robots import parse_robots
from .sitemap import looks_like_product_sitemap, parse_sitemap

SHOPIFY_PATH = "/products.json?limit=1"
WOO_PATHS = ("/wp-json/wc/store/v1/products?per_page=1", "/wp-json/wc/store/products?per_page=1")
DEFAULT_SITEMAP = "/sitemap.xml"
MAX_SITEMAP_FETCHES = 3          # bound the SKU-estimate crawl
MAX_JSONLD_PRODUCT_FETCH = 1     # at most one extra product page for JSON-LD


def _base_url(domain: str) -> str:
    return f"https://{domain}/"


def _positive_price(value: Any) -> bool:
    """A published, non-placeholder price (guards the ₹0 sample-variant case)."""
    if value in (None, "", "0", "0.0", "0.00", 0, 0.0):
        return False
    try:
        return float(str(value).replace(",", "")) > 0
    except (TypeError, ValueError):
        return False


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        # Many real ld+json blocks embed raw control chars in strings; retry
        # leniently rather than silently drop a valid (often priced) block.
        try:
            return json.loads(text, strict=False)
        except (ValueError, TypeError):
            return None


def _try_shopify(fetcher: Fetcher, base: str, robots, result: ProbeResult) -> bool:
    if not robots.can_fetch(SHOPIFY_PATH):
        result.note("shopify", "skipped", reason="robots-disallow")
        return False
    r = fetcher.get(base.rstrip("/") + SHOPIFY_PATH)
    if not r.ok:
        result.note("shopify", "miss", status=r.status_code)
        return False
    data = _parse_json(r.text)
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        result.note("shopify", "not-json", status=r.status_code)
        return False
    result.scrape_tier = ScrapeTier.SHOPIFY
    result.cms = "shopify"
    prices = [
        v.get("price")
        for p in data["products"]
        for v in (p.get("variants") or [])
    ]
    if any(_positive_price(p) for p in prices):
        result.price_published = PricePublished.YES
        result.note("shopify", "hit", price_detected="variant price > 0")
    elif prices:
        result.price_published = PricePublished.UNKNOWN
        result.note("shopify", "hit", price_detected="all variant prices 0 (sample?)")
    else:
        result.note("shopify", "hit", price_detected="no variants in sample")
    return True


def _try_woocommerce(fetcher: Fetcher, base: str, robots, result: ProbeResult) -> bool:
    for path in WOO_PATHS:
        if not robots.can_fetch(path):
            result.note("woocommerce", "skipped", path=path, reason="robots-disallow")
            continue
        r = fetcher.get(base.rstrip("/") + path)
        if not r.ok:
            result.note("woocommerce", "miss", path=path, status=r.status_code)
            continue
        data = _parse_json(r.text)
        items = data if isinstance(data, list) else (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            result.note("woocommerce", "not-products", path=path)
            continue
        result.scrape_tier = ScrapeTier.WOOCOMMERCE
        result.cms = "woocommerce"
        price = None
        first = items[0]
        if isinstance(first, dict):
            price = (first.get("prices") or {}).get("price")
        if _positive_price(price):
            result.price_published = PricePublished.YES
            result.note("woocommerce", "hit", path=path, price_detected="store-api price > 0")
        else:
            result.price_published = PricePublished.UNKNOWN
            result.note("woocommerce", "hit", path=path, price_detected="no positive price in sample")
        return True
    return False


def _iter_jsonld_nodes(html: str) -> list[dict]:
    """All dict nodes across every application/ld+json block (@graph flattened)."""
    nodes: list[dict] = []
    marker = "application/ld+json"
    idx = 0
    lowered = html.lower()
    while True:
        i = lowered.find(marker, idx)
        if i == -1:
            break
        start = html.find(">", i)
        end = html.find("</script", start)
        if start == -1 or end == -1:
            break
        idx = end
        blob = _parse_json(html[start + 1 : end].strip())
        for node in blob if isinstance(blob, list) else [blob]:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph") if isinstance(node.get("@graph"), list) else [node]
            nodes.extend(g for g in graph if isinstance(g, dict))
    return nodes


def _has_type(node: dict, target: str) -> bool:
    t = node.get("@type", "")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() == target for x in types)


def _extract_jsonld_products(html: str) -> list[dict]:
    """application/ld+json nodes whose @type includes Product."""
    return [n for n in _iter_jsonld_nodes(html) if _has_type(n, "product")]


def _itemlist_urls(html: str) -> list[str]:
    """PDP URLs from ItemList/ListItem markup on a listing page (schema.org)."""
    urls: list[str] = []
    for node in _iter_jsonld_nodes(html):
        if not _has_type(node, "itemlist"):
            continue
        for el in node.get("itemListElement") or []:
            if not isinstance(el, dict):
                continue
            u = el.get("url")
            item = el.get("item")
            if not u and isinstance(item, dict):
                u = item.get("url") or item.get("@id")
            elif not u and isinstance(item, str):
                u = item
            if u and u not in urls:
                urls.append(u)
    return urls


def _jsonld_price(product: dict) -> Any:
    offers = product.get("offers")
    for off in offers if isinstance(offers, list) else [offers]:
        if isinstance(off, dict):
            p = off.get("price") or off.get("lowPrice")
            if p is not None:
                return p
    return None


def _try_jsonld(fetcher: Fetcher, base: str, robots, landing_html: str,
                sitemaps: list[str], result: ProbeResult) -> bool:
    candidates = _extract_jsonld_products(landing_html)
    source = "landing"
    if not candidates:
        # Sample one page from the sitemap. It may be a PDP (has Product) or a
        # listing page (has ItemList) — in the latter case hop once to a PDP.
        product_url = _first_product_url(fetcher, sitemaps, result)
        if product_url and robots.can_fetch(product_url):
            r = fetcher.get(product_url)
            if r.ok:
                candidates = _extract_jsonld_products(r.text)
                source = "sitemap-page"
                if not candidates:
                    pdps = _itemlist_urls(r.text)
                    if pdps and robots.can_fetch(pdps[0]):
                        rp = fetcher.get(pdps[0])
                        if rp.ok:
                            candidates = _extract_jsonld_products(rp.text)
                            source = "itemlist-pdp"
    if not candidates:
        return False
    result.scrape_tier = ScrapeTier.JSONLD
    result.cms = "jsonld"
    price = next((p for p in (_jsonld_price(c) for c in candidates) if p is not None), None)
    if _positive_price(price):
        result.price_published = PricePublished.YES
        result.note("jsonld", "hit", source=source, price_detected="offers.price > 0")
    else:
        result.price_published = PricePublished.UNKNOWN
        result.note("jsonld", "hit", source=source, price_detected="Product without priced offer")
    return True


def _first_product_url(fetcher: Fetcher, sitemaps: list[str], result: ProbeResult) -> str | None:
    for sm in sitemaps[:MAX_SITEMAP_FETCHES]:
        r = fetcher.get(sm)
        if not r.ok:
            continue
        kind, locs = parse_sitemap(r.text)
        if kind == "index":
            child = next((l for l in locs if looks_like_product_sitemap(l)), locs[0] if locs else None)
            if child:
                rc = fetcher.get(child)
                if rc.ok:
                    _, child_locs = parse_sitemap(rc.text)
                    if child_locs:
                        return child_locs[0]
        elif locs:
            return locs[0]
    return None


def _estimate_skus(fetcher: Fetcher, sitemaps: list[str], result: ProbeResult) -> None:
    for sm in sitemaps:
        r = fetcher.get(sm)
        if not r.ok:
            continue
        kind, locs = parse_sitemap(r.text)
        if kind == "urlset":
            result.sku_estimate = len(locs)
            result.sitemap_url = sm
            result.note("sku_estimate", "urlset", count=len(locs), sitemap=sm)
            return
        if kind == "index":
            children = [l for l in locs if looks_like_product_sitemap(l)] or locs
            fetched = children[:MAX_SITEMAP_FETCHES]
            total = 0
            for child in fetched:
                rc = fetcher.get(child)
                if not rc.ok:
                    continue
                _, child_locs = parse_sitemap(rc.text)
                total += len(child_locs)
            if total:
                result.sku_estimate = total
                result.sitemap_url = sm
                partial = len(fetched) < len(children)
                result.note("sku_estimate", "index", count=total,
                            sitemaps_used=len(fetched), sitemaps_total=len(children),
                            partial=partial)
                return
    result.note("sku_estimate", "none", reason="no usable sitemap")


def _detect_ambiguity(landing_html: str, result: ProbeResult) -> None:
    """Platform markers that contradict the API results => flag, don't guess."""
    html = landing_html.lower()
    if ("cdn.shopify.com" in html or "shopify.theme" in html):
        result.note("ambiguity", "shopify-markers-without-api",
                    detail="Shopify markers present but /products.json did not classify")
        result.probe_status = ProbeStatus.AMBIGUOUS
    elif "wp-content" in html and "woocommerce" in html:
        result.note("ambiguity", "woo-markers-without-api",
                    detail="WooCommerce markers present but Store API did not classify")
        result.probe_status = ProbeStatus.AMBIGUOUS


def _fetch_landing(domain: str, fetcher: Fetcher, result: ProbeResult):
    """Fetch the landing page, falling back to www. Many India sites serve only
    on www (bare domain has a cert/SNI issue or times out)."""
    hosts = [domain] if domain.startswith("www.") else [domain, f"www.{domain}"]
    resp = None
    for host in hosts:
        resp = fetcher.get(f"https://{host}/")
        if not resp.error:
            if host != domain:
                result.note("landing", "www-fallback", host=host)
            return host, resp
    return domain, resp  # all failed; carries the last error


def classify(domain: str, fetcher: Fetcher) -> ProbeResult:
    result = ProbeResult(domain=domain)

    # 1. Landing (with www fallback).
    base_host, landing = _fetch_landing(domain, fetcher, result)
    base = _base_url(base_host)
    if landing is None or landing.error:
        result.probe_status = ProbeStatus.UNREACHABLE
        result.note("landing", "unreachable", error=landing.error if landing else "no response")
        result.probed_at = now_iso()
        return result
    result.http_status = landing.status_code
    if landing.redirected_host:
        result.final_host = landing.redirected_host
        result.note("landing", "redirected", final_host=landing.redirected_host)
    elif base_host != domain:
        result.final_host = base_host  # we had to switch to www
    if landing.status_code in (401, 403) or landing.status_code == 429:
        result.probe_status = ProbeStatus.BLOCKED
        result.note("landing", "blocked", status=landing.status_code)
        result.probed_at = now_iso()
        return result

    # 2. robots.txt.
    rob_resp = fetcher.get(base + "robots.txt")
    if rob_resp.error:
        robots = parse_robots(base, "", fetched=False)
        result.robots_ok = None
        result.note("robots", "fetch-error", error=rob_resp.error)
    elif rob_resp.status_code == 404 or not rob_resp.text.strip():
        robots = parse_robots(base, "", fetched=False)
        result.robots_ok = True  # absent robots = allow-all (standard)
        result.note("robots", "absent", status=rob_resp.status_code)
    elif rob_resp.ok:
        robots = parse_robots(base, rob_resp.text, fetched=True)
        result.robots_url = base + "robots.txt"
        result.sitemap_url = robots.sitemaps[0] if robots.sitemaps else None
        allowed = robots.can_fetch("/")
        result.robots_ok = allowed
        result.note("robots", "parsed", root_allowed=allowed, sitemaps=len(robots.sitemaps))
        if not allowed:
            result.note("robots", "root-disallowed", action="stop; not probing paths")
            result.probed_at = now_iso()
            return result
    else:
        robots = parse_robots(base, "", fetched=False)
        result.robots_ok = None
        result.note("robots", "unexpected-status", status=rob_resp.status_code)

    sitemaps = list(robots.sitemaps)
    if not sitemaps:
        sitemaps = [base.rstrip("/") + DEFAULT_SITEMAP]

    # 3. Tier ladder (first hit wins).
    classified = (
        _try_shopify(fetcher, base, robots, result)
        or _try_woocommerce(fetcher, base, robots, result)
        or _try_jsonld(fetcher, base, robots, landing.text, sitemaps, result)
    )

    # 4. SKU estimate (independent of tier).
    _estimate_skus(fetcher, sitemaps, result)

    # 5. Resolve unclassified: tier3 vs ambiguous.
    if not classified:
        _detect_ambiguity(landing.text, result)
        if result.probe_status is not ProbeStatus.AMBIGUOUS:
            result.scrape_tier = ScrapeTier.TIER3
            result.note("tier3", "assigned", reason="no API/JSON-LD signal; needs Playwright")

    result.probed_at = now_iso()
    return result


# --- DB write-back ----------------------------------------------------------

_WRITE_COLUMNS = (
    "scrape_tier", "robots_ok", "robots_url", "sitemap_url", "sku_estimate",
    "price_published", "cms", "http_status", "final_host", "probe_status",
    "probed_at", "probe_log",
)


def _to_row(result: ProbeResult) -> tuple:
    return (
        result.scrape_tier.value if result.scrape_tier else None,
        None if result.robots_ok is None else int(result.robots_ok),
        result.robots_url,
        result.sitemap_url,
        result.sku_estimate,
        result.price_published.value,
        result.cms,
        result.http_status,
        result.final_host,
        result.probe_status.value,
        result.probed_at,
        json.dumps(result.log),
    )


def write_result(conn: sqlite3.Connection, result: ProbeResult) -> None:
    set_clause = ", ".join(f"{c}=?" for c in _WRITE_COLUMNS)
    conn.execute(
        f"UPDATE suppliers SET {set_clause} WHERE domain=?",
        (*_to_row(result), result.domain),
    )
    conn.commit()


def run_probe(conn: sqlite3.Connection, domain: str, fetcher: Fetcher) -> ProbeResult:
    result = classify(domain, fetcher)
    write_result(conn, result)
    return result
