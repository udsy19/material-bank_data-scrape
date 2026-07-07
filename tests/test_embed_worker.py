"""Continuous embed worker — consumes newly-harvested products across passes."""

from material_bank import db as db_mod
from material_bank.embed_worker import run_embed_worker
from material_bank.embeddings import FakeEmbedder
from material_bank.harvest.common import build_product


def _add(conn, n, start=0):
    for i in range(start, start + n):
        db_mod.upsert_product(conn, build_product(
            brand="B", sku=f"sku{i}", title=f"chair {i}", category="furniture", source="s"),
            supplier_domain="b.com")


def test_embeds_new_products_each_pass(tmp_path):
    path = tmp_path / "catalog.db"
    c = db_mod.connect(path, check_same_thread=False)
    db_mod.migrate(c)
    _add(c, 5)

    # a "harvester" adds 3 more between passes, simulated via the sleep hook
    state = {"added": False}

    def fake_sleep(_):
        if not state["added"]:
            _add(c, 3, start=5)
            state["added"] = True

    rep = run_embed_worker(path, max_passes=2, poll_interval=0,
                           embedder_factory=FakeEmbedder, sleep=fake_sleep)
    assert rep["passes"] == 2
    assert rep["embedded_total"] == 8            # 5 first pass + 3 the harvester added
    v = db_mod.connect(path)
    assert v.execute("SELECT COUNT(*) FROM embeddings WHERE kind='text'").fetchone()[0] == 8
    # FTS kept in sync so new rows are keyword-searchable
    assert v.execute("SELECT COUNT(*) FROM products_fts").fetchone()[0] == 8
    v.close()
    c.close()


def test_idle_when_nothing_new(tmp_path):
    path = tmp_path / "catalog.db"
    c = db_mod.connect(path, check_same_thread=False)
    db_mod.migrate(c)
    _add(c, 3)
    rep = run_embed_worker(path, max_passes=3, poll_interval=0,
                           embedder_factory=FakeEmbedder, sleep=lambda _: None)
    assert rep["embedded_total"] == 3    # embedded once
    assert rep["idle_passes"] >= 1       # subsequent passes found nothing new
    c.close()
