"""Deterministic image-derived colour (Stage A1).

Pulls the dominant colour(s) out of a product image by pixel clustering — a
*measured* attribute, `basis: derived:pixel-clustering:v1`. Pure numpy + PIL
(no sklearn/skimage/torch): sRGB→CIELAB, k-means (fixed seed → deterministic),
CIEDE2000 nearest-neighbour against a versioned domain palette. Honesty first:
a low-confidence or multi-object result returns ``unknown`` with its measured
confidence, and is the signal to hand the product to the LLM vision path — never
a silent guess.

Pipeline: border-strip background mask → specular (top-luminance) drop → LAB
k-means (k=4, min share 8%) → guard (top cluster must reach 30%) → palette map
(ΔE2000 < 18) → wood-grain two-tone handling.
"""

from __future__ import annotations

import io
import json
import sqlite3

import numpy as np
from PIL import Image

PALETTE_VERSION = "v1"
BASIS = f"derived:pixel-clustering:{PALETTE_VERSION}"

# (name, family, hex) — a domain palette tuned for architectural finishes.
_PALETTE_HEX = [
    ("White", "White", "#f7f7f4"), ("Off-White", "White", "#efe9df"),
    ("Ivory", "White", "#f2e9d8"), ("Marble White", "White", "#eceae3"),
    ("Beige", "Beige", "#d9c7a8"), ("Sand", "Beige", "#c8b28c"),
    ("Cream", "Beige", "#e6d8bf"), ("Travertine", "Beige", "#cdb996"),
    ("Light Grey", "Grey", "#c4c4c1"), ("Ash Grey", "Grey", "#a7a9a5"),
    ("Grey", "Grey", "#8a8c8a"), ("Graphite", "Grey", "#54585a"),
    ("Charcoal", "Grey", "#3a3d3f"), ("Silver", "Grey", "#b8bcbe"),
    ("Black", "Black", "#1c1c1c"), ("Matte Black", "Black", "#232323"),
    ("Brown", "Brown", "#6b4a2f"), ("Walnut", "Brown", "#5a3a24"),
    ("Teak", "Brown", "#8a5a34"), ("Wenge", "Brown", "#3e2a20"),
    ("Oak", "Brown", "#b08a5e"), ("Terracotta", "Red", "#a5502f"),
    ("Brick", "Red", "#8f3b2c"), ("Burgundy", "Red", "#5c1f27"),
    ("Red", "Red", "#b0342b"), ("Blush", "Pink", "#e6b7b0"),
    ("Ocean Blue", "Blue", "#2f6f8f"), ("Navy", "Blue", "#26374d"),
    ("Teal", "Blue", "#2f7d7a"), ("Sage", "Green", "#9aa987"),
    ("Forest", "Green", "#2f4b34"), ("Olive", "Green", "#6b6b3a"),
    ("Brass", "Gold", "#b08d3e"), ("Gold", "Gold", "#c9a437"),
    ("Copper", "Gold", "#a5673f"), ("Yellow", "Yellow", "#d8b13a"),
]


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """(N,3) sRGB in [0,1] -> (N,3) CIELAB (D65)."""
    m = rgb > 0.04045
    lin = np.where(m, ((rgb + 0.055) / 1.055) ** 2.4, rgb / 12.92)
    mat = np.array([[0.4124, 0.3576, 0.1805],
                    [0.2126, 0.7152, 0.0722],
                    [0.0193, 0.1192, 0.9505]])
    xyz = lin @ mat.T
    white = np.array([0.95047, 1.0, 1.08883])
    xyz = xyz / white
    e, k = 0.008856, 903.3
    fx = np.where(xyz > e, np.cbrt(xyz), (k * xyz + 16) / 116)
    L = 116 * fx[:, 1] - 16
    a = 500 * (fx[:, 0] - fx[:, 1])
    b = 200 * (fx[:, 1] - fx[:, 2])
    return np.stack([L, a, b], axis=1)


