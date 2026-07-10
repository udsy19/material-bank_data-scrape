"""Serving layer (Stage 8): hybrid search API + coverage dashboard.

Factory-built so tests inject a fake embedder/store; production wires the real
marqo embedder and the numpy vector index over catalog.db.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from . import db, jobs
from .retrieval import (
    freshest_price,
    hybrid_search,
    list_designs,
    list_products,
    list_suppliers,
    stats,
    top_suppliers,
)
from .vectorstore import NumpyVectorStore

_STATIC = Path(__file__).resolve().parent / "static"
_ALLOWED_IMAGE_HOSTS = None  # None = allow any http(s); tighten if needed


def create_app(state_provider) -> FastAPI:
    """state_provider() -> dict(conn, store, embedder, fetcher). Called once, lazily."""
    app = FastAPI(title="DSource Material Bank")
    lock = threading.Lock()
    state: dict = {}

    def S() -> dict:
        if not state:
            state.update(state_provider())
        return state

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return (_STATIC / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/stats")
    def api_stats() -> dict:
        s = S()
        with lock:
            return {**stats(s["conn"]), "top_suppliers": top_suppliers(s["conn"])}

    @app.get("/api/pipeline")
    def api_pipeline() -> dict:
        """Harvest-queue health so failures are visible, not buried."""
        s = S()
        with lock:
            try:
                return {"jobs": jobs.counts(s["conn"], "harvest"),
                        "dead_letters": jobs.dead_letters(s["conn"], "harvest"),
                        "repairs": jobs.counts(s["conn"], "repair")}
            except Exception:
                return {"jobs": {}, "dead_letters": [], "repairs": {}}  # pre-v6 db

    @app.get("/api/match")
    def api_match(q: str = Query("", min_length=0), k: int = Query(20, ge=1, le=60)) -> dict:
        s = S()
        if not q.strip():
            return {"query": q, "count": 0, "results": []}
        with lock:
            results = hybrid_search(s["conn"], s["embedder"], s["store"], q, k=k)
        return {"query": q, "count": len(results), "results": results}

    @app.get("/api/products")
    def api_products(
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
        order: str = Query("id", pattern="^(id|price|title|brand)$"),
        desc: bool = False,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict:
        """Full internal listing (includes in-enrichment records; trust fields exposed)."""
        s = S()
        with lock:
            return list_products(s["conn"], supplier=supplier, category=category, brand=brand,
                                 family=family, category_std=category_std,
                                 q=q, priced=priced, has_image=has_image, min_price=min_price,
                                 max_price=max_price, publish_ready=publish_ready,
                                 order=order, desc=desc, limit=limit, offset=offset)

    @app.get("/api/catalog")
    def api_catalog(
        supplier: str | None = None,
        category: str | None = None,
        family: str | None = None,
        category_std: str | None = None,
        brand: str | None = None,
        q: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        collapse: bool = False,
        order: str = Query("id", pattern="^(id|price|title|brand)$"),
        desc: bool = False,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict:
        """The B2B catalog surface: publish-gated — only verified-complete records.

        ``collapse=true`` returns one card per design (variants grouped, with a
        price band + variant count) instead of one row per SKU.
        """
        s = S()
        with lock:
            if collapse:
                return list_designs(s["conn"], supplier=supplier, family=family,
                                    category_std=category_std, q=q, min_price=min_price,
                                    max_price=max_price, publish_ready=True,
                                    limit=limit, offset=offset)
            return list_products(s["conn"], supplier=supplier, category=category, brand=brand,
                                 family=family, category_std=category_std,
                                 q=q, min_price=min_price, max_price=max_price,
                                 publish_ready=True, order=order, desc=desc,
                                 limit=limit, offset=offset)

    @app.get("/api/taxonomy")
    def api_taxonomy() -> dict:
        """The canonical tree with live counts — powers faceted browse."""
        from .taxonomy import taxonomy_tree
        s = S()
        with lock:
            tree = taxonomy_tree(s["conn"])
        return {"families": tree, "family_count": len(tree)}

    @app.get("/api/quality")
    def api_quality() -> dict:
        """The trust cockpit: live quality report + scorecard trend."""
        from .quality import metrics_trend, quality_report
        s = S()
        with lock:
            rep = quality_report(s["conn"])
            rep["trend"] = {
                "publish_ready": metrics_trend(s["conn"], "publish_ready", 30),
                "median_completeness": metrics_trend(s["conn"], "median_completeness", 30),
            }
        return rep

    @app.get("/api/suppliers")
    def api_suppliers() -> dict:
        s = S()
        with lock:
            sup = list_suppliers(s["conn"])
        return {"count": len(sup), "suppliers": sup}

    @app.get("/api/product/{pid}")
    def api_product(pid: int) -> dict:
        from .resolve import variants_of
        s = S()
        with lock:
            row = s["conn"].execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if row is None:
                raise HTTPException(404, "product not found")
            obs = [dict(o) for o in s["conn"].execute(
                "SELECT price_inr, price_unit, basis, observed_at, source, source_url "
                "FROM price_observation WHERE product_id=? ORDER BY observed_at DESC", (pid,))]
            similar = _similar(s, pid)
            variants = variants_of(s["conn"], pid)
            product = dict(row)
        product["price"] = freshest_price(s["conn"], pid)
        return {"product": product, "observations": obs,
                "variants": variants, "similar": similar}

    @app.get("/api/image")
    def api_image(url: str = Query(...)) -> Response:
        # NB: no DB lock here — image fetches are network-bound and must run
        # concurrently, or 20+ cards load one-at-a-time.
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "bad url")
        content, ctype = S()["fetch_image"](url)
        if not content:
            raise HTTPException(404, "image unavailable")
        return Response(content=content, media_type=ctype or "image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

    def _similar(s: dict, pid: int) -> list[dict]:
        store: NumpyVectorStore = s["store"]
        # prefer visual back-match if we have an image vector, else text
        for kind in ("image", "text"):
            row = s["conn"].execute(
                "SELECT vector FROM embeddings WHERE product_id=? AND kind=?", (pid, kind)).fetchone()
            if row is None:
                continue
            vec = np.frombuffer(row[0], dtype=np.float32)
            out = []
            for sid, score in store.search(vec, kind=kind, k=7):
                if sid == pid:
                    continue
                p = s["conn"].execute(
                    "SELECT id, title, image_url, supplier_domain FROM products WHERE id=?",
                    (sid,)).fetchone()
                if p:
                    out.append({**dict(p), "score": round(score, 4), "match": kind})
            return out[:6]
        return []

    return app


def _live_fetch_image(url: str) -> tuple[bytes | None, str | None]:
    """Per-call curl_cffi GET — independent handle per request, thread-safe,
    so many card images load concurrently."""
    try:
        from curl_cffi import requests
        r = requests.get(url, impersonate="chrome131", timeout=15, allow_redirects=True)
        if r.status_code and 200 <= r.status_code < 300 and r.content:
            return r.content, r.headers.get("content-type", "image/jpeg")
    except Exception:
        pass
    return None, None


def default_state_provider() -> dict:
    from .embeddings import MarqoEmbedder

    conn = db.connect(check_same_thread=False)
    db.migrate(conn)   # api may boot before any worker after a deploy — own the schema
    store = NumpyVectorStore(conn)
    store.preload("text")
    store.preload("image")
    embedder = MarqoEmbedder()
    embedder.encode_text(["warmup"])  # load model weights before first request
    return {"conn": conn, "store": store, "embedder": embedder,
            "fetch_image": _live_fetch_image}


app = create_app(default_state_provider)
