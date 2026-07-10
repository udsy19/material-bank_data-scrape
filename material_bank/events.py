"""Demand instrumentation + intent capture.

Everything else in this system is supply-side. This module is the first
demand-side signal: lightweight event logging (search / view / click), the
intent-capture path (a buyer's quote request routed to the supplier), and the
supplier claim/correct/takedown flow. It exists so that the moment the catalog
is in front of a real user we are *measuring* adoption and intent — the metrics
that actually determine the outcome — instead of asserting them.

Deliberately minimal and privacy-light: a random client session id, no
tracking cookies, no personal data beyond what a buyer voluntarily submits to
get a quote (first-party, consented). Demand metrics are computed on read.
"""

from __future__ import annotations

import json
import sqlite3

from .db import now_iso

_EVENT_KINDS = {"search", "product_view", "result_click", "quote_request"}


def log_event(conn: sqlite3.Connection, kind: str, *, session_id: str | None = None,
              query: str | None = None, product_id: int | None = None,
              supplier_domain: str | None = None, meta: dict | None = None) -> None:
    """Record a lightweight interaction. Unknown kinds are ignored (never raise
    on a telemetry call — instrumentation must not break the request path)."""
    if kind not in _EVENT_KINDS:
        return
    conn.execute(
        "INSERT INTO events (occurred_at, session_id, kind, query, product_id, "
        "supplier_domain, meta) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), session_id, kind, query, product_id, supplier_domain,
         json.dumps(meta) if meta else None))
    conn.commit()


def record_quote(conn: sqlite3.Connection, *, product_id: int | None,
                 supplier_domain: str | None, source_url: str | None = None,
                 buyer_name: str | None = None, buyer_contact: str | None = None,
                 message: str | None = None) -> int:
    """A buyer's request to source a product — the intent signal Act III sells.
    Also logged as an event so it shows up in demand metrics."""
    cur = conn.execute(
        "INSERT INTO quote_requests (created_at, product_id, supplier_domain, "
        "source_url, buyer_name, buyer_contact, message) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), product_id, supplier_domain, source_url, buyer_name,
         buyer_contact, message))
    conn.commit()
    log_event(conn, "quote_request", product_id=product_id,
              supplier_domain=supplier_domain)
    return cur.lastrowid


def record_claim(conn: sqlite3.Connection, *, supplier_domain: str, kind: str,
                 claimant_email: str | None = None, message: str | None = None) -> int:
    """A supplier claiming, correcting, or requesting removal of their records —
    the flow that turns a brand's objection into an Act III onboarding."""
    if kind not in {"claim", "correct", "remove"}:
        raise ValueError(f"bad claim kind: {kind}")
    cur = conn.execute(
        "INSERT INTO supplier_claims (created_at, supplier_domain, kind, "
        "claimant_email, message) VALUES (?,?,?,?,?)",
        (now_iso(), supplier_domain, kind, claimant_email, message))
    conn.commit()
    return cur.lastrowid


def _count(conn, sql, *params) -> int:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


def demand_metrics(conn: sqlite3.Connection, *, window_days: int = 7) -> dict:
    """The demand-side scorecard. Zero until there are users — and reporting a
    real zero is more honest than omitting the row."""
    since = f"julianday('now') - julianday(occurred_at) <= {float(window_days)}"
    searches = _count(conn, f"SELECT COUNT(*) FROM events WHERE kind='search' AND {since}")
    clicks = _count(conn, f"SELECT COUNT(*) FROM events WHERE kind='result_click' AND {since}")
    return {
        "window_days": window_days,
        "active_sessions": _count(
            conn, f"SELECT COUNT(DISTINCT session_id) FROM events "
                  f"WHERE session_id IS NOT NULL AND {since}"),
        "searches": searches,
        "product_views": _count(conn, f"SELECT COUNT(*) FROM events WHERE kind='product_view' AND {since}"),
        "result_clicks": clicks,
        "search_ctr": round(clicks / searches, 3) if searches else 0.0,
        "quote_requests": _count(conn, f"SELECT COUNT(*) FROM events WHERE kind='quote_request' AND {since}"),
        "quote_requests_total": _count(conn, "SELECT COUNT(*) FROM quote_requests"),
        "supplier_claims_total": _count(conn, "SELECT COUNT(*) FROM supplier_claims"),
    }
