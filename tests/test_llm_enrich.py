"""LLM enrichment: the deterministic verifiers (anti-fabrication core) + run loop."""

import json

import pytest

from material_bank import db as db_mod
from material_bank import llm_enrich as le

FMAP = {"f1": ("title", "Emperador Marble Tile"), "f2": ("category_std", "Tiles"),
        "f3": ("size_mm", "600x600")}
INPUT = "f1 [title]: Emperador Marble Tile\nf2 [category_std]: Tiles\nf3 [size_mm]: 600x600"


def _good():
    return {
        "description": [
            {"text": "A marble-look vitrified tile.", "sources": ["f1", "f2"]},
            {"text": "Warm brown veining across the surface.", "sources": ["img1"]},
        ],
        "style_tags": [{"tag": "modern", "sources": ["img1"]}],
        "use_case_tags": [{"tag": "flooring", "sources": ["f2"]}],
        "vision": {"colour_primary": {"value": "Brown", "confidence": 0.7},
                   "material_look": {"value": "marble", "confidence": 0.8}},
    }


def test_good_output_passes():
    assert le.verify(_good(), FMAP, INPUT) == []


def test_uncited_sentence_fails():
    o = _good(); o["description"][0]["sources"] = []
    assert any("source" in f for f in le.verify(o, FMAP, INPUT))


def test_invalid_source_id_fails():
    o = _good(); o["description"][0]["sources"] = ["f9"]
    assert any("source" in f for f in le.verify(o, FMAP, INPUT))


def test_img_only_cite_on_nonvisual_claim_fails():
    o = _good(); o["description"][0] = {"text": "Ships within two days.", "sources": ["img1"]}
    assert any("img-only" in f for f in le.verify(o, FMAP, INPUT))


def test_invented_number_fails():
    o = _good(); o["description"][0] = {"text": "Rated PEI 4 with 0.05% absorption.", "sources": ["f1"]}
    fails = le.verify(o, FMAP, INPUT)
    assert any("invented number" in f for f in fails) or any("standard" in f for f in fails)


def test_number_present_in_input_is_allowed():
    o = _good(); o["description"][0] = {"text": "Available in 600x600 size.", "sources": ["f3"]}
    assert le.verify(o, FMAP, INPUT) == []          # 600 is in the input


def test_invented_standard_fails():
    o = _good(); o["description"][0] = {"text": "Certified to ISO 13006.", "sources": ["f1"]}
    assert any("standard" in f for f in le.verify(o, FMAP, INPUT))


def test_banned_superlative_fails():
    o = _good(); o["description"][0] = {"text": "The best-in-class marble tile.", "sources": ["f1"]}
    assert any("banned" in f for f in le.verify(o, FMAP, INPUT))


def test_banned_property_allowed_only_if_in_input():
    o = _good(); o["description"][0] = {"text": "Fully waterproof surface.", "sources": ["f1"]}
    assert any("banned" in f for f in le.verify(o, FMAP, INPUT))          # not in input
    assert le.verify(o, FMAP, INPUT + "\nf4 [notes]: waterproof") == []   # now justified


def test_visual_colour_and_shape_claims_are_allowed_img_only():
    # eval found these wrongly rejected: legit visual copy citing only the image
    for text in ("The lamp has a white appearance.", "A distinct folded texture.",
                 "Its wavy design is olive in tone."):
        o = _good(); o["description"] = [{"text": text, "sources": ["img1"]}]
        assert le.verify(o, FMAP, INPUT) == [], text


def test_out_of_vocab_tag_fails():
    o = _good(); o["style_tags"] = [{"tag": "steampunk", "sources": ["img1"]}]
    assert any("out-of-vocab" in f for f in le.verify(o, FMAP, INPUT))


# ── run loop ────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "c.db", check_same_thread=False)
    db_mod.migrate(c)
    from material_bank.harvest.common import build_product
    db_mod.upsert_product(c, build_product(brand="Orientbell", sku="t1",
        title="Emperador Marble Tile", category="tiles", source="u",
        image_url="https://i/x.jpg", size_mm="600x600"), supplier_domain="orientbell.com")
    c.commit()
    yield c
    c.close()


def test_run_enriches_verifies_and_is_novelty_gated(conn):
    client = lambda prompt, img: _good()
    out = le.run(conn, client=client, budget_inr=100)
    assert out["enriched"] == 1 and out["spend_inr"] > 0
    r = conn.execute("SELECT llm_status, llm_content FROM products").fetchone()
    assert r["llm_status"] == "enriched"
    assert json.loads(r["llm_content"])["_meta"]["basis"].startswith("generated:llm:")
    # second run: unchanged input -> novelty-gated, no new work
    assert le.run(conn, client=client)["skipped_novelty"] == 0  # already 'enriched', not re-scanned
    # a re-scan (status stale) hits the novelty hash and skips
    conn.execute("UPDATE products SET llm_status='stale'"); conn.commit()
    assert le.run(conn, client=client)["skipped_novelty"] == 1


def test_run_escalates_then_marks_failed(conn):
    bad = lambda prompt, img: {"description": [{"text": "best-in-class", "sources": ["f1"]}],
                               "style_tags": [], "use_case_tags": [], "vision": {}}
    out = le.run(conn, client=bad, client_strong=bad, budget_inr=100)
    assert out["failed"] == 1 and out["enriched"] == 0
    assert conn.execute("SELECT llm_status FROM products").fetchone()[0] == "enrich_failed"


def test_budget_circuit_breaker_stops_cleanly(conn):
    # budget below one call -> breaks before calling, nothing enriched, no crash
    out = le.run(conn, client=lambda p, i: _good(), budget_inr=0.1, cost_per_call=0.4)
    assert out["enriched"] == 0 and out["scanned"] == 1
