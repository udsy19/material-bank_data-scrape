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

from pydantic import BaseModel, Field, field_validator


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
