"""Dealer / "where to buy" harvesting — the point of sale for most categories.

Three deterministic sources (field shapes verified live 2026-07-11):
  * Kajaria — open REST API (/api/getStores), richest + cleanest.
  * Single-Interface SaaS (H&R Johnson, Somany, Jaquar, Godrej Interio) — one
    parser over server-rendered schema.org LocalBusiness microdata.
  * Orientbell — Next.js ``__NEXT_DATA__`` (company stores + dealer network).

Internal CRM fields that leak into some of these public payloads
(no_of_sales, billed_in_history, invoice_no, SAP codes, sales-rep contacts) are
deliberately EXCLUDED — republishing them is outside the trust contract. Dealer
rows are the evidence; ``regions served`` is DERIVED from them, never declared.
Parsers are pure functions (offline-tested); harvesters add the polite fetch.
"""

from __future__ import annotations

import json
import re
import sqlite3

from . import db
from .fetch import Fetcher

# ── pure parsers (tested offline) ────────────────────────────────────────────


def _clean(*parts) -> str | None:
    s = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    return s or None


def _title(s: str | None) -> str | None:
    return s.strip().title() if s and s.strip() else None


def parse_kajaria_store(rec: dict, state: str | None = None) -> dict:
    """One /api/getStores record -> our dealer dict. Excludes SAP/sales-rep fields."""
    return {
        "name": rec.get("name"),
        "address": _clean(rec.get("address1"), rec.get("address2")),
        "city": _title(rec.get("city")),
        "state": _title(rec.get("state") or state),
        "pincode": (str(rec.get("pincode")).strip() or None) if rec.get("pincode") else None,
        "lat": _float(rec.get("latitude")),
        "lon": _float(rec.get("longitude")),
        "phone": rec.get("mobile") or rec.get("phone"),
        "email": rec.get("email"),
    }


_MICRO_RE = {
    "name": re.compile(r'itemprop=["\']name["\'][^>]*>\s*(?:<span[^>]*>)?([^<]+)', re.I),
    "street": re.compile(r'itemprop=["\']streetAddress["\'][^>]*>([^<]+)', re.I),
    "city": re.compile(r'itemprop=["\']addressRegion["\'][^>]*>([^<]+)', re.I),
    "pincode": re.compile(r'itemprop=["\']postalCode["\'][^>]*>([^<]+)', re.I),
    "phone": re.compile(r'itemprop=["\']telephone["\'][^>]*>([^<]+)', re.I),
    "lat": re.compile(r'itemprop=["\']latitude["\'][^>]*content=["\']([^"\']+)', re.I),
    "lon": re.compile(r'itemprop=["\']longitude["\'][^>]*content=["\']([^"\']+)', re.I),
}
_SI_STATE_RE = re.compile(r',\s*([A-Za-z][A-Za-z\s]+?)\s*-\s*\d{6}\s*<', re.I)


def parse_singleinterface_detail(html: str, url: str, state: str | None = None) -> dict | None:
    """Single-Interface store detail page (schema.org microdata). addressRegion
    here holds the CITY (verified); state is read from the dl-loc-address block
    or passed in from the sitemap. No email exists on this platform."""
    def g(key):
        m = _MICRO_RE[key].search(html)
        return m.group(1).strip() if m else None
    name = g("name")
    if not name:
        return None
    st = state
    if st is None:
        m = _SI_STATE_RE.search(html)
        st = m.group(1) if m else None
    return {
        "name": name, "address": g("street"), "city": _title(g("city")),
        "state": _title(st), "pincode": g("pincode"),
        "lat": _float(g("lat")), "lon": _float(g("lon")),
        "phone": g("phone"), "email": None, "source_url": url,
    }


# fields that must never be stored (internal CRM / ERP leakage)
_ORIENT_DROP = {"no_of_sales", "billed_in_history", "invoice_no", "invoice_date", "distance"}


def parse_orientbell_store(rec: dict) -> dict:
    """One obtbs/nonObtbs record -> dealer dict (handles both shapes)."""
    return {
        "name": rec.get("business_name") or rec.get("name"),
        "address": _clean(rec.get("address_1") or rec.get("address"), rec.get("address_2")),
        "city": _title(rec.get("city")),
        "state": _title(rec.get("state") or rec.get("state_desc")),
        "pincode": (str(rec.get("postcode")).strip() or None) if rec.get("postcode") else None,
        "lat": _float(rec.get("latitude")), "lon": _float(rec.get("longitude")),
        "phone": rec.get("main_phone_no") or rec.get("phone_no"),
        "email": rec.get("email"),
    }


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    try:
        d = json.loads(m.group(1)) if m else {}
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _page_props(html: str) -> dict:
    """The Next.js pageProps dict, or {} — tolerant of missing/odd shapes."""
    props = _next_data(html).get("props")
    pp = props.get("pageProps") if isinstance(props, dict) else None
    return pp if isinstance(pp, dict) else {}


# ── storage + region derivation ──────────────────────────────────────────────


