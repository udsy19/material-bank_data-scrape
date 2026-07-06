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
    }


def top_suppliers(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT supplier_domain AS domain, COUNT(*) AS products FROM products "
        "GROUP BY supplier_domain ORDER BY products DESC LIMIT ?", (limit,))]
