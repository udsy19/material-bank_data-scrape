"""Shared harvest helpers used by every tier's harvester."""

from __future__ import annotations

import re

from ..models import FieldProvenance, NormalizedProduct, PriceUnit

_SURFACE_FIELDS = ("price_unit", "coverage_sqft_per_box", "size_mm", "finish")

# Whole-title match (anchored) so real products like "Test Tube Planter" survive
# while vendor demo SKUs ("Test33", "Example product", "Sample") are dropped.
_PLACEHOLDER_RE = re.compile(
    r"^(test\s*\d*|example\s*product|sample\s*product|untitled|demo)$", re.I)


def is_placeholder_title(title: str | None) -> bool:
    return bool(_PLACEHOLDER_RE.match((title or "").strip()))


def build_product(
    *,
    brand: str,
    sku: str,
    title: str,
    category: str,
    source: str,
    image_url: str | None = None,
    size_mm: str | None = None,
    finish: str | None = None,
    price_unit: PriceUnit | None = None,
    coverage_sqft_per_box: float | None = None,
) -> NormalizedProduct:
    """Assemble a NormalizedProduct, wiring provenance + honest missing[] flags.

    For surface categories the four required fields are either present (with
    provenance) or flagged missing; for non-surfaces only present fields get
    provenance. Centralizes the "surfaces need units" bookkeeping so every
    harvester enforces it identically.
    """
    values = {
        "price_unit": price_unit,
        "coverage_sqft_per_box": coverage_sqft_per_box,
        "size_mm": size_mm,
        "finish": finish,
    }
    provenance: dict[str, FieldProvenance] = {}
    missing: list[str] = []
    from ..models import is_surface

    surface = is_surface(category)
    for field, value in values.items():
        if value is not None:
            provenance[field] = FieldProvenance(source=source, basis="observed")
        elif surface and field in _SURFACE_FIELDS:
            missing.append(field)

    return NormalizedProduct(
        brand=brand, sku=sku, title=title, category=category, image_url=image_url,
        size_mm=size_mm, finish=finish, price_unit=price_unit,
        coverage_sqft_per_box=coverage_sqft_per_box, source_url=source,
        provenance=provenance, missing=missing,
    )
