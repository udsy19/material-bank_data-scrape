"""Canonical taxonomy + deterministic classifier (Phase B).

Folds the messy freeform `category` strings suppliers ship (e.g.
"office furniture|seating|workstations|storage|soft seating") into a clean
architect-facing tree: family → category. Each node carries an OmniClass
Table 23 code where one is VERIFIED (NBIMS-US / OmniClass 23 Products) and
``None`` where it isn't — we never fabricate a standards code.

Classification is deterministic and ordered (first rule that matches the
freeform category, then the title, wins). Unmatched products land in "Other"
and are the work-list for a later LLM classify slot (Gemini) — never guessed.
"""

from __future__ import annotations

import re
import sqlite3

from .db import now_iso

# Ordered rules — SPECIFIC BEFORE GENERAL (e.g. "office plants" must beat
# "office furniture"; "mattresses"/"sofas" must beat bare "furniture").
# (match_terms, family, category, omniclass|None)
RULES: list[tuple[tuple[str, ...], str, str, str | None]] = [
    (("plant", "planter", "gardening", "pots"),        "Decor & Greenery", "Plants & Planters", None),
    (("monitor arm", "monitor mount", "vesa", "sit-stand", "riser", "monitor arms"),
                                                        "Furniture", "Ergonomic Accessories", "23-40 20 00"),
    (("sanitaryware", "bath_fitting", "bathware", "faucet", "washbasin", "water closet", "wc"),
                                                        "Bath & Sanitary", "Sanitaryware & Fittings", "23-45 05 14"),
    (("plumbing", "pipe"),                              "Bath & Sanitary", "Plumbing", "23-45 00 00"),
    (("wallpaper", "mural", "wall_decor", "wall covering", "wallcovering"),
                                                        "Surfaces", "Wallpaper & Wall Coverings", "23-35 10 00"),
    (("laminate",),                                     "Surfaces", "Laminates", "23-35 10 00"),
    (("cladding", "louver", "veneer", "hpl", "panel", "acrylic"),
                                                        "Surfaces", "Panels & Cladding", "23-35 10 00"),
    (("tile",),                                         "Surfaces", "Tiles", "23-35 50 14"),
    (("quartz", "marble", "granite", "engineered stone", "countertop", "solid surface"),
                                                        "Surfaces", "Stone & Engineered Surfaces", None),
    (("paint", "coating", "primer", "texture"),         "Paint & Coatings", "Paint", "23-35 00 00"),
    (("rug", "carpet"),                                 "Flooring", "Rugs & Carpets", "23-35 50 00"),
    (("wood_flooring", "wood flooring", "vinyl", "lvt", "flooring", "decking", "laminate_flooring"),
                                                        "Flooring", "Hard Flooring", "23-35 50 00"),
    (("mattress",),                                     "Furniture", "Mattresses", "23-40 20 00"),
    (("sofa", "recliner", "lounge", "couch"),           "Furniture", "Sofas & Lounge", "23-40 20 14"),
    (("phone booth", "booth", "acoustic pod", "acoustic"),
                                                        "Partitions & Acoustic", "Booths & Pods", None),
    (("seating", "chair", "stool", "workspace"),        "Furniture", "Seating", "23-40 20 14"),
    (("workstation", "desk", "table", "worktable"),     "Furniture", "Desks & Tables", "23-40 20 00"),
    (("storage", "wardrobe", "cabinet", "shelv", "casework"),
                                                        "Furniture", "Storage", "23-40 20 24"),
    (("partition", "door"),                             "Partitions & Acoustic", "Partitions & Doors", None),
    (("fan",),                                          "Lighting & Electrical", "Fans", None),
    (("lighting", "lamp", "luminaire", "light", "chandelier", "pendant"),
                                                        "Lighting & Electrical", "Lighting", None),
    (("soft_furnishing", "bed_bath", "curtain", "fabric", "drapery", "cushion", "upholstery"),
                                                        "Soft Furnishings", "Soft Furnishings", "23-40 00 00"),
    (("furniture",),                                    "Furniture", "General Furniture", "23-40 20 00"),
    (("decor",),                                        "Decor & Greenery", "Decor & Accessories", None),
]


def _slug(*parts: str) -> str:
    return ".".join(re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in parts)


def classify(category: str, title: str = "") -> dict:
    """Return the canonical node for a product. Freeform category leads; title
    is the fallback. Unmatched -> the honest 'Other' bucket (not a guess)."""
    haystacks = [(category or "").lower(), (title or "").lower()]
    for hay in haystacks:
        if not hay:
            continue
        for terms, family, cat, omni in RULES:
            if any(t in hay for t in terms):
                return {"family": family, "category_std": cat, "omniclass": omni,
                        "node": _slug(family, cat), "matched": True}
    return {"family": "Other", "category_std": "Unclassified", "omniclass": None,
            "node": "other.unclassified", "matched": False}


def classify_all(conn: sqlite3.Connection, *, only_unclassified: bool = False,
                 batch: int = 20000) -> dict:
    """Assign the canonical taxonomy to products. Idempotent; read-then-write
    (WAL BUSY_SNAPSHOT rule). ``only_unclassified`` re-runs just the Other bucket."""
    where = "WHERE family IS NULL OR family='Other'" if only_unclassified else ""
    rows = conn.execute(
        f"SELECT id, category, title FROM products {where}").fetchall()
    ts = now_iso()
    updates, summary = [], {"classified": 0, "matched": 0}
    for row in rows:
        c = classify(row["category"] or "", row["title"] or "")
        updates.append((c["family"], c["category_std"], c["omniclass"], ts, row["id"]))
        summary["classified"] += 1
        summary["matched"] += int(c["matched"])
    for i in range(0, len(updates), batch):
        conn.executemany(
            "UPDATE products SET family=?, category_std=?, omniclass=?, classified_at=? "
            "WHERE id=?", updates[i:i + batch])
        conn.commit()
    return summary


def taxonomy_tree(conn: sqlite3.Connection) -> list[dict]:
    """The tree with live counts — powers faceted browse + coverage reporting."""
    rows = conn.execute(
        """SELECT family, category_std,
                  COUNT(*) products,
                  SUM(publish_ready) publish_ready,
                  MAX(omniclass) omniclass
           FROM products WHERE family IS NOT NULL
           GROUP BY family, category_std""").fetchall()
    families: dict[str, dict] = {}
    for r in rows:
        fam = families.setdefault(r["family"], {"family": r["family"], "products": 0,
                                                "publish_ready": 0, "categories": []})
        fam["products"] += r["products"]
        fam["publish_ready"] += (r["publish_ready"] or 0)
        fam["categories"].append({
            "category": r["category_std"], "omniclass": r["omniclass"],
            "products": r["products"], "publish_ready": r["publish_ready"] or 0,
            "node": _slug(r["family"], r["category_std"])})
    out = sorted(families.values(), key=lambda f: -f["products"])
    for f in out:
        f["categories"].sort(key=lambda c: -c["products"])
    return out
