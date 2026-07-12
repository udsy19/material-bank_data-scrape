import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from material_bank import db as db_mod
from material_bank.embeddings import FakeEmbedder
from material_bank.harvest.common import build_product
from material_bank.models import PriceBasis, PriceObservation
from material_bank.serve import create_app
from material_bank.vectorstore import NumpyVectorStore


def _jpeg():
    b = io.BytesIO(); Image.new("RGB", (4, 4), (200, 100, 60)).save(b, "JPEG"); return b.getvalue()


def _fake_prepare_image(url):
    return _jpeg()


@pytest.fixture()
def client(tmp_path):
    conn = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(conn)
    store = NumpyVectorStore(conn)
    emb = FakeEmbedder()
    items = [("Orientbell", "1", "Emperador Marble Glossy", "tiles", 84),
             ("Obeetee", "2", "Handwoven Jute Rug", "rugs", 69300),
             ("Jainsons", "3", "Brass Pendant Lamp", "lighting", 99000)]
    for brand, sku, title, cat, price in items:
        p = build_product(brand=brand, sku=sku, title=title, category=cat, source="t",
                          image_url="https://img/x.jpg" if brand != "Jainsons" else None)
        pid = db_mod.upsert_product(conn, p, supplier_domain=f"{brand.lower()}.com")
        db_mod.add_price_observation(conn, pid, PriceObservation(
            source=f"{brand.lower()}.com", price_inr=price, basis=PriceBasis.LISTED_MRP,
            observed_at=db_mod.now_iso(), source_url="u"))
        store.upsert(pid, "text", emb.encode_text([title])[0], emb.model_id)

    app = create_app(lambda: {"conn": conn, "store": store, "embedder": emb,
                              "prepare_image": _fake_prepare_image})
    tc = TestClient(app)
    tc.conn = conn   # exposed so tests can score/mutate directly
    yield tc
    conn.close()


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "DSource Material Bank" in r.text and "/api/match" in r.text


def test_stats_endpoint(client):
    s = client.get("/api/stats").json()
    assert s["products"] == 3 and s["products_priced"] == 3
    assert isinstance(s["top_suppliers"], list) and len(s["top_suppliers"]) == 3


def test_match_returns_priced_results(client):
    d = client.get("/api/match", params={"q": "marble tile"}).json()
    assert d["count"] >= 1
    top = d["results"][0]
    assert "Marble" in top["title"]
    assert top["price"]["price_inr"] == 84 and top["price"]["basis"] == "listed_mrp"


def test_match_empty_query(client):
    d = client.get("/api/match", params={"q": ""}).json()
    assert d["count"] == 0 and d["results"] == []


def test_product_detail_with_observations(client):
    pid = client.get("/api/match", params={"q": "jute rug"}).json()["results"][0]["id"]
    d = client.get(f"/api/product/{pid}").json()
    assert "Jute" in d["product"]["title"]
    assert d["product"]["price"]["price_inr"] == 69300
    assert len(d["observations"]) == 1

def test_product_404(client):
    assert client.get("/api/product/999999").status_code == 404


