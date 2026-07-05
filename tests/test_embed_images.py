"""Image-embed backfill tests — offline (fake fetcher + fake embedder + a real
tiny in-memory PNG so PIL decode is exercised)."""

import io

import pytest
from PIL import Image

from material_bank import db as db_mod
from material_bank.embeddings import FakeEmbedder
from material_bank.fetch import FetchResult
from material_bank.harvest.images import embed_images
from material_bank.models import FieldProvenance, NormalizedProduct, PriceUnit
from material_bank.vectorstore import NumpyVectorStore


def _png_bytes(color=(180, 120, 90)):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


class _ImageFetcher:
    """Serves a PNG for image URLs; 404 for a designated broken one."""

    def __init__(self, broken=()):
        self.broken = set(broken)

    def get(self, url):
        if url in self.broken:
            return FetchResult(requested_url=url, status_code=404, final_url=url)
        return FetchResult(requested_url=url, status_code=200,
                           content=_png_bytes(), final_url=url)


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


def _tile(sku, img="https://images.x/%s.jpg"):
    return NormalizedProduct(
        brand="Orientbell", sku=sku, title=f"tile {sku}", category="tiles",
        size_mm="600x600", finish="Matt", price_unit=PriceUnit.PER_SQFT,
        coverage_sqft_per_box=None, missing=["coverage_sqft_per_box"],
        image_url=(img % sku),
        provenance={f: FieldProvenance(source="t") for f in ("price_unit", "size_mm", "finish")},
    )


def test_embed_images_stores_vectors(conn):
    for i in range(3):
        db_mod.upsert_product(conn, _tile(str(i)))
    store = NumpyVectorStore(conn)
    stats = embed_images(conn, FakeEmbedder(), store,
                         image_fetcher=_ImageFetcher(), pdp_fetcher=_ImageFetcher())
    assert stats["embedded"] == 3
    assert store.count("image") == 3


def test_embed_images_resumable(conn):
    for i in range(3):
        db_mod.upsert_product(conn, _tile(str(i)))
    store = NumpyVectorStore(conn)
    f = _ImageFetcher()
    embed_images(conn, FakeEmbedder(), store, image_fetcher=f, pdp_fetcher=f)
    stats2 = embed_images(conn, FakeEmbedder(), store, image_fetcher=f, pdp_fetcher=f)
    assert stats2["targets"] == 0            # all already image-embedded
    assert store.count("image") == 3         # no dupes


def test_embed_images_quarantines_broken_image(conn):
    db_mod.upsert_product(conn, _tile("good"))
    db_mod.upsert_product(conn, _tile("bad"))
    store = NumpyVectorStore(conn)
    broken = "https://images.x/bad.jpg"
    stats = embed_images(conn, FakeEmbedder(), store,
                         image_fetcher=_ImageFetcher(broken=[broken]),
                         pdp_fetcher=_ImageFetcher())
    assert stats["embedded"] == 1 and stats["quarantined"] == 1
    q = conn.execute("SELECT stage, source_url FROM quarantine WHERE stage='image'").fetchone()
    assert q["source_url"] == broken


def test_missing_image_url_resolved_from_pdp(conn):
    # Row has no image_url; resolver re-parses the PDP (via price_observation source_url).
    from material_bank.models import PriceBasis, PriceObservation
    pid = db_mod.upsert_product(conn, NormalizedProduct(
        brand="Orientbell", sku="noimg", title="tile", category="tiles",
        size_mm="600x600", finish="Matt", price_unit=PriceUnit.PER_SQFT,
        coverage_sqft_per_box=None, missing=["coverage_sqft_per_box"], image_url=None,
        provenance={f: FieldProvenance(source="t") for f in ("price_unit", "size_mm", "finish")}))
    conn.execute("UPDATE products SET image_url=NULL WHERE id=?", (pid,))
    db_mod.add_price_observation(conn, pid, PriceObservation(
        price_inr=80, basis=PriceBasis.LISTED_MRP, observed_at=db_mod.now_iso(),
        source_url="https://www.orientbell.com/noimg"))
    conn.commit()

    pdp_html = ('<script type="application/ld+json">{"@type":"Product","brand":"Orientbell",'
                '"name":"tile","image":["https://images.x/noimg.jpg"],'
                '"offers":{"price":"80","itemOffered":"/sqft"}}</script><div data-sku="noimg"></div>')

    class _PdpFetcher:
        def get(self, url):
            if "orientbell.com/noimg" in url and "images" not in url:
                return FetchResult(requested_url=url, status_code=200, text=pdp_html, final_url=url)
            return FetchResult(requested_url=url, status_code=200, content=_png_bytes(), final_url=url)

    store = NumpyVectorStore(conn)
    stats = embed_images(conn, FakeEmbedder(), store,
                         image_fetcher=_PdpFetcher(), pdp_fetcher=_PdpFetcher())
    assert stats["embedded"] == 1
    # image_url got backfilled onto the row
    assert conn.execute("SELECT image_url FROM products WHERE id=?", (pid,)).fetchone()[0] \
        == "https://images.x/noimg.jpg"
