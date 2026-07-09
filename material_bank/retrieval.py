"""Hybrid retrieval: FTS5 keyword ∪ vector semantic, rank-fused, priced.

Query path (Stage 8): FTS5 candidates ∪ vector candidates -> reciprocal-rank
fusion -> attach the freshest price observation with its basis and an honest
staleness flag (>90 days). MRP stays labelled MRP.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

RRF_K = 60          # reciprocal-rank-fusion constant (standard)
STALE_DAYS = 90


def _fts_query(text: str) -> str:
    """Safe FTS5 MATCH string: alnum terms as OR'd prefix queries."""
    terms = re.findall(r"[a-z0-9]+", (text or "").lower())
    return " OR ".join(f'"{t}"*' for t in terms)


def keyword_search(conn: sqlite3.Connection, query: str, k: int = 50) -> list[int]:
    match = _fts_query(query)
    if not match:
        return []
    rows = conn.execute(
        "SELECT rowid FROM products_fts WHERE products_fts MATCH ? ORDER BY rank LIMIT ?",
        (match, k),
    ).fetchall()
    return [r[0] for r in rows]


def semantic_search(conn, embedder, store, query: str, k: int = 50) -> list[int]:
    if embedder is None or store is None:
        return []
    vec = embedder.encode_text([query])[0]
    return [pid for pid, _ in store.search(vec, kind="text", k=k)]


