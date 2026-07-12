"""Serving layer (Stage 8): hybrid search API + coverage dashboard.

Factory-built so tests inject a fake embedder/store; production wires the real
marqo embedder and the numpy vector index over catalog.db.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from . import db, jobs
from .retrieval import (
    freshest_price,
    hybrid_search,
    list_designs,
    list_products,
    list_suppliers,
    stats,
    supplier_detail,
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

    @app.get("/llm", response_class=HTMLResponse)
    def llm_ops() -> str:
        """Standalone LLM-ops page: spend cockpit + call ledger, USD/INR toggle."""
        return (_STATIC / "llm.html").read_text(encoding="utf-8")

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

    @app.post("/api/event")
    def api_event(payload: dict = Body(...)) -> dict:
        """Demand instrumentation — the client logs search / view / click here."""
        from . import events
        s = S()
        with lock:
            events.log_event(s["conn"], (payload.get("kind") or ""),
                             session_id=payload.get("session_id"), query=payload.get("query"),
                             product_id=payload.get("product_id"),
                             supplier_domain=payload.get("supplier_domain"),
                             meta=payload.get("meta"))
        return {"ok": True}

    @app.post("/api/quote")
    def api_quote(payload: dict = Body(...)) -> dict:
        """Intent capture — a buyer asks to source a product. The signal Act III sells."""
        from . import events
        s = S()
        with lock:
            qid = events.record_quote(
                s["conn"], product_id=payload.get("product_id"),
                supplier_domain=payload.get("supplier_domain"),
                source_url=payload.get("source_url"), buyer_name=payload.get("buyer_name"),
                buyer_contact=payload.get("buyer_contact"), message=payload.get("message"))
        return {"ok": True, "id": qid}

    @app.post("/api/claim")
    def api_claim(payload: dict = Body(...)) -> dict:
        """Supplier claim / correct / takedown — turns an objection into an onboarding."""
        from . import events
        dom = (payload.get("supplier_domain") or "").strip()
        kind = (payload.get("kind") or "").strip()
        if not dom or kind not in {"claim", "correct", "remove"}:
            raise HTTPException(400, "supplier_domain and a valid kind are required")
        s = S()
        with lock:
            cid = events.record_claim(s["conn"], supplier_domain=dom, kind=kind,
                                      claimant_email=payload.get("claimant_email"),
                                      message=payload.get("message"))
        return {"ok": True, "id": cid}

    @app.get("/api/demand")
    def api_demand() -> dict:
        """The demand-side scorecard — zero until there are users, honestly."""
        from . import events
        s = S()
        with lock:
            return events.demand_metrics(s["conn"])

    @app.get("/api/llm")
    def api_llm() -> dict:
        """LLM-ops cockpit: spend (today/window/all-time), per-model + per-status,
        verifier pass-rate, daily series — every ₹ derived from actual tokens."""
        from . import llm_accounting as acct
        s = S()
        with lock:
            return acct.llm_report(s["conn"])

    @app.get("/api/llm/calls")
    def api_llm_calls(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                      status: str | None = None) -> dict:
        """The raw ledger — every call, newest first (product, tokens, ₹, status)."""
        from . import llm_accounting as acct
        s = S()
        with lock:
            return acct.recent_calls(s["conn"], limit=limit, offset=offset, status=status)

    @app.get("/api/suppliers")
    def api_suppliers() -> dict:
        s = S()
        with lock:
            sup = list_suppliers(s["conn"])
        return {"count": len(sup), "suppliers": sup}

    @app.get("/api/supplier/{domain}")
    def api_supplier(domain: str) -> dict:
        """Procurement profile: who they are, how to reach them, where to buy."""
        s = S()
        with lock:
            d = supplier_detail(s["conn"], domain)
        if d is None:
            raise HTTPException(404, "supplier not found")
        return d

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
            supplier = supplier_detail(s["conn"], row["supplier_domain"], with_dealers=False)
        product["price"] = freshest_price(s["conn"], pid)
        return {"product": product, "observations": obs, "variants": variants,
                "supplier": supplier, "similar": similar}

    @app.get("/api/image")
    def api_image(url: str = Query(...)) -> Response:
        # Served through image_prep: a content-addressed disk cache of ≤384px JPEGs
        # (shared with enrichment). Cache hit = a ~1ms disk read; miss = fetch+resize
        # +cache once; dead URLs are memoized so they fail fast instead of re-timing
        # out every page load. No DB lock — reads run fully concurrent across cards.
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "bad url")
        jpeg = S()["prepare_image"](url)
        if not jpeg:
            raise HTTPException(404, "image unavailable")
        return Response(content=jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})

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


def default_state_provider() -> dict:
    from . import image_prep
    from .embeddings import MarqoEmbedder

    conn = db.connect(check_same_thread=False)
    db.migrate(conn)   # api may boot before any worker after a deploy — own the schema
    store = NumpyVectorStore(conn)
    store.preload("text")
    store.preload("image")
    embedder = MarqoEmbedder()
    embedder.encode_text(["warmup"])  # load model weights before first request
    return {"conn": conn, "store": store, "embedder": embedder,
            "prepare_image": image_prep.prepare_image}


app = create_app(default_state_provider)