def _ciede2000(lab: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """ΔE2000 between each row of (N,3) ``lab`` and a single (3,) ``ref``."""
    L1, a1, b1 = lab[:, 0], lab[:, 1], lab[:, 2]
    L2, a2, b2 = ref
    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cbar ** 7 / (Cbar ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    dhp = h2p - h1p
    dhp = np.where(dhp > 180, dhp - 360, np.where(dhp < -180, dhp + 360, dhp))
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp) / 2)
    Lbp = (L1 + L2) / 2
    Cbp = (C1p + C2p) / 2
    hsum = h1p + h2p
    hbp = np.where(np.abs(h1p - h2p) > 180, (hsum + 360) / 2, hsum / 2)
    T = (1 - 0.17 * np.cos(np.radians(hbp - 30)) + 0.24 * np.cos(np.radians(2 * hbp))
         + 0.32 * np.cos(np.radians(3 * hbp + 6)) - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    dtheta = 30 * np.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25.0 ** 7))
    Sl = 1 + (0.015 * (Lbp - 50) ** 2) / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
                   + Rt * (dCp / Sc) * (dHp / Sh))


_PALETTE_LAB = _srgb_to_lab(
    np.array([[int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)] for _, _, h in _PALETTE_HEX]))


def _nearest(lab: np.ndarray) -> tuple[str, str, float]:
    """(name, family, ΔE to nearest, ΔE margin to 2nd) for one LAB colour."""
    d = np.array([_ciede2000(lab[None, :], _PALETTE_LAB[i])[0] for i in range(len(_PALETTE_HEX))])
    order = np.argsort(d)
    name, family, _ = _PALETTE_HEX[order[0]]
    margin = float(d[order[1]] - d[order[0]]) if len(order) > 1 else 99.0
    return name, family, float(d[order[0]]), margin


def _kmeans(pixels: np.ndarray, k: int = 4, iters: int = 25, seed: int = 42):
    """Deterministic k-means in LAB. Returns (centroids, share_per_centroid)."""
    rng = np.random.default_rng(seed)
    n = len(pixels)
    k = min(k, n)
    cent = pixels[rng.choice(n, k, replace=False)]
    for _ in range(iters):
        d = np.linalg.norm(pixels[:, None, :] - cent[None, :, :], axis=2)
        lab = d.argmin(axis=1)
        newc = np.array([pixels[lab == j].mean(axis=0) if np.any(lab == j) else cent[j]
                         for j in range(k)])
        if np.allclose(newc, cent):
            cent = newc
            break
        cent = newc
    shares = np.array([np.mean(lab == j) for j in range(k)])
    return cent, shares


def analyze(image_bytes: bytes, *, max_dim: int = 128) -> dict:
    """Dominant colour(s) of a product image. Returns colour_primary /
    colour_secondary (palette name or 'unknown'), confidence 0-1, and basis.
    'unknown' means: hand to the LLM vision path, don't guess."""
    out = {"colour_primary": "unknown", "colour_secondary": None,
           "colour_family": None, "confidence": 0.0, "basis": BASIS}
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return out
    im.thumbnail((max_dim, max_dim))
    arr = np.asarray(im, dtype=np.float64) / 255.0
    if arr.size < 300:
        return out
    h, w, _ = arr.shape

    # 1. border-strip background mask (cheap tier)
    b = max(1, int(0.05 * min(h, w)))
    border = np.concatenate([arr[:b].reshape(-1, 3), arr[-b:].reshape(-1, 3),
                             arr[:, :b].reshape(-1, 3), arr[:, -b:].reshape(-1, 3)])
    flat = arr.reshape(-1, 3)
    lab = _srgb_to_lab(flat)
    keep = np.ones(len(flat), dtype=bool)
    if border.std(axis=0).mean() < 0.06:                 # near-uniform border => background
        bg_lab = _srgb_to_lab(border.mean(axis=0)[None, :])[0]
        keep = _ciede2000(lab, bg_lab) > 10
    if keep.mean() < 0.10:                                # masked ~everything => full-bleed
        keep = np.ones(len(flat), dtype=bool)             # swatch, the image IS the colour

    # 2. specular / highlight drop (top 2% luminance)
    L = lab[:, 0]
    thr = np.percentile(L[keep], 98)
    keep &= L <= thr                                     # drop strict top-2% (gloss); keep uniform
    pix = lab[keep]
    if len(pix) < 50:
        return out

    # 3. cluster
    cent, shares = _kmeans(pix)
    order = np.argsort(-shares)
    top, top_share = cent[order[0]], float(shares[order[0]])

    # 4. multi-object / unreliable guard
    if top_share < 0.30:
        out["confidence"] = round(top_share, 3)
        return out                                       # -> LLM vision path

    name, family, de, margin = _nearest(top)
    if de >= 18:                                         # nothing in palette is close enough
        out["confidence"] = round(top_share * 0.5, 3)
        return out
    conf = top_share * (1 - de / 18) * min(1.0, 0.5 + margin / 10)
    out.update(colour_primary=name, colour_family=family, confidence=round(float(conf), 3))

    # 5. wood-grain / two-tone: a large second cluster in a different family
    if len(order) > 1 and float(shares[order[1]]) > 0.25:
        n2, f2, de2, _ = _nearest(cent[order[1]])
        if de2 < 18 and n2 != name:
            out["colour_secondary"] = n2
    return out


