"""Canonical taxonomy: ordered classification, honest Other bucket, tree."""

import pytest

from material_bank import db as db_mod
from material_bank import taxonomy
from material_bank.harvest.common import build_product


@pytest.mark.parametrize("category,title,family,cat", [
    ("tiles", "", "Surfaces", "Tiles"),
    ("laminates|acrylic|cladding", "", "Surfaces", "Laminates"),          # laminate beats cladding
    ("laminates|wood_flooring", "", "Surfaces", "Laminates"),             # laminate beats flooring
    ("wallpaper|self-adhesive|murals", "", "Surfaces", "Wallpaper & Wall Coverings"),
    ("rugs|carpets", "", "Flooring", "Rugs & Carpets"),
    ("furniture|mattresses", "", "Furniture", "Mattresses"),              # mattress beats furniture
    ("sofas|phone booths|recliners", "", "Furniture", "Sofas & Lounge"),  # sofa beats booth/seating
    ("office furniture|seating|workstations|storage", "", "Furniture", "Seating"),
    ("furniture|storage", "", "Furniture", "Storage"),
    ("office plants|planters", "", "Decor & Greenery", "Plants & Planters"),  # plant beats furniture-ish
    ("monitor arms|risers|accessories", "", "Furniture", "Ergonomic Accessories"),
    ("sanitaryware|bath_fittings", "", "Bath & Sanitary", "Sanitaryware & Fittings"),
    ("plumbing|pipes", "", "Bath & Sanitary", "Plumbing"),
    ("commercial lighting", "", "Lighting & Electrical", "Lighting"),
    ("lighting|fans", "", "Lighting & Electrical", "Fans"),               # fan beats lighting (order)
    ("quartz", "", "Surfaces", "Stone & Engineered Surfaces"),
    ("paint", "", "Paint & Coatings", "Paint"),
    ("furniture|decor", "", "Furniture", "General Furniture"),
    ("soft_furnishing|bed_bath", "", "Soft Furnishings", "Soft Furnishings"),
])
def test_classify_real_categories(category, title, family, cat):
    c = taxonomy.classify(category, title)
    assert (c["family"], c["category_std"]) == (family, cat)
    assert c["matched"] is True


def test_title_fallback_when_category_blank():
    c = taxonomy.classify("", "Steelcase Gesture Ergonomic Office Chair")
    assert c["family"] == "Furniture" and c["category_std"] == "Seating"


def test_unmatched_goes_to_honest_other():
    c = taxonomy.classify("miscellaneous gizmo", "widget")
    assert c["family"] == "Other" and c["matched"] is False
    assert c["omniclass"] is None


def test_verified_omniclass_only():
    assert taxonomy.classify("tiles")["omniclass"] == "23-35 50 14"
    assert taxonomy.classify("sanitaryware")["omniclass"] == "23-45 05 14"
    assert taxonomy.classify("commercial lighting")["omniclass"] is None   # not fabricated


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    yield c
    c.close()


def test_classify_all_and_tree(conn):
    for i, (cat, sup) in enumerate([("tiles", "orientbell.com"), ("tiles", "kajaria.com"),
                                    ("laminates|acrylic", "advancelam.com"),
                                    ("rugs|carpets", "obeetee.in"),
                                    ("weird thing", "x.com")]):
        db_mod.upsert_product(conn, build_product(
            brand="B", sku=f"s{i}", title=f"item {i}", category=cat, source="u"),
            supplier_domain=sup)
    conn.execute("UPDATE products SET publish_ready=1 WHERE category='tiles'")
    conn.commit()

    summary = taxonomy.classify_all(conn)
    assert summary["classified"] == 5 and summary["matched"] == 4  # 'weird thing' -> Other

    tree = taxonomy.taxonomy_tree(conn)
    fams = {f["family"]: f for f in tree}
    assert fams["Surfaces"]["products"] == 3          # 2 tiles + 1 laminate
    surf_cats = {c["category"]: c for c in fams["Surfaces"]["categories"]}
    assert surf_cats["Tiles"]["products"] == 2 and surf_cats["Tiles"]["publish_ready"] == 2
    assert surf_cats["Tiles"]["omniclass"] == "23-35 50 14"
    assert "Other" in fams and fams["Other"]["products"] == 1
