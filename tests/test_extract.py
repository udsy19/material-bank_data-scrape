"""Extractor patterns — conservative: no unit, no match (honesty > recall)."""

from material_bank.extract import (
    derive_sheet_coverage,
    extract_all,
    extract_color,
    extract_finish,
    extract_size_mm,
    extract_thickness_mm,
)


def test_size_bare_mm_pair():
    assert extract_size_mm("Emperador Marble 600x600 Vitrified")[0] == "600x600"
    assert extract_size_mm("300X450 wall tile")[0] == "300x450"
    assert extract_size_mm("1200 x 2400 slab")[0] == "1200x2400"


def test_size_explicit_mm():
    assert extract_size_mm("Size: 600 x 1200 mm")[0] == "600x1200"
    assert extract_size_mm("600mm x 600mm")[0] == "600x600"


def test_size_cm_ft_inch_converted():
    assert extract_size_mm("60x120 cm porcelain")[0] == "600x1200"
    assert extract_size_mm("8 ft x 4 ft laminate")[0] == "2438x1219"
    assert extract_size_mm("8ft. x 4ft. sheet")[0] == "2438x1219"
    assert extract_size_mm('48" x 96" board')[0] == "1219x2438"


def test_size_unit_on_second_number_only():
    assert extract_size_mm("8 x 4 ft decorative laminate")[0] == "2438x1219"


def test_size_ambiguity_refused():
    assert extract_size_mm("8x4 decorative laminate")[0] is None      # no unit, small ints
    assert extract_size_mm("Set of 2 x 3 cushions")[0] is None
    assert extract_size_mm("2x3 rug")[0] is None


def test_three_dim_gives_thickness():
    size, th = extract_size_mm("600x600x10mm heavy duty")
    assert size == "600x600" and th == 10.0


def test_thickness_standalone_not_from_size():
    assert extract_thickness_mm("Decorative Laminate - 1 mm") == 1.0
    assert extract_thickness_mm("0.8mm liner grade") == 0.8
    assert extract_thickness_mm("600x600 mm tile") is None            # size, not thickness
    assert extract_thickness_mm("weighs 500 mm") is None              # implausible -> refused


def test_finish_vocab():
    assert extract_finish("Glossy finish tile") == "Glossy"
    assert extract_finish("Matt Finish") == "Matte"
    assert extract_finish("High Gloss laminate") == "High Gloss"
    assert extract_finish("Lappato surface") == "Lapato"
    assert extract_finish("Full Polished vitrified") == "Polished"
    assert extract_finish("format tile") is None                      # no substring traps


def test_color_and_family():
    assert extract_color("Charcoal Grey Fluted") == ("Charcoal", "Black")
    assert extract_color("Walnut wood laminate") == ("Walnut", "Brown")
    assert extract_color("plain product") == (None, None)


def test_materialdepot_style_title_end_to_end():
    t = ("Charcoal Grey Fluted Solid Color Look LM 02843 Trout 8 ft x 4 ft "
         "Bamboo Channel Finish Decorative Laminate - 1 mm")
    out = extract_all(t)
    assert out["size_mm"] == "2438x1219"
    assert out["thickness_mm"] == 1.0
    assert out["color"] == "Charcoal" and out["color_family"] == "Black"


def test_sheet_coverage_derivation():
    import pytest
    # 2438x1219mm is 7.999x3.999 ft -> 31.99 sqft (honest, not rounded to nominal)
    assert derive_sheet_coverage("2438x1219", "laminates|acrylic") == pytest.approx(32.0, abs=0.05)
    assert derive_sheet_coverage("600x600", "tiles") is None          # not sheet goods
    assert derive_sheet_coverage("2438x1219", "tiles") is None
    assert derive_sheet_coverage(None, "laminates") is None