FAMILY_OF = {name: family for name, family, _ in _PALETTE_HEX}


def _default_fetch(url: str) -> bytes | None:
    try:
        from curl_cffi import requests
        r = requests.get(url, impersonate="chrome131", timeout=15)
        if r.status_code and 200 <= r.status_code < 300 and r.content:
            return r.content
    except Exception:
        pass
    return None


def run(conn: sqlite3.Connection, *, fetch=_default_fetch, limit: int = 1000) -> dict:
    """Score image colour for un-scored products with an image. Resumable
    (colour_scored_at marks done). Writes colour_primary/secondary/confidence +
    provenance; 'unknown' is stored honestly (it's the LLM-vision work-list)."""
    from .db import now_iso
    rows = conn.execute(
        "SELECT id, image_url, provenance FROM products "
        "WHERE image_url IS NOT NULL AND colour_scored_at IS NULL LIMIT ?", (limit,)).fetchall()
    stats = {"scanned": len(rows), "resolved": 0, "unknown": 0, "fetch_failed": 0}
    for row in rows:
        img = fetch(row["image_url"])
        ts = now_iso()
        if not img:
            stats["fetch_failed"] += 1
            conn.execute("UPDATE products SET colour_scored_at=? WHERE id=?", (ts, row["id"]))
            continue
        r = analyze(img)
        prov = json.loads(row["provenance"] or "{}")
        if r["colour_primary"] != "unknown":
            prov["colour_primary"] = {"source": row["image_url"], "basis": r["basis"],
                                      "confidence": r["confidence"]}
            stats["resolved"] += 1
        else:
            stats["unknown"] += 1
        conn.execute(
            "UPDATE products SET colour_primary=?, colour_secondary=?, colour_confidence=?, "
            "provenance=?, colour_scored_at=? WHERE id=?",
            (r["colour_primary"], r["colour_secondary"], r["confidence"],
             json.dumps(prov), ts, row["id"]))
    conn.commit()
    return stats


def eval_colour(conn: sqlite3.Connection) -> dict:
    """Validation harness (mandatory before publish-gating the field): compare
    pixel colour family against the independently text-derived color_family on
    records carrying both. Returns overall + per-category same-family accuracy —
    the permanent regression metric."""
    rows = conn.execute(
        "SELECT category_std, colour_primary, color_family FROM products "
        "WHERE colour_primary IS NOT NULL AND colour_primary!='unknown' "
        "AND color_family IS NOT NULL").fetchall()
    per: dict[str, list[int]] = {}
    hit = 0
    for r in rows:
        ok = int(FAMILY_OF.get(r["colour_primary"]) == r["color_family"])
        hit += ok
        per.setdefault(r["category_std"] or "?", []).append(ok)
    return {
        "n": len(rows),
        "same_family_accuracy": round(hit / len(rows), 3) if rows else 0.0,
        "per_category": {k: {"n": len(v), "acc": round(sum(v) / len(v), 3)}
                         for k, v in sorted(per.items(), key=lambda kv: -len(kv[1]))[:12]},
    }


def main(argv=None) -> int:
    import argparse
    import sys

    from . import db

    ap = argparse.ArgumentParser(prog="mb-image-colour")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    if args.eval:
        print(json.dumps(eval_colour(conn)), file=sys.stderr)
    else:
        print(json.dumps(run(conn, limit=args.limit)), file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
