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
    yield TestClient(app)
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
