"""GSTIN registry-lookup adapter — behind a flag, ₹0 until enabled.

GSTIN is a business identifier almost never published on a supplier's own site
(it surfaces on invoices / the GST portal), so it must come from a *licensed*
GSP/ASP API — we never scrape the GST portal (its ToS forbids it). This adapter
is OFF by default: with no provider configured, ``backfill_gstins`` is a no-op
that fabricates nothing. When a provider is wired (env ``GSTIN_API_URL`` +
``GSTIN_API_KEY``), lookups are per-supplier and budget-capped, and each result
is validated against the GSTIN format and stored with basis ``registry:gstin``.
"""

from __future__ import annotations

import json
import os
import sqlite3

from .company_extract import _RE_GSTIN
from .db import now_iso


def provider_enabled() -> bool:
    return bool(os.getenv("GSTIN_API_URL") and os.getenv("GSTIN_API_KEY"))


def _provider_lookup(legal_name: str, state: str | None) -> str | None:
    """Call the configured licensed GSP/ASP endpoint. Returns a GSTIN or None.
    Kept deliberately thin — the real request shape depends on the vendor."""
    try:
        from curl_cffi import requests
        r = requests.get(os.environ["GSTIN_API_URL"],
                         params={"name": legal_name, "state": state or ""},
                         headers={"Authorization": f"Bearer {os.environ['GSTIN_API_KEY']}"},
                         timeout=15)
        if r.status_code == 200:
            return (r.json() or {}).get("gstin")
    except Exception:
        pass
    return None


def lookup(legal_name: str, state: str | None = None, *, lookup_fn=None) -> str | None:
    """Validated GSTIN for a legal name, or None. Disabled ⇒ None (never guess)."""
    fn = lookup_fn or (_provider_lookup if provider_enabled() else None)
    if fn is None or not (legal_name or "").strip():
        return None
    g = fn(legal_name, state)
    return g if g and _RE_GSTIN.fullmatch(g.strip()) else None


def backfill_gstins(conn: sqlite3.Connection, *, limit: int = 50, lookup_fn=None) -> dict:
    """Fill GSTIN for suppliers that have a legal name but no GSTIN. No-op (and
    honestly reports it) unless a provider is configured or a lookup_fn injected."""
    if lookup_fn is None and not provider_enabled():
        return {"enabled": False, "updated": 0, "attempted": 0}
    rows = conn.execute(
        "SELECT domain, legal_name, state FROM suppliers "
        "WHERE legal_name IS NOT NULL AND (gstin IS NULL OR TRIM(gstin)='') LIMIT ?",
        (limit,)).fetchall()
    updated = 0
    for r in rows:
        g = lookup(r["legal_name"], r["state"], lookup_fn=lookup_fn)
        if not g:
            continue
        prov = json.loads((conn.execute("SELECT supplier_provenance FROM suppliers WHERE domain=?",
                                        (r["domain"],)).fetchone()[0]) or "{}")
        prov["gstin"] = {"source": "gst-registry", "basis": "registry:gstin", "confidence": 0.95}
        conn.execute("UPDATE suppliers SET gstin=?, supplier_provenance=? WHERE domain=?",
                     (g, json.dumps(prov), r["domain"]))
        updated += 1
    conn.commit()
    return {"enabled": True, "attempted": len(rows), "updated": updated}


def main(argv=None) -> int:
    import argparse
    import sys

    from . import db

    ap = argparse.ArgumentParser(prog="mb-gstin")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    print(json.dumps(backfill_gstins(conn, limit=args.limit)), file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
