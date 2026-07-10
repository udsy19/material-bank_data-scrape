"""Demand instrumentation: event logging, intent capture, claims, metrics."""

import pytest

from material_bank import db as db_mod
from material_bank import events


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "catalog.db", check_same_thread=False)
    db_mod.migrate(c)
    yield c
    c.close()


def test_log_event_ignores_unknown_kinds(conn):
    events.log_event(conn, "search", session_id="s1", query="marble tile")
    events.log_event(conn, "not_a_kind", session_id="s1")   # silently ignored
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n == 1


def test_quote_records_and_fires_event(conn):
    qid = events.record_quote(conn, product_id=7, supplier_domain="orientbell.com",
                              source_url="https://o/x", buyer_name="A Firm",
                              buyer_contact="buyer@firm.example", message="need 20 boxes")
    assert qid == 1
    row = conn.execute("SELECT * FROM quote_requests WHERE id=?", (qid,)).fetchone()
    assert row["supplier_domain"] == "orientbell.com" and row["status"] == "new"
    # a quote also shows up as a demand event
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='quote_request'").fetchone()[0] == 1


def test_claim_flow_validates_kind(conn):
    cid = events.record_claim(conn, supplier_domain="kajaria.com", kind="remove",
                              claimant_email="legal@kajaria.example", message="please remove")
    assert cid == 1
    assert conn.execute("SELECT kind FROM supplier_claims WHERE id=?", (cid,)).fetchone()[0] == "remove"
    with pytest.raises(ValueError):
        events.record_claim(conn, supplier_domain="x.com", kind="bogus")


def test_demand_metrics_shape_and_ctr(conn):
    d0 = events.demand_metrics(conn)
    assert d0["active_sessions"] == 0 and d0["search_ctr"] == 0.0   # honest zero

    for i in range(4):
        events.log_event(conn, "search", session_id="s1", query=f"q{i}")
    events.log_event(conn, "result_click", session_id="s1", product_id=1)
    events.log_event(conn, "product_view", session_id="s2", product_id=1)
    events.record_quote(conn, product_id=1, supplier_domain="o.com")

    d = events.demand_metrics(conn)
    assert d["active_sessions"] == 2 and d["searches"] == 4
    assert d["result_clicks"] == 1 and d["search_ctr"] == 0.25
    assert d["product_views"] == 1 and d["quote_requests"] == 1
    assert d["quote_requests_total"] == 1
