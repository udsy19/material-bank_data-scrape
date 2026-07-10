"""Deterministic image colour: LAB/ΔE2000 sanity, palette map, bg mask, guards."""

import io

import numpy as np
from PIL import Image

from material_bank import image_colour as ic


def _png(arr: np.ndarray) -> bytes:
    b = io.BytesIO()
    Image.fromarray(arr.astype("uint8"), "RGB").save(b, "PNG")
    return b.getvalue()


def test_ciede2000_identity_is_zero():
    lab = ic._srgb_to_lab(np.array([[0.5, 0.2, 0.1]]))
    assert ic._ciede2000(lab, lab[0]) < 1e-6


def test_solid_beige_maps_to_beige_family():
    img = np.full((80, 80, 3), (217, 199, 168), dtype="uint8")   # ~beige
    r = ic.analyze(_png(img))
    assert r["colour_family"] == "Beige" and r["confidence"] > 0.5
    assert r["basis"] == "derived:pixel-clustering:v1"


def test_white_background_object_reads_the_object_not_the_bg():
    img = np.full((100, 100, 3), 247, dtype="uint8")             # white background
    img[30:70, 30:70] = (47, 111, 143)                            # ocean-blue object
    r = ic.analyze(_png(img))
    assert r["colour_family"] == "Blue"                          # bg masked out


def test_black_maps_to_black():
    img = np.full((80, 80, 3), 28, dtype="uint8")
    assert ic.analyze(_png(img))["colour_family"] == "Black"


def test_multi_object_noise_returns_unknown():
    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, (80, 80, 3), dtype="uint8")       # no dominant cluster
    r = ic.analyze(_png(img))
    assert r["colour_primary"] == "unknown" and r["confidence"] < 0.3


def test_determinism_same_image_same_result():
    img = np.full((60, 60, 3), (90, 58, 36), dtype="uint8")      # walnut-ish brown
    a, b = ic.analyze(_png(img)), ic.analyze(_png(img))
    assert a == b and a["colour_family"] == "Brown"


def test_garbage_bytes_return_unknown_not_crash():
    r = ic.analyze(b"not an image")
    assert r["colour_primary"] == "unknown" and r["confidence"] == 0.0


def _mk(conn, sku, colour_rgb, *, color_family=None):
    import json as _j
    from material_bank import db as db_mod
    from material_bank.harvest.common import build_product
    pid = db_mod.upsert_product(conn, build_product(
        brand="B", sku=sku, title=f"item {sku}", category="tiles", source=f"u/{sku}",
        image_url=f"https://img/{sku}.png"), supplier_domain="x.com")
    if color_family:
        conn.execute("UPDATE products SET color_family=? WHERE id=?", (color_family, pid))
    conn.commit()
    return pid


def test_run_writes_colour_with_provenance_and_is_resumable(tmp_path):
    import json as _j
    from material_bank import db as db_mod
    conn = db_mod.connect(tmp_path / "c.db", check_same_thread=False)
    db_mod.migrate(conn)
    pid = _mk(conn, "t1", (217, 199, 168))
    beige = _png(np.full((60, 60, 3), (217, 199, 168), dtype="uint8"))
    calls = {"n": 0}
    def fetch(url):
        calls["n"] += 1
        return beige
    out = ic.run(conn, fetch=fetch, limit=10)
    assert out["scanned"] == 1 and out["resolved"] == 1
    r = conn.execute("SELECT colour_primary, colour_confidence, provenance FROM products "
                     "WHERE id=?", (pid,)).fetchone()
    assert ic.FAMILY_OF[r["colour_primary"]] == "Beige"
    assert _j.loads(r["provenance"])["colour_primary"]["basis"].startswith("derived:pixel-clustering")
    # resumable: a second run scans nothing (already scored)
    assert ic.run(conn, fetch=fetch, limit=10)["scanned"] == 0
    conn.close()


def test_eval_colour_scores_against_text_ground_truth(tmp_path):
    from material_bank import db as db_mod
    conn = db_mod.connect(tmp_path / "c.db", check_same_thread=False)
    db_mod.migrate(conn)
    # 2 agree (Beige), 1 disagrees (pixel Beige vs text Grey)
    for sku, fam in [("a", "Beige"), ("b", "Beige"), ("c", "Grey")]:
        _mk(conn, sku, None, color_family=fam)
        conn.execute("UPDATE products SET colour_primary='Beige' WHERE sku=?", (sku,))
    conn.commit()
    e = ic.eval_colour(conn)
    assert e["n"] == 3 and e["same_family_accuracy"] == round(2 / 3, 3)
    conn.close()
