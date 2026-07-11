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
            {"text": "A polished vitrified tile with soft marble veining.", "sources": ["f1", "f3"]},
            {"text": "Warm brown tones lend a understated, classic surface.", "sources": ["img1"]},
        ],
        "style_tags": [{"tag": "modern", "sources": ["img1"]}],
        "use_case_tags": [{"tag": "flooring", "sources": ["img1"]}],  # grounded in the image
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


def test_out_of_vocab_tag_is_dropped_not_failed():
    o = _good(); o["style_tags"] = [{"tag": "steampunk", "sources": ["img1"]}]
    s = le.sanitize(o, FMAP, INPUT)
    assert s["style_tags"] == [] and le.verify(s, FMAP, INPUT) == []   # dropped, no retry


# ── v3: usefulness checks (restatement, plumbing, tag discipline) ─────────────

def test_restatement_filler_rejected():
    # "It is from the brand Emperador Marble Tile" adds nothing over f1
    o = _good()
    o["description"] = [{"text": "This is the Emperador Marble Tile.", "sources": ["f1"]}]
    assert any("restatement" in f for f in le.verify(o, FMAP, INPUT))


def test_plumbing_in_prose_rejected():
    for bad in ("Its supplier domain is orientbell.com.", "This falls under category standard Tiles.",
                "The product is identified as f1."):
        o = _good(); o["description"] = [{"text": bad, "sources": ["f1"]}]
        fails = le.verify(o, {**FMAP, "f4": ("supplier_domain", "orientbell.com")}, INPUT)
        assert any("plumbing" in f or "citation id" in f for f in fails), bad


def test_use_case_tags_are_derived_from_evidence_not_the_llm():
    # a functional keyword in the record -> derived tag; the LLM's guess is ignored
    inp = "f1 [title]: Anti Static Conductive Tile\nf2 [category_std]: Tiles\nf3 [finish]: Matte"
    o = _good()
    o["use_case_tags"] = [{"tag": "residential", "sources": ["img1"]}]    # LLM guess -> ignored
    s = le.sanitize(o, FMAP, inp)
    uc = [t["tag"] for t in s["use_case_tags"]]
    assert ("commercial" in uc or "high-traffic" in uc)                  # from 'anti static conductive'
    assert "residential" not in uc                                       # not derived (no evidence)
    assert all(t["sources"] == ["derived"] for t in s["use_case_tags"])


def test_style_tags_kept_only_with_evidence_or_image():
    o = _good()
    o["style_tags"] = [{"tag": "modern", "sources": ["img1"]},           # image-supported -> kept
                       {"tag": "rustic", "sources": ["f1"]}]             # no evidence, no img -> dropped
    s = le.sanitize(o, FMAP, INPUT)
    tags = [t["tag"] for t in s["style_tags"]]
    assert "modern" in tags and "rustic" not in tags
    # textual evidence keeps a style tag even without the image
    s2 = le.sanitize({**_good(), "style_tags": [{"tag": "rustic", "sources": ["f1"]}]},
                     FMAP, INPUT + "\nf4 [title2]: distressed rustic finish")
    assert [t["tag"] for t in s2["style_tags"]] == ["rustic"]


def test_sanitize_nulls_out_of_vocab_vision():
    o = _good(); o["vision"] = {"material_look": {"value": "Marble Look", "confidence": 0.9}}
    s = le.sanitize(o, FMAP, INPUT)
    assert s["vision"]["material_look"]["value"] == "unknown"
    assert le.verify(s, FMAP, INPUT) == []


def test_description_fabrication_still_hard_fails():
    o = _good(); o["description"] = [{"text": "Certified to ISO 13006.", "sources": ["f1"]}]
    assert le.verify(le.sanitize(o, FMAP, INPUT), FMAP, INPUT)            # still triggers retry


def test_novelty_hash_is_version_prefixed():
    h = le.novelty_hash("some input", "http://img")
    assert h.startswith(le.PROMPT_VERSION + ":")


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
    client = lambda prompt, img: {"output": _good(), "usage": {"input_tokens": 500, "output_tokens": 200}}
    out = le.run(conn, client=client, budget_inr=100)
    assert out["enriched"] == 1 and out["spend_inr"] > 0
    # every call is on the ledger with a real cost
    call = conn.execute("SELECT status, input_tokens, cost_inr FROM llm_calls").fetchone()
    assert call["status"] == "enriched" and call["input_tokens"] == 500 and call["cost_inr"] > 0
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


def test_drain_concurrent_enriches_all_and_is_resumable(tmp_path):
    from material_bank.harvest.common import build_product
    dbp = tmp_path / "c.db"
    conn = db_mod.connect(dbp, check_same_thread=False)
    db_mod.migrate(conn)
    for i in range(6):
        db_mod.upsert_product(conn, build_product(brand="B", sku=f"s{i}",
            title="Emperador Marble Tile", category="tiles", source=f"u{i}",
            image_url="https://i/x.jpg", size_mm="600x600"), supplier_domain="orientbell.com")
    conn.commit(); conn.close()

    factory = lambda: (lambda p, i: {"output": _good(), "usage": {"input_tokens": 100, "output_tokens": 50}})
    out = le.drain_concurrent(dbp, workers=3, client_factory=factory)
    assert out["enriched"] == 6 and out["remaining"] == 0 and out["spend_inr"] > 0
    # resumable: a second drain finds nothing left
    assert le.drain_concurrent(dbp, workers=3, client_factory=factory)["enriched"] == 0
    c = db_mod.connect(dbp)
    assert c.execute("SELECT COUNT(*) FROM llm_calls WHERE status='enriched'").fetchone()[0] == 6
    c.close()


def test_budget_circuit_breaker_stops_on_real_spend(conn):
    # add a 2nd product so the breaker can fire between them
    from material_bank.harvest.common import build_product
    db_mod.upsert_product(conn, build_product(brand="B", sku="t2", title="Second Marble Tile",
        category="tiles", source="u2", image_url="https://i/y.jpg", size_mm="300x300"),
        supplier_domain="orientbell.com")
    conn.commit()
    costly = lambda p, i: {"output": _good(), "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}}
    out = le.run(conn, client=costly, budget_inr=1.0)      # one call ~₹200 >> ₹1 budget
    assert out["enriched"] == 1                             # first processed, then breaker fires
    assert conn.execute("SELECT COUNT(*) FROM products WHERE llm_status IS NULL").fetchone()[0] == 1
    assert out["spend_inr"] > 1.0


def test_extract_json_recovers_fences_and_extra_data():
    assert le.extract_json('{"a": 1}') == {"a": 1}
    assert le.extract_json('```json\n{"a": 1}\n```') == {"a": 1}          # markdown fence
    assert le.extract_json('{"a": 1}\n\nHope this helps!') == {"a": 1}    # trailing prose
    assert le.extract_json('Here you go: {"a": {"b": 2}} done') == {"a": {"b": 2}}  # balanced
    import pytest as _p
    with _p.raises(ValueError):
        le.extract_json("no json here")
