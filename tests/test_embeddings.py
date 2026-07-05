import numpy as np
import pytest

from material_bank import db as db_mod
from material_bank.embeddings import (
    EMBED_DIM,
    FakeEmbedder,
    embed_catalog_text,
    product_text,
)
from material_bank.models import FieldProvenance, NormalizedProduct, PriceUnit
from material_bank.vectorstore import (
    NumpyVectorStore,
    from_blob,
    normalize,
    to_blob,
)


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


def _surface(brand, sku, title, size="600x600", finish="Matt"):
    return NormalizedProduct(
        brand=brand, sku=sku, title=title, category="tiles",
        size_mm=size, finish=finish, price_unit=PriceUnit.PER_SQFT,
        coverage_sqft_per_box=None, missing=["coverage_sqft_per_box"],
        provenance={f: FieldProvenance(source="t") for f in ("price_unit", "size_mm", "finish")},
    )


def test_v4_creates_embeddings_and_image_url(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
    assert "image_url" in cols
    ecols = {r["name"] for r in conn.execute("PRAGMA table_info(embeddings)")}
    assert {"product_id", "kind", "vector", "dim", "model"} <= ecols
    assert db_mod.get_schema_version(conn) == db_mod.SCHEMA_VERSION


def test_blob_roundtrip_and_normalize():
    v = np.array([3.0, 4.0], dtype=np.float32)
    assert np.allclose(from_blob(to_blob(v)), v)
    assert np.isclose(np.linalg.norm(normalize(v)), 1.0)


def test_store_search_ranks_by_cosine(conn):
    pid = db_mod.upsert_product(conn, _surface("B", "1", "x"))
    store = NumpyVectorStore(conn)
    # three orthogonal-ish vectors; query closest to v2
    store.upsert(pid, "text", np.array([1, 0, 0], dtype=np.float32), "m")
    pid2 = db_mod.upsert_product(conn, _surface("B", "2", "y"))
    store.upsert(pid2, "text", np.array([0, 1, 0], dtype=np.float32), "m")
    pid3 = db_mod.upsert_product(conn, _surface("B", "3", "z"))
    store.upsert(pid3, "text", np.array([0.9, 0.1, 0], dtype=np.float32), "m")

    hits = store.search(np.array([1, 0, 0], dtype=np.float32), kind="text", k=3)
    assert hits[0][0] == pid          # exact match ranks first
    assert hits[1][0] == pid3         # near match second
    assert hits[0][1] > hits[1][1] > hits[2][1]


def test_upsert_is_idempotent_per_kind(conn):
    pid = db_mod.upsert_product(conn, _surface("B", "1", "x"))
    store = NumpyVectorStore(conn)
    store.upsert(pid, "text", np.ones(EMBED_DIM, dtype=np.float32), "m")
    store.upsert(pid, "text", np.ones(EMBED_DIM, dtype=np.float32), "m")  # again
    assert store.count("text") == 1                                       # no dupe
    store.upsert(pid, "image", np.ones(EMBED_DIM, dtype=np.float32), "m")  # other kind
    assert store.count("image") == 1 and store.count("text") == 1


def test_text_and_image_share_one_index(conn):
    # Shared space: a text vector can retrieve an image vector of the same item.
    pid = db_mod.upsert_product(conn, _surface("B", "1", "x"))
    store = NumpyVectorStore(conn)
    shared = normalize(np.arange(EMBED_DIM, dtype=np.float32))
    store.upsert(pid, "image", shared, "m")
    hits = store.search(shared, kind="image", k=1)
    assert hits[0][0] == pid and hits[0][1] > 0.99


def test_product_text_composition(conn):
    db_mod.upsert_product(conn, _surface("Orientbell", "9", "Statuario Marble", "800x800", "Glossy"))
    row = conn.execute("SELECT * FROM products WHERE sku='9'").fetchone()
    txt = product_text(row)
    assert "Statuario Marble" in txt and "800x800" in txt and "Glossy finish" in txt


def test_embed_catalog_text_is_resumable(conn):
    for i in range(5):
        db_mod.upsert_product(conn, _surface("B", str(i), f"tile {i}"))
    store = NumpyVectorStore(conn)
    emb = FakeEmbedder()
    r1 = embed_catalog_text(conn, emb, store, batch_size=2)
    assert r1["embedded"] == 5 and store.count("text") == 5
    r2 = embed_catalog_text(conn, emb, store)   # resume: nothing new
    assert r2["embedded"] == 0


def test_fake_embedder_deterministic_and_normalized():
    emb = FakeEmbedder()
    a = emb.encode_text(["glossy tile"])
    b = emb.encode_text(["glossy tile"])
    assert np.allclose(a, b)                      # deterministic
    assert np.isclose(np.linalg.norm(a[0]), 1.0)  # normalized
    assert a.shape == (1, EMBED_DIM)
