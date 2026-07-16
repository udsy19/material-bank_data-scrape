"""LLM call accounting — the money ledger.

Every LLM call (realtime or batch, success or failure, incl. retries and
escalations) is written to ``llm_calls`` with its ACTUAL token counts (from the
provider's usageMetadata) and a ₹ cost derived from them at the published rate.
Nothing is estimated. This is the source of truth for spend, the budget
circuit-breaker, and the LLM-ops cockpit.

Rates are a HAND-MAINTAINED forecast layer, not the source of truth for money:
Google exposes no pricing API (``models.get`` carries token limits + capabilities
but no price), so ``PRICING`` is transcribed from the public pricing page and
dated (``PRICING_AS_OF``). The ACTUAL truth is the billing console — see
``reconcile()``. Two guards keep the forecast from silently drifting from reality:
(1) spend is priced by the model that ACTUALLY RAN (the response ``modelVersion``,
logged into the ``model`` column) — never the requested ``-latest`` alias, which
can repoint to a 4×-pricier generation with no local diff; (2) a model with no
known rate is priced at the priciest rate (never undercount) AND flagged in the
report so the drift is visible, not buried. Batch is billed at 50%.
"""

from __future__ import annotations

import sqlite3

from .db import now_iso

USD_INR = 83.0                              # update as needed; shown in the report
PRICING_AS_OF = "2026-07"                   # hand-transcribed; Gemini has no pricing API
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
# model -> (input $/1M tokens, output $/1M tokens), published Gemini rates.
PRICING = {
    # Floating aliases repoint across generations — priced at their CURRENT
    # resolution (verified 2026-07 via modelVersion): -latest => gemini-3.5-flash.
    "gemini-flash-latest": (1.50, 9.00),        # alias -> 3.5-flash (was 2.5 @ 0.30/2.50)
    "gemini-flash-lite-latest": (0.25, 1.50),   # alias -> 3.1-flash-lite
    "gemini-3.5-flash": (1.50, 9.00),           # canary ceiling probe: grounding 0.657 @ ₹0.28/product
    "gemini-3.1-flash-lite": (0.25, 1.50),      # THE PIN: canary 0.582 grounding @ ₹0.044/product
    "gemini-2.5-flash": (0.30, 2.50),           # 404s for this key — kept for history
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
}
# Unknown model => price at the priciest known rate, never undercount (the $291 lesson).
_DEFAULT_RATE = (1.50, 9.00)


def is_priced(model: str) -> bool:
    """Do we have a published rate for this model, or are we guessing (default)?"""
    return model in PRICING


def rate_for(model: str) -> tuple[float, float, bool]:
    """(input $/1M, output $/1M, known). Unknown models get the priciest rate so
    the ledger over-, never under-, estimates — and ``known=False`` lets the report
    flag the spend instead of quietly trusting a guessed price."""
    if model in PRICING:
        return (*PRICING[model], True)
    return (*_DEFAULT_RATE, False)


def call_cost(input_tokens: int, output_tokens: int, model: str, *, batch: bool = False) -> float:
    """₹ cost of one call from its actual token counts (batch = 50% off)."""
    ci, co, _known = rate_for(model)
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


def spend_total(conn: sqlite3.Connection) -> float:
    """All-time ledger spend (₹). The basis for the hard budget cap in advance()."""
    return round(conn.execute("SELECT COALESCE(SUM(cost_inr),0) FROM llm_calls").fetchone()[0], 4)


def reconcile(conn: sqlite3.Connection, external_inr: float, *, tolerance: float = 0.10) -> dict:
    """Reconcile the self-reported ledger against the Google Cloud billing console —
    the external truth. The ledger was once internally consistent and 14x wrong
    (uncounted thinking tokens), the signature failure of self-reported accounting;
    this is the check that would have caught it. ``external_inr`` is read from the
    billing console (compare same-day-yesterday — billing data lags a few hours).
    ``halt`` fires when divergence exceeds tolerance: the operator (or the timer
    wrapper) must stop the drain and find the gap before spending more."""
    ledger = spend_total(conn)
    div = abs(ledger - external_inr) / external_inr if external_inr else (1.0 if ledger else 0.0)
    return {"ledger_inr": ledger, "external_inr": round(external_inr, 2),
            "divergence": round(div, 3), "tolerance": tolerance, "halt": div > tolerance}


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
    by_model = _rows(conn, "SELECT model, COUNT(*) calls, ROUND(SUM(cost_inr),4) cost_inr, "
                     "SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens "
                     "FROM llm_calls GROUP BY model ORDER BY cost_inr DESC")
    for m in by_model:                               # flag rows priced by guess, not by table
        m["priced"] = is_priced(m["model"])
    unpriced = [m for m in by_model if not m["priced"]]
    return {
        "rates": {"usd_inr": USD_INR, "pricing_usd_per_1m": PRICING,
                  "as_of": PRICING_AS_OF, "source": PRICING_SOURCE},
        # spend logged against a model with NO published rate (priced at the default,
        # guessed): if this is nonzero, the ₹ figures are estimates — reconcile.
        "unpriced_spend_inr": round(sum(m["cost_inr"] for m in unpriced), 4),
        "unpriced_models": [m["model"] for m in unpriced],
        "all_time": {"calls": tot["n"], "cost_inr": round(tot["c"], 4),
                     "input_tokens": tot["it"], "output_tokens": tot["ot"]},
        "spend_today_inr": spend_since(conn, 1),
        "spend_window_inr": spend_since(conn, days),
        "by_status": by_status,
        "verifier_pass_rate": round(enriched / verified, 3) if verified else None,
        "by_model": by_model,
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
    from .llm_enrich import MODEL
    p("verifier pass-rate: %s   (%s: $%s/$%s per 1M in/out, ₹%.0f/$)"
      % (rep["verifier_pass_rate"], MODEL, *PRICING.get(MODEL, _DEFAULT_RATE), USD_INR))
    p("by status:", {k: f"{v['calls']} (₹{v['cost_inr']:.2f})" for k, v in rep["by_status"].items()})
    p("rates as of %s (%s) — reconcile against the billing console for truth"
      % (rep["rates"]["as_of"], rep["rates"]["source"]))
    if rep["unpriced_spend_inr"]:
        p("  ⚠ ₹%.2f logged on UNPRICED models %s — priced at the default guess, VERIFY"
          % (rep["unpriced_spend_inr"], rep["unpriced_models"]))
    for m in rep["by_model"]:
        p("  model %-22s %5d calls  ₹%.2f%s" % (m["model"], m["calls"], m["cost_inr"],
                                                "" if m["priced"] else "  ⚠ unpriced"))
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