def store_dealers(conn: sqlite3.Connection, domain: str, dealers: list[dict],
                  *, source_url: str | None = None) -> int:
    ts = db.now_iso()
    n = 0
    for d in dealers:
        if not (d.get("name") and (d.get("city") or d.get("pincode"))):
            continue
        # NULL-safe dedup (the table UNIQUE misses rows with a NULL address/pincode)
        exists = conn.execute(
            "SELECT 1 FROM dealers WHERE supplier_domain=? AND name=? "
            "AND COALESCE(pincode,'')=COALESCE(?,'') AND COALESCE(city,'')=COALESCE(?,'')",
            (domain, d.get("name"), d.get("pincode"), d.get("city"))).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO dealers (supplier_domain, name, address, city, state, "
            "pincode, lat, lon, phone, email, source_url, observed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (domain, d.get("name"), d.get("address"), d.get("city"), d.get("state"),
             d.get("pincode"), d.get("lat"), d.get("lon"), d.get("phone"), d.get("email"),
             d.get("source_url") or source_url, ts))
        n += 1
    conn.commit()
    return n


def derive_regions(conn: sqlite3.Connection, domain: str) -> dict:
    """Compute states/cities served + dealer_count + pan_india FROM the dealer
    rows (a derived aggregate, refreshed each harvest — never a declared fact)."""
    states = [r[0] for r in conn.execute(
        "SELECT DISTINCT state FROM dealers WHERE supplier_domain=? AND state IS NOT NULL "
        "AND TRIM(state)!='' ORDER BY state", (domain,))]
    cities = [r[0] for r in conn.execute(
        "SELECT DISTINCT city FROM dealers WHERE supplier_domain=? AND city IS NOT NULL "
        "AND TRIM(city)!='' ORDER BY city", (domain,))]
    count = conn.execute("SELECT COUNT(*) FROM dealers WHERE supplier_domain=?",
                         (domain,)).fetchone()[0]
    pan = 1 if len(states) >= 18 else 0        # ~2/3 of India's 28 states + UTs
    conn.execute(
        "UPDATE suppliers SET states_served=?, cities_served=?, dealer_count=?, "
        "pan_india=? WHERE domain=?",
        (json.dumps(states), json.dumps(cities), count, pan, domain))
    conn.commit()
    return {"states": len(states), "cities": len(cities), "dealers": count, "pan_india": pan}


# ── harvesters (parser + polite fetch) ───────────────────────────────────────

KAJARIA = "kajariaceramics.com"
ORIENTBELL = "orientbell.com"


def harvest_kajaria_dealers(conn, fetcher: Fetcher, *, limit_states: int | None = None) -> dict:
    base = "https://www.kajariaceramics.com"
    r = fetcher.get(f"{base}/api/getStates?country=India")
    states = json.loads(r.text) if r.ok else []
    states = [s for s in states if isinstance(s, str)]
    if limit_states:
        states = states[:limit_states]
    total = 0
    for st in states:
        rr = fetcher.get(f"{base}/api/getStores?state={st}&country=India&page=1")
        if not rr.ok:
            continue
        recs = (json.loads(rr.text) or {}).get("data", [])
        rows = [{**parse_kajaria_store(x, st),
                 "source_url": f"{base}/where-to-buy"} for x in recs]
        total += store_dealers(conn, KAJARIA, rows)
    derive_regions(conn, KAJARIA)
    return {"domain": KAJARIA, "states": len(states), "dealers_added": total}


def harvest_orientbell_dealers(conn, fetcher: Fetcher, *, limit_states: int | None = None) -> dict:
    base = "https://www.orientbell.com"
    first = fetcher.get(f"{base}/store-locator/maharashtra")
    states_data = _page_props(first.text).get("statesData", []) if first.ok else []
    slugs = [re.sub(r"\s+", "-", (s.get("state") or "").lower())
             for s in states_data if isinstance(s, dict)]
    slugs = [s for s in slugs if s] or ["maharashtra"]
    if limit_states:
        slugs = slugs[:limit_states]
    total = 0
    for slug in slugs:
        rr = fetcher.get(f"{base}/store-locator/{slug}")
        if not rr.ok:
            continue
        sd = _page_props(rr.text).get("storeData", {})
        if not isinstance(sd, dict):     # some states return an empty list, not {}
            continue
        recs = (sd.get("obtbs") or []) + (sd.get("nonObtbs") or [])
        rows = [{**parse_orientbell_store(x),
                 "source_url": f"{base}/store-locator/{slug}"}
                for x in recs if isinstance(x, dict)]
        total += store_dealers(conn, ORIENTBELL, rows)
    derive_regions(conn, ORIENTBELL)
    return {"domain": ORIENTBELL, "states": len(slugs), "dealers_added": total}


DEALER_HARVESTERS = {
    KAJARIA: harvest_kajaria_dealers,
    ORIENTBELL: harvest_orientbell_dealers,
}


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="mb-dealers")
    ap.add_argument("--domain", choices=sorted(DEALER_HARVESTERS), required=True)
    ap.add_argument("--limit-states", type=int, default=None)
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    stats = DEALER_HARVESTERS[args.domain](conn, Fetcher(raw_dir=None),
                                           limit_states=args.limit_states)
    print(json.dumps(stats), file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
