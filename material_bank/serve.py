"""Serving layer (Stage 8): hybrid search API + coverage dashboard.

Factory-built so tests inject a fake embedder/store; production wires the real
marqo embedder and the numpy vector index over catalog.db.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
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


def _warm_one(prepare, url: str, inflight: set) -> None:
    try:
        prepare(url)
    except Exception:
        pass
    finally:
        inflight.discard(url)


def _prewarm_images(s: dict, urls: list[str]) -> None:
    """Fire-and-forget thumbnail warming for a result page. The browser lazy-loads
    only on-screen cards, so a cold query used to pay the origin fetch per card as
    it scrolled into view; warming every result the moment the query returns means
    the disk cache is already filled by the time a card is looked at."""
    pool = s.setdefault(
        "_img_pool", ThreadPoolExecutor(max_workers=8, thread_name_prefix="img-warm"))
    inflight: set = s.setdefault("_img_inflight", set())
    prepare = s["prepare_image"]
    for url in dict.fromkeys(u for u in urls if u):
        if url not in inflight:
            inflight.add(url)
            pool.submit(_warm_one, prepare, url, inflight)


def create_app(state_provider) -> FastAPI:
    """state_provider() -> dict(conn, store, embedder, fetcher). Called once, lazily."""
    app = FastAPI(title="DSource Material Bank")
    lock = threading.Lock()          # serializes the shared conn: writes + the embedder path
    _tls = threading.local()
    state: dict = {}

    def S() -> dict:
        if not state:
            state.update(state_provider())
        return state

    def rconn():
        """A per-thread READ connection. WAL SQLite serves many readers at once, so
        read endpoints no longer queue behind the single shared connection + global
        lock — this is what lets external systems fetch in parallel instead of one at
        a time. Falls back to the shared connection when no factory is wired (tests)."""
        factory = S().get("conn_factory")
        if factory is None:
            return S()["conn"]
        c = getattr(_tls, "conn", None)
        if c is None:
            c = factory()
            _tls.conn = c
        return c

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
        c = rconn()
        return {**stats(c), "top_suppliers": top_suppliers(c)}

    @app.get("/api/pipeline")
    def api_pipeline() -> dict:
        """Harvest-queue health so failures are visible, not buried."""
        c = rconn()
        try:
            return {"jobs": jobs.counts(c, "harvest"),
                    "dead_letters": jobs.dead_letters(c, "harvest"),
                    "repairs": jobs.counts(c, "repair")}
        except Exception:
            return {"jobs": {}, "dead_letters": [], "repairs": {}}  # pre-v6 db

    @app.get("/api/match")
    def api_match(q: str = Query("", min_length=0), k: int = Query(20, ge=1, le=60)) -> dict:
        s = S()
        if not q.strip():
            return {"query": q, "count": 0, "results": []}
        with lock:
            results = hybrid_search(s["conn"], s["embedder"], s["store"], q, k=k)
        _prewarm_images(s, [r.get("image_url") for r in results])
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
        return list_products(rconn(), supplier=supplier, category=category, brand=brand,
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
        c = rconn()
        if collapse:
            return list_designs(c, supplier=supplier, family=family,
                                category_std=category_std, q=q, min_price=min_price,
                                max_price=max_price, publish_ready=True,
                                limit=limit, offset=offset)
        return list_products(c, supplier=supplier, category=category, brand=brand,
                             family=family, category_std=category_std,
                             q=q, min_price=min_price, max_price=max_price,
                             publish_ready=True, order=order, desc=desc,
                             limit=limit, offset=offset)

    @app.get("/api/taxonomy")
    def api_taxonomy() -> dict:
        """The canonical tree with live counts — powers faceted browse."""
        from .taxonomy import taxonomy_tree
        tree = taxonomy_tree(rconn())
        return {"families": tree, "family_count": len(tree)}

    @app.get("/api/quality")
    def api_quality() -> dict:
        """The trust cockpit: live quality report + scorecard trend."""
        from .quality import metrics_trend, quality_report
        c = rconn()
        rep = quality_report(c)
        rep["trend"] = {
            "publish_ready": metrics_trend(c, "publish_ready", 30),
            "median_completeness": metrics_trend(c, "median_completeness", 30),
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
        return events.demand_metrics(rconn())

    @app.get("/api/llm")
    def api_llm() -> dict:
        """LLM-ops cockpit: spend (today/window/all-time), per-model + per-status,
        verifier pass-rate, daily series — every ₹ derived from actual tokens."""
        from . import llm_accounting as acct
        return acct.llm_report(rconn())

    @app.get("/api/llm/calls")
    def api_llm_calls(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                      status: str | None = None) -> dict:
        """The raw ledger — every call, newest first (product, tokens, ₹, status)."""
        from . import llm_accounting as acct
        return acct.recent_calls(rconn(), limit=limit, offset=offset, status=status)

    @app.get("/api/suppliers")
    def api_suppliers() -> dict:
        sup = list_suppliers(rconn())
        return {"count": len(sup), "suppliers": sup}

    @app.get("/api/supplier/{domain}")
    def api_supplier(domain: str) -> dict:
        """Procurement profile: who they are, how to reach them, where to buy."""
        d = supplier_detail(rconn(), domain)
        if d is None:
            raise HTTPException(404, "supplier not found")
        return d

    @app.get("/api/product/{pid}")
    def api_product(pid: int) -> dict:
        from .resolve import variants_of
        c = rconn()
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise HTTPException(404, "product not found")
        obs = [dict(o) for o in c.execute(
            "SELECT price_inr, price_unit, basis, observed_at, source, source_url "
            "FROM price_observation WHERE product_id=? ORDER BY observed_at DESC", (pid,))]
        similar = _similar(c, S(), pid)
        variants = variants_of(c, pid)
        product = dict(row)
        supplier = supplier_detail(c, row["supplier_domain"], with_dealers=False)
        product["price"] = freshest_price(c, pid)
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

    def _similar(c, s: dict, pid: int) -> list[dict]:
        store: NumpyVectorStore = s["store"]      # preloaded numpy arrays: read-only, thread-safe
        # prefer visual back-match if we have an image vector, else text
        for kind in ("image", "text"):
            row = c.execute(
                "SELECT vector FROM embeddings WHERE product_id=? AND kind=?", (pid, kind)).fetchone()
            if row is None:
                continue
            vec = np.frombuffer(row[0], dtype=np.float32)
            out = []
            for sid, score in store.search(vec, kind=kind, k=7):
                if sid == pid:
                    continue
                p = c.execute(
                    "SELECT id, title, image_url, supplier_domain FROM products WHERE id=?",
                    (sid,)).fetchone()
                if p:
                    out.append({**dict(p), "score": round(score, 4), "match": kind})
            return out[:6]
        return []

    return app


def default_state_provider() -> dict:
    import functools

    from . import image_prep
    from .embeddings import MarqoEmbedder

    conn = db.connect(check_same_thread=False)
    try:
        db.migrate(conn)   # api may boot before any worker after a deploy — own the schema
    except sqlite3.OperationalError:
        # ...but a locked migrate (a worker mid-write) must NEVER brick serving: the
        # schema is then already owned by that writer. Skip and serve reads.
        pass
    store = NumpyVectorStore(conn)
    store.preload("text")
    store.preload("image")
    embedder = MarqoEmbedder()
    embedder.encode_text(["warmup"])  # load model weights before first request
    # conn_factory gives each request thread its own reader so WAL SQLite serves many
    # consumers concurrently (no global lock on the read path). connect_reader skips the
    # journal_mode pragma (which needs a write lock and would block behind the probe/
    # harvest/enrich writers) and is query_only (can't corrupt the catalog).
    conn_factory = db.connect_reader  # noqa: E731
    # Serving uses a short fetch timeout: a dead/slow origin fails fast (and gets
    # its .miss memo) instead of pinning a dashboard image lane for 20s.
    return {"conn": conn, "conn_factory": conn_factory, "store": store, "embedder": embedder,
            "prepare_image": functools.partial(
                image_prep.prepare_image, fetch=image_prep.make_fetch(6.0))}


app = create_app(default_state_provider)