def _rrf(rankings: list[list[int]]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, pid in enumerate(ranking):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


def freshest_price(conn: sqlite3.Connection, product_id: int, *, now: datetime | None = None) -> dict | None:
    row = conn.execute(
        "SELECT price_inr, price_unit, basis, observed_at, source, source_url "
        "FROM price_observation WHERE product_id=? ORDER BY observed_at DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["stale"] = False
    try:
        observed = datetime.fromisoformat(row["observed_at"])
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        ref = now or datetime.now(timezone.utc)
        out["age_days"] = (ref - observed).days
        out["stale"] = out["age_days"] > STALE_DAYS
    except (ValueError, TypeError):
        out["age_days"] = None
    return out


def _hydrate(conn: sqlite3.Connection, pid: int, score: float, *, now=None) -> dict | None:
    p = conn.execute(
        "SELECT id, brand, title, category, size_mm, finish, price_unit, image_url, "
        "supplier_domain FROM products WHERE id=?", (pid,),
    ).fetchone()
    if p is None:
        return None
    d = dict(p)
    d["score"] = round(score, 6)
    d["price"] = freshest_price(conn, pid, now=now)
    return d


def hybrid_search(conn, embedder, store, query: str, *, k: int = 20,
                  candidates: int = 50, now=None) -> list[dict]:
    """Fuse keyword + semantic candidates, hydrate top-k with freshest price."""
    kw = keyword_search(conn, query, k=candidates)
    sem = semantic_search(conn, embedder, store, query, k=candidates)
    fused = _rrf([kw, sem])
    if not fused:
        return []
    ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
    out = [_hydrate(conn, pid, score, now=now) for pid, score in ranked]
    return [r for r in out if r is not None]


def stats(conn: sqlite3.Connection) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
    return {
        "suppliers_registry": q("SELECT COUNT(*) FROM suppliers"),
        "suppliers_harvested": q("SELECT COUNT(DISTINCT supplier_domain) FROM products"),
        "products": q("SELECT COUNT(*) FROM products"),
        "products_priced": q("SELECT COUNT(DISTINCT product_id) FROM price_observation"),
        "products_with_image": q("SELECT COUNT(*) FROM products WHERE image_url IS NOT NULL"),
        "price_observations": q("SELECT COUNT(*) FROM price_observation"),
        "text_vectors": q("SELECT COUNT(*) FROM embeddings WHERE kind='text'"),
        "image_vectors": q("SELECT COUNT(*) FROM embeddings WHERE kind='image'"),
        "quarantine": q("SELECT COUNT(*) FROM quarantine"),
        "categories": q("SELECT COUNT(DISTINCT category) FROM products"),
        # trust contract (Phase A) — 0 until the planner's first scoring run
        "publish_ready": q("SELECT COUNT(*) FROM products WHERE publish_ready=1"),
        "median_completeness": (lambda r: r[0] if r else 0)(conn.execute(
            "SELECT completeness FROM products WHERE completeness IS NOT NULL "
            "ORDER BY completeness LIMIT 1 OFFSET "
            "(SELECT COUNT(*) FROM products WHERE completeness IS NOT NULL)/2").fetchone()) or 0,
    }


def top_suppliers(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT supplier_domain AS domain, COUNT(*) AS products FROM products "
        "GROUP BY supplier_domain ORDER BY products DESC LIMIT ?", (limit,))]


def list_suppliers(conn: sqlite3.Connection) -> list[dict]:
    """Every supplier that has products, with counts + registry metadata."""
    return [dict(r) for r in conn.execute(
        """
        SELECT p.supplier_domain AS domain,
               COALESCE(s.brand, p.supplier_domain) AS brand,
               s.scrape_tier AS tier, s.categories,
               COUNT(*) AS products,
               COUNT(DISTINCT po.product_id) AS priced,
               COUNT(DISTINCT CASE WHEN p.image_url IS NOT NULL THEN p.id END) AS with_image
        FROM products p
        LEFT JOIN suppliers s ON s.domain = p.supplier_domain
        LEFT JOIN price_observation po ON po.product_id = p.id
        GROUP BY p.supplier_domain
        ORDER BY products DESC
        """)]


# Columns safe to expose/order by in the products query.
_LIST_ORDER = {"id": "p.id", "price": "l.price_inr", "title": "p.title", "brand": "p.brand"}


def list_products(
    conn: sqlite3.Connection,
    *,
    supplier: str | None = None,
    category: str | None = None,
    family: str | None = None,
    category_std: str | None = None,
    brand: str | None = None,
    q: str | None = None,
    priced: bool | None = None,
    has_image: bool | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    publish_ready: bool | None = None,
    order: str = "id",
    desc: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Filtered, paginated product listing with the freshest price attached.

    Returns {total, count, limit, offset, items}. All filters are parameterized.
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where: list[str] = []
    params: list = []
    if supplier:
        where.append("p.supplier_domain = ?"); params.append(supplier)
    if brand:
        where.append("p.brand = ?"); params.append(brand)
    if category:
        where.append("p.category LIKE ?"); params.append(f"%{category}%")
    if family:
        where.append("p.family = ?"); params.append(family)
    if category_std:
        where.append("p.category_std = ?"); params.append(category_std)
    if q:
        where.append("p.title LIKE ?"); params.append(f"%{q}%")
    if has_image is True:
        where.append("p.image_url IS NOT NULL")
    elif has_image is False:
        where.append("p.image_url IS NULL")
    if priced is True:
        where.append("l.price_inr IS NOT NULL")
    elif priced is False:
        where.append("l.price_inr IS NULL")
    if min_price is not None:
        where.append("l.price_inr >= ?"); params.append(float(min_price))
    if max_price is not None:
        where.append("l.price_inr <= ?"); params.append(float(max_price))
    if publish_ready is True:
        where.append("p.publish_ready = 1")
    elif publish_ready is False:
        where.append("p.publish_ready = 0")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    # freshest price per product via a grouped subquery
    base = f"""
        FROM products p
        LEFT JOIN (
            SELECT product_id, price_inr, price_unit, basis,
                   ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY observed_at DESC) rn
            FROM price_observation
        ) l ON l.product_id = p.id AND l.rn = 1
        {clause}
    """
    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    order_col = _LIST_ORDER.get(order, "p.id")
    direction = "DESC" if desc else "ASC"
    rows = conn.execute(
        f"""SELECT p.id, p.brand, p.title, p.category, p.size_mm, p.finish,
                   p.price_unit, p.coverage_sqft_per_box, p.image_url, p.source_url,
                   p.supplier_domain, l.price_inr, l.basis AS price_basis,
                   p.completeness, p.verification_tier, p.publish_ready,
                   p.family, p.category_std, p.omniclass
            {base} ORDER BY {order_col} {direction}, p.id LIMIT ? OFFSET ?""",
        [*params, limit, offset]).fetchall()
    return {"total": total, "count": len(rows), "limit": limit, "offset": offset,
            "items": [dict(r) for r in rows]}
