"""Bill-of-materials math for boxed surfaces (tiles, laminates).

Rule (CLAUDE.md / PIPELINE.md Stage 3):
    boxes = ceil( area_sqft * (1 + wastage) / coverage_sqft_per_box )
    default wastage = 10%.

Deterministic and unit-checked; the price side is separate (Stage 7).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_WASTAGE = 0.10
# Absorbs float noise (e.g. 50*1.10 = 55.0000000001) so an exact fit doesn't
# silently over-order a whole box; far smaller than any real fractional box.
_CEIL_EPS = 1e-9


@dataclass(frozen=True)
class BomResult:
    boxes: int
    covered_sqft: float       # what the ordered boxes actually cover
    wastage: float
    required_sqft: float      # area * (1 + wastage), before rounding to whole boxes


def boxes_for_area(
    area_sqft: float,
    coverage_sqft_per_box: float,
    wastage: float = DEFAULT_WASTAGE,
) -> BomResult:
    """Whole boxes needed to cover ``area_sqft`` including wastage."""
    if area_sqft < 0:
        raise ValueError("area_sqft must be >= 0")
    if coverage_sqft_per_box <= 0:
        raise ValueError("coverage_sqft_per_box must be > 0 (missing coverage cannot be BOM'd)")
    if wastage < 0:
        raise ValueError("wastage must be >= 0")

    required = area_sqft * (1.0 + wastage)
    boxes = math.ceil(required / coverage_sqft_per_box - _CEIL_EPS)
    return BomResult(
        boxes=boxes,
        covered_sqft=boxes * coverage_sqft_per_box,
        wastage=wastage,
        required_sqft=required,
    )
