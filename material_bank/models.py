"""Pydantic models + honesty enums for the registry/probe layer.

Deviation from PIPELINE.md (deliberate, honesty-driven):
  - ``price_published`` is an enum {yes,no,unknown}, never a bool — undetectable
    is not the same as false.
  - ``scrape_tier`` records *how to harvest* and is None until we actually
    classify. A blocked / unreachable domain has an UNKNOWN tier; that outcome
    lives on ``probe_status`` instead of masquerading as a tier value.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class ScrapeTier(str, Enum):
    """Harvest strategy for a domain, set only once positively classified."""

    SHOPIFY = "shopify"          # /products.json
    WOOCOMMERCE = "woocommerce"  # /wp-json/wc/store
    JSONLD = "jsonld"            # schema.org Product in page markup
    TIER3 = "tier3"              # needs Playwright (JS-rendered / no API)


class PricePublished(str, Enum):
    """Whether a price is publicly listed. ``unknown`` != ``no`` (honesty)."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class ProbeStatus(str, Enum):
    """Meta-outcome of a probe run, orthogonal to the harvest tier."""

    OK = "ok"                  # classified cleanly
    AMBIGUOUS = "ambiguous"    # mixed signals — flag for subagent, do NOT guess
    BLOCKED = "blocked"        # 403 / WAF — respect it, move on
    UNREACHABLE = "unreachable"  # DNS / connection failure
    ERROR = "error"            # unexpected failure during probe


class ProbeResult(BaseModel):
    """The verified facts a probe writes back into the registry.

    ``log`` is the per-decision trail ("log every decision"); it is serialized
    to JSON in the ``probe_log`` column.
    """

    domain: str
    scrape_tier: ScrapeTier | None = None
    robots_ok: bool | None = None
    robots_url: str | None = None
    sitemap_url: str | None = None
    sku_estimate: int | None = None
    price_published: PricePublished = PricePublished.UNKNOWN
    cms: str | None = None
    http_status: int | None = None
    final_host: str | None = None  # redirect capture vs the seed domain
    probe_status: ProbeStatus = ProbeStatus.OK
    probed_at: str | None = None
    log: list[dict[str, Any]] = Field(default_factory=list)

    def note(self, step: str, result: str, **detail: Any) -> None:
        """Append one decision to the trail."""
        entry: dict[str, Any] = {"step": step, "result": result}
        if detail:
            entry.update(detail)
        self.log.append(entry)


class PriceUnit(str, Enum):
    """The unit a surface price is quoted in (BOM math depends on it)."""

    PER_SQFT = "per_sqft"
    PER_BOX = "per_box"
    PER_PIECE = "per_piece"
    PER_LITRE = "per_litre"


# Categories where units are mandatory (matched as substrings against the
# pipe-delimited categories string). See CLAUDE.md "Surfaces need units".
SURFACE_MARKERS = ("tile", "paint", "laminate", "floor", "veneer")
# What every surface SKU must carry — as a value (with provenance) or an
# explicit entry in ``missing``. Never ingest a surface without these.
REQUIRED_SURFACE_FIELDS = ("price_unit", "coverage_sqft_per_box", "size_mm", "finish")


def is_surface(category: str) -> bool:
    c = (category or "").lower()
    return any(m in c for m in SURFACE_MARKERS)


class FieldProvenance(BaseModel):
    """Per-attribute honesty record: where a value came from and how sure."""

    confidence: float = 1.0
    source: str = ""                    # url / api / derived
    basis: str = "observed"             # observed | derived_proxy | estimated


class NormalizedProduct(BaseModel):
    """Stage-3 normalized spec (no price — prices are observations, Stage 7).

    Enforces the surface-units rule structurally: a tile/paint/laminate/floor/
    veneer SKU must carry price_unit + coverage_sqft_per_box + size_mm + finish,
    or list each genuinely-absent one in ``missing``.
    """

    brand: str
    sku: str
    title: str = ""
    category: str = ""
    size_mm: str | None = None
    finish: str | None = None
    price_unit: PriceUnit | None = None
    coverage_sqft_per_box: float | None = None
    image_url: str | None = None
    source_url: str | None = None   # the PDP this came from (exact resume key)
    # per-field provenance; missing = fields known-absent and honestly flagged
    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _surfaces_need_units(self) -> "NormalizedProduct":
        if not is_surface(self.category):
            return self
        for f in REQUIRED_SURFACE_FIELDS:
            present = getattr(self, f, None) is not None
            if not present and f not in self.missing:
                raise ValueError(
                    f"surface SKU {self.brand}/{self.sku}: '{f}' absent and not "
                    f"flagged in missing[] — surfaces need units (CLAUDE.md)"
                )
            if present and f not in self.provenance:
                raise ValueError(
                    f"surface SKU {self.brand}/{self.sku}: '{f}' has a value but no "
                    f"provenance — every attribute needs {{confidence, source, basis}}"
                )
        return self


class PriceBasis(str, Enum):
    """Provenance of a price — MRP is labelled MRP, never 'cost' (CLAUDE.md)."""

    LISTED_MRP = "listed_mrp"
    DEALER_QUOTE = "dealer_quote"
    ESTIMATED_BAND = "estimated_band"


class PriceObservation(BaseModel):
    """A price is an observation with a basis + timestamp, not an attribute."""

    source: str = ""
    price_inr: float
    price_unit: PriceUnit | None = None
    basis: PriceBasis
    observed_at: str
    source_url: str = ""

    @field_validator("price_inr")
    @classmethod
    def _price_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price_inr must be > 0 (a 0/placeholder price is not an observation)")
        return v


class Supplier(BaseModel):
    """A seed registry row — pre-probe. Carries no probe facts by design."""

    brand: str
    domain: str
    categories: str = ""
    domain_confidence: str = "medium"
    status: str = "active"
    notes: str = ""

    @field_validator("domain")
    @classmethod
    def _domain_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("supplier domain must be non-empty")
        return v.strip()
