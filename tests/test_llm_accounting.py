"""LLM accounting: cost math (real tokens), the ledger, and the rollup report."""

import pytest

from material_bank import db as db_mod
from material_bank import llm_accounting as acct


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "c.db", check_same_thread=False)
    db_mod.migrate(c)
    yield c
    c.close()


def test_cost_is_derived_from_tokens_and_rate():
    # gemini-2.5-flash: $0.30/1M in, $2.50/1M out, ×83 ₹/$
    c = acct.call_cost(1_000_000, 1_000_000, "gemini-2.5-flash")
    assert c == round((0.30 + 2.50) * 83.0, 4)
    # batch is half
    assert acct.call_cost(1_000_000, 1_000_000, "gemini-2.5-flash", batch=True) == round(c / 2, 4)
    # zero tokens (an api_error call) costs nothing
    assert acct.call_cost(0, 0, "gemini-2.5-flash") == 0.0


def test_log_call_writes_ledger_and_returns_cost(conn):
    cost = acct.log_call(conn, product_id=7, model="gemini-2.5-flash", phase="realtime",
                         attempt=0, input_tokens=1000, output_tokens=500, latency_ms=800,
                         status="enriched", prompt_version="v3")
    r = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert r["product_id"] == 7 and r["status"] == "enriched" and r["cost_inr"] == cost > 0
    assert r["input_tokens"] == 1000 and r["latency_ms"] == 800


def test_report_rollups_and_pass_rate(conn):
    acct.log_call(conn, product_id=1, model="gemini-2.5-flash", phase="batch", attempt=0,
                  input_tokens=1000, output_tokens=500, status="enriched", batch=True)
    acct.log_call(conn, product_id=2, model="gemini-2.5-flash", phase="batch", attempt=0,
                  input_tokens=1000, output_tokens=500, status="verifier_failed", batch=True)
    acct.log_call(conn, product_id=3, model="gemini-2.5-flash", phase="realtime", attempt=0,
                  status="api_error", fail_reason="quota")
    rep = acct.llm_report(conn)
    assert rep["all_time"]["calls"] == 3
    assert rep["by_status"]["enriched"]["calls"] == 1
    assert rep["by_status"]["api_error"]["calls"] == 1
    assert rep["verifier_pass_rate"] == 0.5           # 1 enriched / (1 enriched + 1 verifier_failed)
    assert rep["spend_today_inr"] > 0
    assert rep["rates"]["usd_inr"] == acct.USD_INR
    assert rep["by_model"][0]["model"] == "gemini-2.5-flash"


def test_recent_calls_lists_every_call_newest_first(conn):
    for i in range(3):
        acct.log_call(conn, product_id=i, model="m", phase="realtime", attempt=0,
                      input_tokens=10, output_tokens=5, status="enriched")
    out = acct.recent_calls(conn, limit=2)
    assert out["total"] == 3 and out["count"] == 2
    assert out["items"][0]["id"] > out["items"][1]["id"]     # newest first
    only_err = acct.recent_calls(conn, status="api_error")
    assert only_err["total"] == 0


def test_reconcile_flags_divergence(tmp_path):
    """External-truth check: ledger vs Google billing. >10% gap must halt — the
    check that would have caught the 14x thinking-token undercount."""
    from material_bank import db as db_mod
    from material_bank import llm_accounting as acct
    c = db_mod.connect(tmp_path / "c.db"); db_mod.migrate(c)
    acct.log_call(c, product_id=None, model="gemini-flash-latest", phase="batch", attempt=0,
                  input_tokens=1_000_000, output_tokens=1_000_000, status="enriched", batch=True)
    c.commit()
    ledger = acct.spend_total(c)
    # in tolerance -> no halt
    near = acct.reconcile(c, ledger * 1.05)
    assert near["halt"] is False
    # 14x off (the incident) -> halt
    far = acct.reconcile(c, ledger * 14)
    assert far["halt"] is True and far["divergence"] > 0.9