def test_image_proxy(client):
    r = client.get("/api/image", params={"url": "https://img/x.jpg"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"          # served from image_prep as JPEG
    assert "max-age" in r.headers.get("cache-control", "")


def test_image_proxy_rejects_bad_url(client):
    assert client.get("/api/image", params={"url": "file:///etc/passwd"}).status_code == 400


def test_pipeline_health_endpoint(client):
    d = client.get("/api/pipeline").json()
    assert "jobs" in d and "dead_letters" in d
    for s in ("pending", "running", "done", "failed"):
        assert s in d["jobs"]


def test_products_listing_and_filters(client):
    d = client.get("/api/products", params={"limit": 10}).json()
    assert d["total"] == 3 and len(d["items"]) == 3
    assert {"id", "title", "supplier_domain", "price_inr"} <= set(d["items"][0])
    # filter by supplier
    r = client.get("/api/products", params={"supplier": "orientbell.com"}).json()
    assert r["total"] == 1 and r["items"][0]["title"] == "Emperador Marble Glossy"
    # filter by category substring
    assert client.get("/api/products", params={"category": "lighting"}).json()["total"] == 1
    # price range
    hi = client.get("/api/products", params={"min_price": 50000}).json()
    assert all(i["price_inr"] >= 50000 for i in hi["items"]) and hi["total"] == 2


def test_products_pagination(client):
    p1 = client.get("/api/products", params={"limit": 2, "offset": 0}).json()
    p2 = client.get("/api/products", params={"limit": 2, "offset": 2}).json()
    assert p1["total"] == 3 and len(p1["items"]) == 2 and len(p2["items"]) == 1
    assert {i["id"] for i in p1["items"]}.isdisjoint({i["id"] for i in p2["items"]})


def test_products_order_by_price(client):
    d = client.get("/api/products", params={"order": "price", "desc": True}).json()
    prices = [i["price_inr"] for i in d["items"] if i["price_inr"] is not None]
    assert prices == sorted(prices, reverse=True)


def test_suppliers_endpoint(client):
    d = client.get("/api/suppliers").json()
    assert d["count"] == 3
    doms = {s["domain"]: s for s in d["suppliers"]}
    assert doms["orientbell.com"]["products"] == 1 and doms["orientbell.com"]["priced"] == 1


def test_catalog_is_publish_gated(client):
    # nothing scored yet -> the external catalog is empty (gate closed by default)
    assert client.get("/api/catalog").json()["total"] == 0
    # internal listing still shows everything, with trust fields exposed
    d = client.get("/api/products").json()
    assert d["total"] == 3 and "publish_ready" in d["items"][0]
    # after the planner scores, publishable records appear on the catalog surface
    from material_bank.quality import score_all
    score_all(client.conn)
    gated = client.get("/api/catalog").json()
    assert 0 < gated["total"] <= 3
    assert all(i["publish_ready"] == 1 for i in gated["items"])


def test_quality_endpoint_shape(client):
    q = client.get("/api/quality").json()
    for k in ("products", "publish_ready", "median_completeness", "tiers", "trend"):
        assert k in q
    assert q["products"] == 3


def test_llm_ops_page_served(client):
    r = client.get("/llm")
    assert r.status_code == 200
    assert "LLM enrichment" in r.text and "/api/llm/calls" in r.text
    assert 'data-cur="USD"' in r.text and 'data-cur="INR"' in r.text   # the toggle


def test_llm_ops_endpoints(client):
    from material_bank import llm_accounting as acct
    # empty ledger reads honest zeros
    r0 = client.get("/api/llm").json()
    assert r0["all_time"]["calls"] == 0 and r0["spend_today_inr"] == 0
    assert r0["rates"]["usd_inr"] == acct.USD_INR
    # log a couple of calls, then the cockpit + raw ledger reflect them
    acct.log_call(client.conn, product_id=1, model="gemini-2.5-flash", phase="realtime",
                  attempt=0, input_tokens=1000, output_tokens=400, status="enriched")
    acct.log_call(client.conn, product_id=1, model="gemini-2.5-flash", phase="realtime",
                  attempt=0, status="api_error", fail_reason="quota")
    client.conn.commit()
    r = client.get("/api/llm").json()
    assert r["all_time"]["calls"] == 2 and r["spend_today_inr"] > 0
    calls = client.get("/api/llm/calls", params={"limit": 10}).json()
    assert calls["total"] == 2 and calls["items"][0]["status"] in ("enriched", "api_error")
    assert client.get("/api/llm/calls", params={"status": "api_error"}).json()["total"] == 1


def test_supplier_detail_endpoint_and_embedded_block(client):
    conn = client.conn
    conn.execute("INSERT INTO suppliers (domain, brand, status, legal_name, phones, "
                 "gstin, states_served, dealer_count, pan_india) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("orientbell.com", "Orientbell", "active", "Orient Bell Limited",
                  '["1244623000"]', "09AABCO1234M1Z5", '["Maharashtra","Delhi"]', 42, 0))
    conn.execute("INSERT INTO dealers (supplier_domain, name, city, state, pincode, phone) "
                 "VALUES (?,?,?,?,?,?)",
                 ("orientbell.com", "Tile World", "Pune", "Maharashtra", "411001", "9800000000"))
    conn.commit()
    d = client.get("/api/supplier/orientbell.com").json()
    assert d["legal_name"] == "Orient Bell Limited" and d["gstin"].startswith("09")
    assert d["phones"] == ["1244623000"] and d["dealer_count"] == 42
    assert set(d["states_served"]) == {"Maharashtra", "Delhi"}
    assert d["dealers"][0]["name"] == "Tile World"
    assert d["products"] == 1                       # the Orientbell tile in the fixture
    assert client.get("/api/supplier/nope.com").status_code == 404
    # product detail embeds the (dealer-less) supplier block
    pid = client.get("/api/products", params={"supplier": "orientbell.com"}).json()["items"][0]["id"]
    prod = client.get(f"/api/product/{pid}").json()
    assert prod["supplier"]["brand"] == "Orientbell" and prod["supplier"]["dealer_count"] == 42
    assert "dealers" not in prod["supplier"]        # embedded block is lightweight


def test_demand_endpoints_event_quote_claim(client):
    # empty demand scorecard reads an honest zero
    d0 = client.get("/api/demand").json()
    assert d0["active_sessions"] == 0 and d0["quote_requests_total"] == 0
    # client logs a search + click
    client.post("/api/event", json={"kind": "search", "session_id": "s1", "query": "tile"})
    client.post("/api/event", json={"kind": "result_click", "session_id": "s1", "product_id": 1})
    client.post("/api/event", json={"kind": "bogus"})          # ignored, no error
    # intent capture
    q = client.post("/api/quote", json={"product_id": 1, "supplier_domain": "orientbell.com",
                                        "buyer_contact": "buyer@firm.example",
                                        "message": "need 20 boxes"}).json()
    assert q["ok"] and q["id"] >= 1
    # supplier claim/takedown
    c = client.post("/api/claim", json={"supplier_domain": "orientbell.com", "kind": "remove",
                                        "claimant_email": "legal@orientbell.com"}).json()
    assert c["ok"]
    assert client.post("/api/claim", json={"kind": "remove"}).status_code == 400   # missing domain
    d = client.get("/api/demand").json()
    assert d["active_sessions"] == 1 and d["searches"] == 1 and d["search_ctr"] == 1.0
    assert d["quote_requests_total"] == 1 and d["supplier_claims_total"] == 1


def test_catalog_collapse_and_product_variants(client):
    from material_bank.harvest.common import build_product
    from material_bank.models import PriceBasis, PriceObservation
    from material_bank.quality import score_all
    from material_bank.resolve import assign_variant_groups
    # two variants of one design (same title, distinct SKUs) + prices/specs so
    # they clear the gate
    ids = []
    for sku, size, price in [("dc-s", "1830x910", 8999), ("dc-q", "1980x1520", 14999)]:
        p = build_product(brand="Wakefit", sku=sku, title="Dual Comfort Mattress",
                          category="mattresses", source=f"https://w/{sku}",
                          image_url="https://img/m.jpg", size_mm=size, finish="Firm",
                          price_unit="per_piece", coverage_sqft_per_box=1.0)
        pid = db_mod.upsert_product(client.conn, p, supplier_domain="wakefit.co")
        db_mod.add_price_observation(client.conn, pid, PriceObservation(
            source="wakefit.co", price_inr=price, basis=PriceBasis.LISTED_MRP,
            observed_at=db_mod.now_iso(), source_url=f"https://w/{sku}"))
        ids.append(pid)
    score_all(client.conn)
    assign_variant_groups(client.conn)

    # collapsed catalog: the two SKUs show as ONE design card with a price band
    d = client.get("/api/catalog", params={"collapse": True, "supplier": "wakefit.co"}).json()
    assert d["total"] == 1
    card = d["items"][0]
    assert card["variant_count"] == 2
    assert card["min_price"] == 8999 and card["max_price"] == 14999

    # product detail exposes sibling variants with their own prices
    detail = client.get(f"/api/product/{ids[0]}").json()
    assert len(detail["variants"]) == 2
    assert {v["price"]["price_inr"] for v in detail["variants"]} == {8999, 14999}


def test_taxonomy_endpoint_and_family_filter(client):
    from material_bank.taxonomy import classify_all
    classify_all(client.conn)
    tree = client.get("/api/taxonomy").json()
    fams = {f["family"] for f in tree["families"]}
    assert {"Surfaces", "Flooring", "Lighting & Electrical"} <= fams
    # the fixture's tile is under Surfaces/Tiles
    surf = next(f for f in tree["families"] if f["family"] == "Surfaces")
    assert any(c["category"] == "Tiles" and c["omniclass"] == "23-35 50 14"
               for c in surf["categories"])
    # filter products by canonical family
    d = client.get("/api/products", params={"family": "Surfaces"}).json()
    assert d["total"] == 1 and d["items"][0]["family"] == "Surfaces"



def test_match_prewarms_result_thumbnails(tmp_path):
    """A search fires thumbnail warming for every result image_url, so cold
    below-the-fold cards are already cached when they scroll into view."""
    import time

    conn = db_mod.connect(tmp_path / "warm.db", check_same_thread=False)
    db_mod.migrate(conn)
    store = NumpyVectorStore(conn)
    emb = FakeEmbedder()
    p = build_product(brand="Obeetee", sku="9", title="Handwoven Jute Rug",
                      category="rugs", source="t", image_url="https://img/warm.jpg")
    pid = db_mod.upsert_product(conn, p, supplier_domain="obeetee.com")
    db_mod.add_price_observation(conn, pid, PriceObservation(
        source="obeetee.com", price_inr=69300, basis=PriceBasis.LISTED_MRP,
        observed_at=db_mod.now_iso(), source_url="u"))
    store.upsert(pid, "text", emb.encode_text(["Handwoven Jute Rug"])[0], emb.model_id)

    warmed: list[str] = []

    def _recording_prepare(url):
        warmed.append(url)
        return _jpeg()

    app = create_app(lambda: {"conn": conn, "store": store, "embedder": emb,
                              "prepare_image": _recording_prepare})
    tc = TestClient(app)
    r = tc.get("/api/match", params={"q": "jute rug", "k": 5})
    assert r.status_code == 200 and r.json()["count"] >= 1

    deadline = time.time() + 5
    while time.time() < deadline and "https://img/warm.jpg" not in warmed:
        time.sleep(0.05)
    assert "https://img/warm.jpg" in warmed
    conn.close()
