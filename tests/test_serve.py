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


def _png():
    b = io.BytesIO(); Image.new("RGB", (4, 4), (200, 100, 60)).save(b, "PNG"); return b.getvalue()


def _fake_fetch_image(url):
    return _png(), "image/png"


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
                              "fetch_image": _fake_fetch_image})
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
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:4] == b"\x89PNG"


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
