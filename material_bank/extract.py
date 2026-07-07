"""Deterministic attribute extraction (Phase B) — regex + controlled vocab.

Pulls structured attributes out of the text suppliers already publish (titles,
descriptions, ld+json blobs): size (with ft/in/cm→mm conversion), thickness,
finish, color(+family). Conservative by design: only unambiguous matches are
returned — a bare "8x4" without a unit is NOT a size; "2 x 3 Set" is nothing.
Everything extracted is written with provenance basis='derived', and never
overwrites a harvested (measured) value. Honesty > recall.
"""

from __future__ import annotations

import re

_N = r"(\d+(?:\.\d+)?)"
_X = r"\s*[x×X*]\s*"

# explicit-unit pairs (unit may appear on both or only the second number)
_FT_RE = re.compile(_N + r"\s*(?:ft\.?|feet|')?" + _X + _N + r"\s*(?:ft\.?|feet|')(?![a-z])", re.I)
_IN_RE = re.compile(_N + r'\s*(?:in\.?|inch(?:es)?|")?' + _X + _N + r'\s*(?:in\.?|inch(?:es)?|")(?![a-z])', re.I)
_CM_RE = re.compile(_N + r"\s*(?:cm)?" + _X + _N + r"\s*cm(?![a-z])", re.I)
_MM3_RE = re.compile(_N + _X + _N + _X + _N + r"\s*mm(?![a-z])", re.I)   # 600x600x10mm
_MM_RE = re.compile(_N + r"\s*(?:mm)?" + _X + _N + r"\s*mm(?![a-z])", re.I)
_BARE_RE = re.compile(r"(?<![\d.])(\d{3,4})" + _X + r"(\d{3,4})(?![\d.])")  # ≥100 ⇒ mm implied
_THICK_RE = re.compile(r"(?<![\dx×X*.])" + _N + r"\s*mm\b", re.I)

_DIM_MIN, _DIM_MAX = 50, 10000          # plausible surface dimension, mm
_THICK_MIN, _THICK_MAX = 0.2, 50        # plausible thickness, mm

# canonical finish vocabulary (first match wins — order = specificity)
_FINISHES = [
    ("Lapato", r"lapp?ato"), ("Sugar", r"sugar"), ("Carving", r"carving"),
    ("Polished", r"(?:full[- ])?polish(?:ed)?|mirror[- ]polish"),
    ("High Gloss", r"(?:high|hi|super)[- ]gloss(?:y)?"),
    ("Glossy", r"gloss(?:y)?"), ("Matte", r"matt(?:e)?"),
    ("Satin", r"satin"), ("Rustic", r"rustic"), ("Metallic", r"metallic"),
    ("Textured", r"textur(?:e|ed)"), ("Suede", r"suede"),
]
_FINISH_RES = [(canon, re.compile(r"\b(?:" + pat + r")\b", re.I)) for canon, pat in _FINISHES]

# color word -> (canonical color, family)
_COLORS = {
    "white": ("White", "White"), "ivory": ("Ivory", "White"), "snow": ("Snow White", "White"),
    "black": ("Black", "Black"), "charcoal": ("Charcoal", "Black"),
    "grey": ("Grey", "Grey"), "gray": ("Grey", "Grey"), "silver": ("Silver", "Grey"),
    "beige": ("Beige", "Beige"), "cream": ("Cream", "Beige"), "sand": ("Sand", "Beige"),
    "brown": ("Brown", "Brown"), "walnut": ("Walnut", "Brown"), "teak": ("Teak", "Brown"),
    "oak": ("Oak", "Brown"), "wenge": ("Wenge", "Brown"), "tan": ("Tan", "Brown"),
    "blue": ("Blue", "Blue"), "navy": ("Navy", "Blue"), "teal": ("Teal", "Blue"),
    "green": ("Green", "Green"), "olive": ("Olive", "Green"),
    "red": ("Red", "Red"), "maroon": ("Maroon", "Red"), "terracotta": ("Terracotta", "Red"),
    "yellow": ("Yellow", "Yellow"), "mustard": ("Mustard", "Yellow"), "gold": ("Gold", "Yellow"),
    "orange": ("Orange", "Orange"), "pink": ("Pink", "Pink"), "purple": ("Purple", "Purple"),
}
_COLOR_RE = re.compile(r"\b(" + "|".join(_COLORS) + r")\b", re.I)


def _dims_ok(*mm: float) -> bool:
    return all(_DIM_MIN <= d <= _DIM_MAX for d in mm)


def extract_size_mm(text: str) -> tuple[str | None, float | None]:
    """Return (size_mm 'WxH', thickness_mm_from_3dim). Unit-explicit first;
    a bare NxM pair counts only when both numbers are >=100 (mm-scale)."""
    t = text or ""
    m = _MM3_RE.search(t)
    if m:
        a, b, th = (float(m.group(i)) for i in (1, 2, 3))
        if _dims_ok(a, b) and _THICK_MIN <= th <= _THICK_MAX:
            return f"{a:g}x{b:g}", th
    for rx, factor in ((_MM_RE, 1.0), (_CM_RE, 10.0), (_FT_RE, 304.8), (_IN_RE, 25.4)):
        m = rx.search(t)
        if m:
            a, b = float(m.group(1)) * factor, float(m.group(2)) * factor
            if _dims_ok(a, b):
                return f"{round(a):g}x{round(b):g}", None
    m = _BARE_RE.search(t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if _dims_ok(a, b):
            return f"{a:g}x{b:g}", None
    return None, None


def extract_thickness_mm(text: str) -> float | None:
    """Standalone 'N mm' (not part of an NxM size). Size spans are removed first."""
    t = text or ""
    for rx in (_MM3_RE, _MM_RE, _CM_RE):
        t = rx.sub(" ", t)
    for m in _THICK_RE.finditer(t):
        v = float(m.group(1))
        if _THICK_MIN <= v <= _THICK_MAX:
            return v
    return None


def extract_finish(text: str) -> str | None:
    for canon, rx in _FINISH_RES:
        if rx.search(text or ""):
            return canon
    return None


def extract_color(text: str) -> tuple[str | None, str | None]:
    m = _COLOR_RE.search(text or "")
    if not m:
        return None, None
    return _COLORS[m.group(1).lower()]


def extract_all(text: str) -> dict:
    """All extractable attributes from one text blob (only found keys present)."""
    out: dict = {}
    size, th3 = extract_size_mm(text)
    if size:
        out["size_mm"] = size
    thickness = th3 if th3 is not None else extract_thickness_mm(text)
    if thickness is not None:
        out["thickness_mm"] = thickness
    finish = extract_finish(text)
    if finish:
        out["finish"] = finish
    color, family = extract_color(text)
    if color:
        out["color"], out["color_family"] = color, family
    return out


def derive_sheet_coverage(size_mm: str | None, category: str) -> float | None:
    """Laminate/ply sheets are sold per sheet: coverage = the sheet's own area.
    Only derived for sheet-goods categories with a plausible sheet size."""
    if not size_mm or not any(k in (category or "").lower()
                              for k in ("laminate", "plywood", "mdf", "veneer", "acrylic")):
        return None
    try:
        a, b = (float(x) for x in size_mm.split("x"))
    except ValueError:
        return None
    sqft = (a / 304.8) * (b / 304.8)
    return round(sqft, 2) if 4 <= sqft <= 64 else None   # sanity: 2x2ft .. 8x8ft
