"""Batch LLM pipeline: request building, submit/collect orchestration, verify."""

import json

import pytest

from material_bank import db as db_mod
from material_bank import llm_batch as lb


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "c.db", check_same_thread=False)
    db_mod.migrate(c)
    from material_bank.harvest.common import build_product
    for sku in ("t1", "t2"):
        db_mod.upsert_product(c, build_product(brand="Orientbell", sku=sku,
            title="Emperador Marble Tile", category="tiles", source="u",
            image_url="https://i/x.jpg", size_mm="600x600"), supplier_domain="orientbell.com")
    c.commit()
    yield c
    c.close()


def _good_json():
    return json.dumps({
        "description": [{"text": "A polished vitrified tile with soft veining.", "sources": ["f1", "f4"]}],
        "style_tags": [{"tag": "modern", "sources": ["img1"]}],
        "use_case_tags": [{"tag": "flooring", "sources": ["img1"]}],
        "vision": {"colour_primary": {"value": "Brown", "confidence": 0.6}}})


class FakeTransport:
    def __init__(self, results): self._results = results; self.submitted = None
    def submit(self, requests): self.submitted = requests; return "operations/batch-123"
    def results(self, job_name): return self._results


def test_build_batch_request_shape(conn):
    row = conn.execute(f"SELECT id, {', '.join(lb._INPUT_FIELDS)}, image_url FROM products LIMIT 1").fetchone()
    req = lb.build_batch_request(row, prepare=lambda u: None)
    assert req["key"] == str(row["id"])
    assert req["request"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "Emperador" in req["request"]["contents"][0]["parts"][0]["text"]


def test_submit_marks_rows_batched(conn):
    t = FakeTransport([])
    out = lb.submit_batch(conn, transport=t, limit=10, prepare=lambda u: None)
    assert out["count"] == 2 and out["job_name"] == "operations/batch-123"
    assert len(t.submitted) == 2
    assert conn.execute("SELECT COUNT(*) FROM products WHERE llm_status='batched'").fetchone()[0] == 2
    # a second submit finds nothing new (already batched)
    assert lb.submit_batch(conn, transport=t, limit=10, prepare=lambda u: None)["count"] == 0


def test_collect_verifies_and_writes(conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM products ORDER BY id")]
    results = [
        {"key": str(ids[0]), "text": _good_json()},                 # passes -> enriched
        {"key": str(ids[1]), "text": json.dumps({"description": [   # fabricated standard -> reject
            {"text": "Certified to ISO 13006.", "sources": ["f1"]}],
            "style_tags": [], "use_case_tags": [], "vision": {}})},
    ]
    out = lb.collect_batch(conn, "operations/batch-123", transport=FakeTransport(results))
    assert out["enriched"] == 1 and out["failed"] == 1
    r0 = conn.execute("SELECT llm_status, llm_content FROM products WHERE id=?", (ids[0],)).fetchone()
    assert r0["llm_status"] == "enriched"
    assert json.loads(r0["llm_content"])["_meta"]["basis"].startswith("generated:llm:")
    assert conn.execute("SELECT llm_status FROM products WHERE id=?", (ids[1],)).fetchone()[0] == "enrich_failed"


def test_collect_handles_error_result(conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM products ORDER BY id")]
    out = lb.collect_batch(conn, "j", transport=FakeTransport([{"key": str(ids[0]), "error": "quota"}]))
    assert out["failed"] == 1
    assert conn.execute("SELECT llm_status FROM products WHERE id=?", (ids[0],)).fetchone()[0] == "enrich_failed"
