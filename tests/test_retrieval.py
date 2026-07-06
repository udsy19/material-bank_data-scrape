from datetime import datetime, timedelta, timezone

import pytest

from material_bank import db as db_mod
from material_bank.embeddings import FakeEmbedder
from material_bank.models import (
    FieldProvenance,
    NormalizedProduct,
    PriceBasis,
    PriceObservation,
    PriceUnit,
)
from material_bank.retrieval import (
    _fts_query,
    freshest_price,
    hybrid_search,
    keyword_search,
    stats,
)
from material_bank.vectorstore import NumpyVectorStore


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


def _add(conn, brand, sku, title, category="tiles", price=None, days_ago=0):
    p = NormalizedProduct(
        brand=brand, sku=sku, title=title, category=category,
        size_mm="600x600", finish="Matt", price_unit=PriceUnit.PER_SQFT,
        coverage_sqft_per_box=None, missing=["coverage_sqft_per_box"],
        provenance={f: FieldProvenance(source="t") for f in ("price_unit", "size_mm", "finish")})
    pid = db_mod.upsert_product(conn, p, supplier_domain=f"{brand.lower()}.com")
    if price is not None:
        obs_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        db_mod.add_price_observation(conn, pid, PriceObservation(
            source=f"{brand.lower()}.com", price_inr=price, price_unit=PriceUnit.PER_SQFT,
            basis=PriceBasis.LISTED_MRP, observed_at=obs_at, source_url="u"))
    return pid


def test_fts_query_sanitizes():
    assert _fts_query("glossy white tile!!") == '"glossy"* OR "white"* OR "tile"*'
    assert _fts_query("  ") == ""


def test_keyword_search_finds_by_title(conn):
    _add(conn, "Orientbell", "1", "Emperador Marble Glossy")
    _add(conn, "Somany", "2", "Rustic Wood Plank")
    hits = keyword_search(conn, "marble", k=10)
    assert len(hits) == 1
    row = conn.execute("SELECT title FROM products WHERE id=?", (hits[0],)).fetchone()
    assert "Marble" in row["title"]


def test_freshest_price_serves_latest_with_basis(conn):
    pid = _add(conn, "Orientbell", "1", "Tile", price=84, days_ago=10)
    # a newer observation at a different price
    db_mod.add_price_observation(conn, pid, PriceObservation(
        source="orientbell.com", price_inr=90, price_unit=PriceUnit.PER_SQFT,
        basis=PriceBasis.LISTED_MRP, observed_at=datetime.now(timezone.utc).isoformat(), source_url="u"))
    fp = freshest_price(conn, pid)
    assert fp["price_inr"] == 90 and fp["basis"] == "listed_mrp" and fp["stale"] is False


def test_freshest_price_flags_stale(conn):
    pid = _add(conn, "Orientbell", "1", "Tile", price=84, days_ago=120)
    fp = freshest_price(conn, pid)
    assert fp["stale"] is True and fp["age_days"] >= 120


def test_hybrid_search_fuses_and_prices(conn):
    store = NumpyVectorStore(conn)
    emb = FakeEmbedder()
    p1 = _add(conn, "Orientbell", "1", "Emperador Marble Glossy", price=84)
    _add(conn, "Somany", "2", "Rustic Wood Plank", price=70)
    # embed text so semantic arm contributes
    for pid, txt in [(p1, "Emperador Marble Glossy")]:
        store.upsert(pid, "text", emb.encode_text([txt])[0], emb.model_id)
    results = hybrid_search(conn, emb, store, "marble", k=5)
    assert results and results[0]["title"] == "Emperador Marble Glossy"
    assert results[0]["price"]["price_inr"] == 84
    assert results[0]["price"]["basis"] == "listed_mrp"


def test_hybrid_search_keyword_only_when_no_vectors(conn):
    _add(conn, "Orientbell", "1", "Blue Mosaic Kitchen", price=77)
    results = hybrid_search(conn, FakeEmbedder(), NumpyVectorStore(conn), "mosaic", k=5)
    assert len(results) == 1 and results[0]["price"]["price_inr"] == 77


def test_stats_shape(conn):
    _add(conn, "Orientbell", "1", "Tile", price=84)
    s = stats(conn)
    assert s["products"] == 1 and s["products_priced"] == 1 and "quarantine" in s


def test_fts_stays_in_sync_on_delete(conn):
    pid = _add(conn, "Orientbell", "1", "Deletable Marble")
    assert keyword_search(conn, "deletable", k=5) == [pid]
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    assert keyword_search(conn, "deletable", k=5) == []  # trigger kept FTS in sync
