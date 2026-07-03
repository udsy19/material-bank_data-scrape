import json

import pytest
from pydantic import ValidationError

from material_bank.models import (
    PricePublished,
    ProbeResult,
    ProbeStatus,
    ScrapeTier,
    Supplier,
)


def test_price_published_defaults_unknown_not_false():
    r = ProbeResult(domain="x.com")
    assert r.price_published is PricePublished.UNKNOWN  # undetectable != no


def test_scrape_tier_enum_rejects_junk():
    with pytest.raises(ValidationError):
        ProbeResult(domain="x.com", scrape_tier="magento")


def test_price_published_enum_rejects_junk():
    with pytest.raises(ValidationError):
        ProbeResult(domain="x.com", price_published="maybe")


def test_blocked_is_a_status_not_a_tier():
    # A blocked domain has an unknown tier, recorded via probe_status.
    r = ProbeResult(domain="x.com", probe_status=ProbeStatus.BLOCKED)
    assert r.scrape_tier is None
    assert r.probe_status is ProbeStatus.BLOCKED


def test_note_appends_decision_trail_and_json_round_trips():
    r = ProbeResult(domain="x.com")
    r.note("robots", "ok", url="https://x.com/robots.txt")
    r.note("shopify", "hit", price="1999")
    assert [e["step"] for e in r.log] == ["robots", "shopify"]
    dumped = json.dumps(r.log)
    assert json.loads(dumped)[1]["result"] == "hit"


def test_supplier_domain_must_be_nonempty():
    with pytest.raises(ValidationError):
        Supplier(brand="X", domain="   ")


def test_scrape_tier_values():
    assert {t.value for t in ScrapeTier} == {"shopify", "woocommerce", "jsonld", "tier3"}
