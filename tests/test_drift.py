"""Yield-drift self-healing: detect collapses / quarantine spikes -> repair jobs."""

import pytest

from material_bank import db as db_mod
from material_bank import drift, jobs


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db")
    db_mod.migrate(c)
    yield c
    c.close()


def _hist(conn, domain, seq):
    """seq = list of (products, priced, quarantined) oldest->newest."""
    for products, priced, quar in seq:
        db_mod.record_harvest(conn, domain, products=products, priced=priced, quarantined=quar)


def test_detects_yield_collapse(conn):
    _hist(conn, "somany.com", [(500, 400, 0), (120, 100, 0)])  # 76% drop
    d = drift.detect_drift(conn)
    assert len(d) == 1 and d[0]["domain"] == "somany.com"
    assert d[0]["drop_pct"] == 76.0 and d[0]["reason"] == "yield_drop"


def test_stable_yield_not_flagged(conn):
    _hist(conn, "steady.com", [(500, 0, 0), (495, 0, 0)])  # ~1% drop
    assert drift.detect_drift(conn) == []


def test_growth_not_flagged(conn):
    _hist(conn, "growing.com", [(100, 0, 0), (900, 0, 0)])  # uncapped resume grew it
    assert drift.detect_drift(conn) == []


def test_tiny_supplier_ignored(conn):
    _hist(conn, "tiny.com", [(10, 0, 0), (2, 0, 0)])  # big % but below min_prev
    assert drift.detect_drift(conn) == []


def test_quarantine_spike_detected(conn):
    db_mod.record_harvest(conn, "broken.com", products=5, priced=0, quarantined=95)
    s = drift.detect_quarantine_spikes(conn)
    assert len(s) == 1 and s[0]["domain"] == "broken.com"


def test_scan_opens_repair_jobs(conn):
    _hist(conn, "somany.com", [(500, 0, 0), (100, 0, 0)])   # drift
    db_mod.record_harvest(conn, "broken.com", products=2, priced=0, quarantined=80)  # spike
    rep = drift.scan_and_open(conn)
    assert rep["repairs_opened"] == 2
    assert jobs.counts(conn, "repair")["pending"] == 2
    # idempotent: a second scan doesn't duplicate
    drift.scan_and_open(conn)
    assert jobs.counts(conn, "repair")["pending"] == 2


def test_repair_job_is_high_priority(conn):
    _hist(conn, "somany.com", [(500, 0, 0), (100, 0, 0)])
    drift.scan_and_open(conn)
    j = conn.execute("SELECT priority FROM pipeline_jobs WHERE target='somany.com'").fetchone()
    assert j["priority"] == 10
