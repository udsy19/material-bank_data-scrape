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
    def __init__(self, results): self._results = results; self.submitted = None; self._n = 0
    def submit(self, requests):
        self.submitted = requests; self._n += 1
        return f"operations/batch-{self._n}"
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
    assert out["count"] == 2 and out["job_name"] == "operations/batch-1"
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


def test_submit_records_job_and_submit_all_chunks(conn):
    t = FakeTransport([])
    lb.submit_batch(conn, transport=t, limit=1, prepare=lambda u: None)
    j = conn.execute("SELECT status, product_count, prompt_version FROM llm_batch_jobs").fetchone()
    assert j["status"] == "submitted" and j["product_count"] == 1
    out = lb.submit_all(conn, transport=t, chunk=1, prepare=lambda u: None)  # chunk the rest
    assert out["products"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM llm_batch_jobs").fetchone()[0] >= 2


def test_advance_stops_cleanly_on_quota_429(conn):
    """A 429 mid-tick must end submission without crashing or marking rows — those
    products stay eligible for the next tick."""
    class QuotaT:
        def __init__(self): self._n = 0
        def submit(self, reqs):
            self._n += 1
            if self._n > 1:
                raise RuntimeError("batch submit 429: RESOURCE_EXHAUSTED")
            return "operations/j1"
        def results(self, job): raise RuntimeError("not done yet")   # nothing to collect
    out = lb.advance(conn, transport=QuotaT(), chunk=1, prepare=lambda u: None)
    assert out["jobs_submitted"] == 1                    # one landed, then quota stopped it
    assert out["remaining_unbatched"] >= 1               # the rest untouched, still eligible
    assert conn.execute("SELECT COUNT(*) FROM llm_batch_jobs").fetchone()[0] == 1


def test_advance_drains_until_exhausted(conn):
    """With no quota limit, advance keeps submitting until the catalog is empty."""
    class OpenT:
        def __init__(self): self._n = 0
        def submit(self, reqs): self._n += 1; return f"operations/j{self._n}"
        def results(self, job): raise RuntimeError("not done yet")
    out = lb.advance(conn, transport=OpenT(), chunk=1, prepare=lambda u: None)
    assert out["remaining_unbatched"] == 0
    assert out["jobs_submitted"] == 2                    # two products, chunk=1


def test_collect_pending_ingests_finished_jobs(conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM products ORDER BY id")]

    class T:
        def submit(self, reqs): return "operations/j1"
        def results(self, job): return [{"key": str(ids[0]), "text": _good_json(),
                                         "usage": {"input_tokens": 100, "output_tokens": 50}}]
    t = T()
    lb.submit_batch(conn, transport=t, limit=1, prepare=lambda u: None)
    out = lb.collect_pending(conn, transport=t)
    assert out["jobs_ingested"] == 1
    assert conn.execute("SELECT status FROM llm_batch_jobs").fetchone()[0] == "ingested"
    assert conn.execute("SELECT COUNT(*) FROM products WHERE llm_status='enriched'").fetchone()[0] == 1
    # idempotent: a second collect finds nothing pending
    assert lb.collect_pending(conn, transport=t)["jobs_ingested"] == 0
