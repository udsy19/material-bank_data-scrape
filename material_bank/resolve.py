"""Entity resolution (Phase C): non-destructive variant grouping.

The catalog is full of *variants*, not duplicates: one mattress model listed in
~200 size×thickness SKUs, one tile design in two finishes. Investigating the
data settled the design (2026-07-08):

  - Cross-supplier duplicates barely exist — our suppliers are disjoint,
    brand-direct catalogs (0 (brand,size) keys span two suppliers). So classic
    cross-supplier price comparison has no data to operate on yet.
  - Within a supplier, 23k products share an identical (brand, title) with a
    sibling — every one carrying a *distinct SKU*. These are real, distinct
    SKUs. Merging them would destroy data (a fabrication in reverse).

So "one product, one truth" here means GROUPING variants under one canonical
design, never deleting a SKU. Each group gets a stable ``variant_group_id``
(deterministic hash of supplier + normalized brand + normalized title); the
catalog can then collapse to one card per design, and a product page can show
its sibling variants side by side with each one's own price observation.

Deterministic, idempotent, read-then-write (WAL BUSY_SNAPSHOT rule). Singletons
keep ``variant_group_id`` NULL — they are their own canonical, nothing to group.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import defaultdict

from .db import now_iso

_WORD_RE = re.compile(r"[a-z0-9]+")


def _norm(text: str) -> str:
    """Lowercase alnum tokens, space-joined — punctuation/spacing invariant."""
    return " ".join(_WORD_RE.findall((text or "").lower()))


def group_key(supplier_domain: str, brand: str, title: str) -> str | None:
    """Deterministic variant-group id, or None when the title is too thin to
    group safely (a bare/generic title must not swallow unrelated products)."""
    nt = _norm(title)
    if len(nt.split()) < 2:            # 1-token titles are too generic to trust
        return None
    raw = f"{_norm(supplier_domain)}|{_norm(brand)}|{nt}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def assign_variant_groups(conn: sqlite3.Connection, *, batch: int = 20000) -> dict:
    """Assign ``variant_group_id`` to every product that shares a design key with
    at least one sibling; NULL for singletons. Idempotent."""
    rows = conn.execute(          # read fully BEFORE writing (WAL rule)
        "SELECT id, supplier_domain, brand, title FROM products").fetchall()
    members: dict[str, list[int]] = defaultdict(list)
    key_of: dict[int, str] = {}
    for r in rows:
        k = group_key(r["supplier_domain"] or "", r["brand"] or "", r["title"] or "")
        if k is not None:
            members[k].append(r["id"])
            key_of[r["id"]] = k

    ts = now_iso()
    updates, grouped = [], 0
    for r in rows:
        k = key_of.get(r["id"])
        gid = k if (k is not None and len(members[k]) >= 2) else None
        if gid is not None:
            grouped += 1
        updates.append((gid, ts, r["id"]))
    for i in range(0, len(updates), batch):
        conn.executemany(
            "UPDATE products SET variant_group_id=?, resolved_at=? WHERE id=?",
            updates[i:i + batch])
        conn.commit()
    n_groups = sum(1 for v in members.values() if len(v) >= 2)
    return {"scanned": len(rows), "groups": n_groups, "grouped_products": grouped}


# Attributes that distinguish one variant from another within a design group.
_VARIANT_AXES = ("size_mm", "finish", "color", "thickness_mm")


def variants_of(conn: sqlite3.Connection, product_id: int) -> list[dict]:
    """Sibling variants of a product (incl. itself) with each one's freshest
    price and its distinguishing attributes. Empty when the product is a
    singleton (no variant group)."""
    row = conn.execute(
        "SELECT variant_group_id FROM products WHERE id=?", (product_id,)).fetchone()
    if row is None or row["variant_group_id"] is None:
        return []
    from .retrieval import freshest_price
    sibs = conn.execute(
        f"SELECT id, sku, title, size_mm, finish, color, thickness_mm, "
        f"image_url, source_url, completeness, publish_ready "
        f"FROM products WHERE variant_group_id=? ORDER BY completeness DESC, id",
        (row["variant_group_id"],)).fetchall()
    out = []
    for s in sibs:
        d = {k: s[k] for k in ("id", "sku", "title", "image_url", "source_url",
                               "publish_ready")}
        d["attrs"] = {a: s[a] for a in _VARIANT_AXES if s[a] is not None}
        d["price"] = freshest_price(conn, s["id"])
        out.append(d)
    return out


def audit_variant_groups(conn: sqlite3.Connection) -> dict:
    """Flag suspect variant groups — a free auditor of grouping quality. A group
    whose members span >1 canonical category, or whose fresh prices spread >20x,
    was probably mis-grouped (a generic title collided unrelated SKUs). Reported
    as a data-quality metric; the samples are the review/self-repair work-list."""
    rows = conn.execute("""
        WITH latest AS (
            SELECT product_id, price_inr,
                   ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY observed_at DESC) rn
            FROM price_observation),
        g AS (
            SELECT p.variant_group_id AS gid,
                   COUNT(DISTINCT p.category_std) AS ncat,
                   MIN(l.price_inr) AS mn, MAX(l.price_inr) AS mx, COUNT(*) AS n
            FROM products p LEFT JOIN latest l ON l.product_id = p.id AND l.rn = 1
            WHERE p.variant_group_id IS NOT NULL
            GROUP BY p.variant_group_id)
        SELECT gid, ncat, mn, mx, n FROM g
        WHERE ncat > 1 OR (mn IS NOT NULL AND mn > 0 AND mx > 20 * mn)
        ORDER BY (mx / NULLIF(mn, 0)) DESC""").fetchall()
    return {"suspect_count": len(rows), "samples": [dict(r) for r in rows[:10]]}


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    from . import db

    ap = argparse.ArgumentParser(prog="mb-resolve")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    print(json.dumps(assign_variant_groups(conn)), file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
