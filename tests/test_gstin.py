"""GSTIN adapter: off by default (no fabrication), validates when enabled."""

import json

import pytest

from material_bank import db as db_mod
from material_bank import gstin


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "c.db", check_same_thread=False)
    db_mod.migrate(c)
    c.execute("INSERT INTO suppliers (domain, brand, status, legal_name, state) "
              "VALUES ('somany.example','Somany','active','Somany Ceramics Limited','Haryana')")
    c.commit()
    yield c
    c.close()


def test_disabled_by_default_is_noop(conn):
    out = gstin.backfill_gstins(conn)          # no provider env, no lookup_fn
    assert out == {"enabled": False, "updated": 0, "attempted": 0}
    assert conn.execute("SELECT gstin FROM suppliers WHERE domain='somany.example'").fetchone()[0] is None


def test_injected_lookup_stores_validated_gstin_with_provenance(conn):
    def fake(name, state):
        return "06AABCS1234M1Z5" if "Somany" in name else None
    out = gstin.backfill_gstins(conn, lookup_fn=fake)
    assert out["enabled"] and out["updated"] == 1
    r = conn.execute("SELECT gstin, supplier_provenance FROM suppliers WHERE domain='somany.example'").fetchone()
    assert r["gstin"] == "06AABCS1234M1Z5"
    assert json.loads(r["supplier_provenance"])["gstin"]["basis"] == "registry:gstin"


def test_invalid_gstin_from_provider_is_rejected(conn):
    out = gstin.backfill_gstins(conn, lookup_fn=lambda n, s: "NOT-A-GSTIN")
    assert out["updated"] == 0
    assert conn.execute("SELECT gstin FROM suppliers WHERE domain='somany.example'").fetchone()[0] is None
