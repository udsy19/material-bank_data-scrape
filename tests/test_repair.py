"""Repair execution: deterministic re-probe, escalate to LLM slot when stuck."""

import pytest

import material_bank.repair as repair
from material_bank import db as db_mod
from material_bank import jobs
from material_bank.models import ProbeResult, ProbeStatus, ScrapeTier


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    c.execute("INSERT INTO suppliers(brand,domain,categories,status,scrape_tier) "
              "VALUES('S','s.com','tiles','active',NULL)")
    c.commit()
    yield c
    c.close()


def test_repair_reclassifies_on_reprobe(conn, monkeypatch):
    # site now looks like Shopify on re-probe
    def fake_classify(domain, fetcher):
        r = ProbeResult(domain=domain, scrape_tier=ScrapeTier.SHOPIFY,
                        probe_status=ProbeStatus.OK, probed_at=db_mod.now_iso())
        return r
    monkeypatch.setattr(repair, "classify", fake_classify)

    result = repair.run_repair(conn, "s.com")
    assert result["new_tier"] == "shopify"
    row = conn.execute("SELECT scrape_tier FROM suppliers WHERE domain='s.com'").fetchone()
    assert row["scrape_tier"] == "shopify"  # registry updated -> next harvest works


def test_repair_escalates_when_still_unclassified(conn, monkeypatch):
    def fake_classify(domain, fetcher):
        return ProbeResult(domain=domain, scrape_tier=None,
                           probe_status=ProbeStatus.AMBIGUOUS, probed_at=db_mod.now_iso())
    monkeypatch.setattr(repair, "classify", fake_classify)
    with pytest.raises(RuntimeError, match="LLM parser-repair"):
        repair.run_repair(conn, "s.com")


def test_drain_repairs_processes_queue(conn, monkeypatch, tmp_path):
    jobs.enqueue(conn, "repair", "s.com", priority=10)

    def fake_classify(domain, fetcher):
        return ProbeResult(domain=domain, scrape_tier=ScrapeTier.WOOCOMMERCE,
                           probe_status=ProbeStatus.OK, probed_at=db_mod.now_iso())
    monkeypatch.setattr(repair, "classify", fake_classify)

    final = repair.drain_repairs(tmp_path / "catalog.db")
    assert final["done"] == 1
