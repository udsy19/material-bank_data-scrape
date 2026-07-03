"""Step-2: BOM math, surface-units enforcement, and the v2 products migration."""

import pytest
from pydantic import ValidationError

from material_bank import db as db_mod
from material_bank.bom import DEFAULT_WASTAGE, boxes_for_area
from material_bank.models import FieldProvenance, NormalizedProduct, PriceUnit, is_surface


# --- BOM ---------------------------------------------------------------------

def test_bom_ceils_boxes_with_default_wastage():
    # 100 sqft, 15 sqft/box, +10% => ceil(110/15)=ceil(7.33)=8
    r = boxes_for_area(100, 15)
    assert r.boxes == 8
    assert r.wastage == DEFAULT_WASTAGE
    assert r.covered_sqft == 8 * 15


def test_bom_exact_boundary_not_over_ordered():
    # required exactly divisible => no extra box
    r = boxes_for_area(area_sqft=50, coverage_sqft_per_box=11, wastage=0.10)  # 55/11 = 5.0
    assert r.boxes == 5


def test_bom_custom_wastage():
    assert boxes_for_area(100, 15, wastage=0.0).boxes == 7  # ceil(100/15)


def test_bom_zero_coverage_rejected():
    with pytest.raises(ValueError):
        boxes_for_area(100, 0)


def test_bom_negative_area_rejected():
    with pytest.raises(ValueError):
        boxes_for_area(-1, 15)


# --- surface-units enforcement ----------------------------------------------

def _prov(*fields):
    return {f: FieldProvenance(source="probe", basis="observed") for f in fields}


def test_non_surface_needs_no_units():
    p = NormalizedProduct(brand="Nilkamal", sku="CHR-1", category="furniture|chairs")
    assert not is_surface(p.category)


def test_surface_with_full_units_ok():
    p = NormalizedProduct(
        brand="Orientbell", sku="OBT-84", category="tiles",
        price_unit=PriceUnit.PER_SQFT, coverage_sqft_per_box=15.0,
        size_mm="600x600", finish="matt",
        provenance=_prov("price_unit", "coverage_sqft_per_box", "size_mm", "finish"),
    )
    assert p.price_unit is PriceUnit.PER_SQFT


def test_surface_missing_units_rejected():
    with pytest.raises(ValidationError):
        NormalizedProduct(brand="X", sku="1", category="tiles",
                          price_unit=PriceUnit.PER_BOX)  # no coverage/size/finish


def test_surface_with_explicit_missing_flag_ok():
    # Honest absence is allowed; silent absence is not.
    p = NormalizedProduct(
        brand="Somany", sku="S-1", category="tiles",
        size_mm="300x300", finish="gloss", price_unit=PriceUnit.PER_BOX,
        coverage_sqft_per_box=None,
        missing=["coverage_sqft_per_box"],
        provenance=_prov("price_unit", "size_mm", "finish"),
    )
    assert "coverage_sqft_per_box" in p.missing


def test_surface_value_without_provenance_rejected():
    with pytest.raises(ValidationError):
        NormalizedProduct(
            brand="X", sku="1", category="tiles",
            price_unit=PriceUnit.PER_SQFT, coverage_sqft_per_box=15.0,
            size_mm="600x600", finish="matt",
            provenance={},  # values present but no provenance
        )


def test_is_surface_matches_markers():
    for c in ("tiles", "paint|textures", "laminates|veneers", "wood_flooring"):
        assert is_surface(c)
    assert not is_surface("lighting|fans")


# --- v2 migration ------------------------------------------------------------

def test_v2_creates_products_table(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(products)")}
    for col in ("brand", "sku", "category", "price_unit", "coverage_sqft_per_box",
                "size_mm", "finish", "provenance", "missing"):
        assert col in cols
    assert db_mod.get_schema_version(c) == db_mod.SCHEMA_VERSION
    c.close()


def test_v1_db_upgrades_to_v2_without_losing_suppliers(tmp_path):
    # Simulate a legacy v1 db: suppliers + only the v1 stamp.
    c = db_mod.connect(tmp_path / "catalog.db")
    c.execute(db_mod._SCHEMA_VERSION_DDL)
    c.execute(db_mod._SUPPLIERS_DDL)
    c.execute("INSERT INTO schema_version(version, applied_at, description) VALUES (1, ?, 'v1')",
              (db_mod.now_iso(),))
    c.execute("INSERT INTO suppliers(brand, domain) VALUES ('Orientbell', 'orientbell.com')")
    c.commit()

    db_mod.migrate(c)  # upgrade v1 -> latest
    assert db_mod.get_schema_version(c) == db_mod.SCHEMA_VERSION
    assert c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0  # table exists
    assert c.execute("SELECT COUNT(*) FROM price_observation").fetchone()[0] == 0  # v3 table exists
    assert c.execute("SELECT brand FROM suppliers WHERE domain='orientbell.com'").fetchone()[0] == "Orientbell"
    c.close()
