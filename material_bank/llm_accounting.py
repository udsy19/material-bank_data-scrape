"""LLM call accounting — the money ledger.

Every LLM call (realtime or batch, success or failure, incl. retries and
escalations) is written to ``llm_calls`` with its ACTUAL token counts (from the
provider's usageMetadata) and a ₹ cost derived from them at the published rate.
Nothing is estimated. This is the source of truth for spend, the budget
circuit-breaker, and the LLM-ops cockpit.

Rates are kept visible (``PRICING`` + ``USD_INR``) so the ₹ math is auditable
and trivially updated when Google changes pricing. Batch is billed at 50%.
"""

from __future__ import annotations

import sqlite3

from .db import now_iso

USD_INR = 83.0                              # update as needed; shown in the report
# model -> (input $/1M tokens, output $/1M tokens), published Gemini rates.
PRICING = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
}
_DEFAULT_RATE = (0.30, 2.50)


def call_cost(input_tokens: int, output_tokens: int, model: str, *, batch: bool = False) -> float:
    """₹ cost of one call from its actual token counts (batch = 50% off)."""
    ci, co = PRICING.get(model, _DEFAULT_RATE)
    usd = (input_tokens / 1e6) * ci + (output_tokens / 1e6) * co
    if batch:
        usd *= 0.5
    return round(usd * USD_INR, 4)


def log_call(conn: sqlite3.Connection, *, product_id, model, phase, attempt,
             input_tokens=0, output_tokens=0, latency_ms=0, status,
             fail_reason=None, batch=False, prompt_version=None, batch_job=None) -> float:
    """Write one ledger row; returns its ₹ cost (so callers can sum spend)."""
    cost = call_cost(input_tokens or 0, output_tokens or 0, model, batch=batch)
    conn.execute(
        "INSERT INTO llm_calls (occurred_at, product_id, model, prompt_version, phase, "
        "attempt, input_tokens, output_tokens, cost_inr, latency_ms, status, fail_reason, "
        "batch_job) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), product_id, model, prompt_version, phase, attempt,
         input_tokens or 0, output_tokens or 0, cost, latency_ms or 0, status,
         (fail_reason or "")[:200] or None, batch_job))
    return cost


def _rows(conn, sql, *p):
    return [dict(r) for r in conn.execute(sql, p)]


def spend_since(conn: sqlite3.Connection, days: float) -> float:
    r = conn.execute("SELECT COALESCE(SUM(cost_inr),0) FROM llm_calls "
                     "WHERE julianday('now') - julianday(occurred_at) <= ?", (days,)).fetchone()
    return round(r[0], 4)


def llm_report(conn: sqlite3.Connection, *, days: int = 30) -> dict:
    """The LLM-ops cockpit: spend (today / window / all-time), per-model and
    per-status breakdowns, verifier pass-rate, and a daily spend series."""
    tot = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(cost_inr),0) c, "
                       "COALESCE(SUM(input_tokens),0) it, COALESCE(SUM(output_tokens),0) ot "
                       "FROM llm_calls").fetchone()
    by_status = {r["status"]: {"calls": r["n"], "cost_inr": round(r["c"], 4)}
                 for r in conn.execute("SELECT status, COUNT(*) n, SUM(cost_inr) c "
                                       "FROM llm_calls GROUP BY status")}
    enriched = by_status.get("enriched", {}).get("calls", 0)
    verified = enriched + by_status.get("verifier_failed", {}).get("calls", 0)
    return {
        "rates": {"usd_inr": USD_INR, "pricing_usd_per_1m": PRICING},
        "all_time": {"calls": tot["n"], "cost_inr": round(tot["c"], 4),
                     "input_tokens": tot["it"], "output_tokens": tot["ot"]},
        "spend_today_inr": spend_since(conn, 1),
        "spend_window_inr": spend_since(conn, days),
        "by_status": by_status,
        "verifier_pass_rate": round(enriched / verified, 3) if verified else None,
        "by_model": _rows(conn, "SELECT model, COUNT(*) calls, ROUND(SUM(cost_inr),4) cost_inr, "
                          "SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens "
                          "FROM llm_calls GROUP BY model ORDER BY cost_inr DESC"),
        "daily": _rows(conn, "SELECT substr(occurred_at,1,10) day, COUNT(*) calls, "
                       "ROUND(SUM(cost_inr),4) cost_inr FROM llm_calls "
                       "GROUP BY day ORDER BY day DESC LIMIT ?", days),
    }


def recent_calls(conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0,
                 status: str | None = None) -> dict:
    """The raw ledger, newest first, joined to the product title — every call."""
    where = "WHERE c.status = ?" if status else ""
    params = ([status] if status else []) + [limit, offset]
    total = conn.execute(f"SELECT COUNT(*) FROM llm_calls c {where}",
                         ([status] if status else [])).fetchone()[0]
    items = _rows(conn, f"""
        SELECT c.id, c.occurred_at, c.product_id, p.title, c.model, c.prompt_version,
               c.phase, c.attempt, c.input_tokens, c.output_tokens, c.cost_inr,
               c.latency_ms, c.status, c.fail_reason
        FROM llm_calls c LEFT JOIN products p ON p.id = c.product_id
        {where} ORDER BY c.id DESC LIMIT ? OFFSET ?""", *params)
    return {"total": total, "count": len(items), "items": items}


def main(argv=None) -> int:
    import argparse
    import sys

    from . import db

    ap = argparse.ArgumentParser(prog="mb-llm-report")
    ap.add_argument("--calls", type=int, default=15, help="how many recent calls to list")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    rep = llm_report(conn)
    p = lambda *a: print(*a, file=sys.stderr)  # noqa: E731
    at = rep["all_time"]
    p("LLM SPEND  today ₹%.2f | 30d ₹%.2f | all-time ₹%.2f (%d calls, %d in + %d out tokens)"
      % (rep["spend_today_inr"], rep["spend_window_inr"], at["cost_inr"], at["calls"],
         at["input_tokens"], at["output_tokens"]))
    p("verifier pass-rate: %s   (rate: $%s/$%s per 1M in/out, ₹%.0f/$)"
      % (rep["verifier_pass_rate"], *PRICING.get("gemini-2.5-flash"), USD_INR))
    p("by status:", {k: f"{v['calls']} (₹{v['cost_inr']:.2f})" for k, v in rep["by_status"].items()})
    for m in rep["by_model"]:
        p("  model %-22s %5d calls  ₹%.2f" % (m["model"], m["calls"], m["cost_inr"]))
    p("recent calls:")
    for c in recent_calls(conn, limit=args.calls)["items"]:
        p("  #%d %s  %s  a%d  %s  in%d/out%d  ₹%.4f  %sms  %s"
          % (c["id"], c["occurred_at"][11:19], (c["title"] or "?")[:28], c["attempt"],
             c["status"], c["input_tokens"], c["output_tokens"], c["cost_inr"],
             c["latency_ms"], c["fail_reason"] or ""))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
