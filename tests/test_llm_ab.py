"""A/B decision rule — pre-committed, mechanical (no vibes) — plus the re-canary harness."""

import json

from material_bank import llm_ab as ab


def _m(pass_rate=0.95, malformed=0.0, colour=0.7, kw=0.8):
    return {"pass_rate": pass_rate, "malformed_rate": malformed,
            "colour_agreement": colour, "keyword_overlap": kw}


def test_lite_wins_when_within_all_thresholds():
    d = ab.decide(_m(0.95, 0, 0.72, 0.80), _m(0.94, 0.01, 0.69, 0.78))
    assert d["winner"].startswith("flash-lite")


def test_malformed_json_disqualifies_lite():
    d = ab.decide(_m(), _m(malformed=0.05))          # > 2%
    assert d["winner"] == ab.FLASH and "malformed" in d["reason"]


def test_worse_colour_agreement_disqualifies_lite():
    d = ab.decide(_m(colour=0.80), _m(colour=0.65))  # 15pt worse
    assert d["winner"] == ab.FLASH and "vision" in d["reason"]


def test_lower_pass_rate_loses():
    d = ab.decide(_m(0.95), _m(0.90))                # 5pt below, > 2 tolerance
    assert d["winner"] == ab.FLASH


def test_weaker_keywords_loses():
    d = ab.decide(_m(kw=0.85), _m(kw=0.70))          # 15pt below
    assert d["winner"] == ab.FLASH


def test_colour_agreement_none_is_tolerated():
    # if neither has enough pixel checks, colour is not a blocker
    d = ab.decide({**_m(), "colour_agreement": None}, {**_m(0.94, 0.0, None, 0.79)})
    assert d["winner"].startswith("flash-lite")


def _canary_json(keywords=("marble tile", "vitrified tile")):
    return json.dumps({
        "description": [{"text": "A polished vitrified tile with soft grey veining.", "sources": ["f1"]}],
        "style_tags": [], "use_case_tags": [],
        "search_keywords": list(keywords), "vision": {}})


class _CanaryTransport:
    """Fake batch transport: echoes submitted request keys, thinking_tokens=0."""
    def __init__(self, model): self.model = model; self._keys = []
    def submit(self, reqs): self._keys = [r["key"] for r in reqs]; return "op/j1"
    def results(self, job):
        return [{"key": k, "text": _canary_json(),
                 "usage": {"input_tokens": 1800, "output_tokens": 450, "thinking_tokens": 0}}
                for k in self._keys]


def _seed_db(tmp_path, n=3, completeness=None):
    from material_bank import db as db_mod
    from material_bank.harvest.common import build_product
    c = db_mod.connect(tmp_path / "c.db", check_same_thread=False); db_mod.migrate(c)
    for i in range(n):
        p = build_product(brand="Orientbell", sku=f"s{i}", title="Emperador Marble Tile",
                          category="tiles", source="u", image_url="https://i/x.jpg", size_mm="600x600")
        pid = db_mod.upsert_product(c, p, supplier_domain="orientbell.com")
        if completeness:
            c.execute("UPDATE products SET completeness=? WHERE id=?", (completeness[i], pid))
    c.commit(); c.close()
    return str(tmp_path / "c.db")


def test_run_canary_emits_five_signals(tmp_path):
    dbp = _seed_db(tmp_path, n=3)
    out = ab.run_canary(dbp, model="gemini-flash-latest", n=3, prepare=lambda u: None,
                        transport_factory=_CanaryTransport)
    assert out["n"] == 3
    assert out["pass_rate"] == 1.0                       # honest description -> passes
    assert out["keyword_grounding"] > 0                  # keywords share tokens with "marble tile"
    assert out["thinking_tokens_max"] == 0               # budget=0 honored (the proof)
    assert out["retry_rate"] == 0.0                      # = 1 - pass_rate
    assert out["inr_per_product"] > 0                    # measured, reconciliation seed


def test_run_canary_logs_spend_for_reconciliation(tmp_path):
    from material_bank import db as db_mod
    from material_bank import llm_accounting as acct
    dbp = _seed_db(tmp_path, n=2)
    ab.run_canary(dbp, model="gemini-flash-latest", n=2, prepare=lambda u: None,
                  transport_factory=_CanaryTransport)
    c = db_mod.connect(dbp)
    assert c.execute("SELECT COUNT(*) FROM llm_calls WHERE phase='canary'").fetchone()[0] == 2
    assert acct.spend_total(c) > 0                        # spend captured -> reconcile() can compare


def test_canary_selects_highest_value_first(tmp_path):
    from material_bank import db as db_mod
    from material_bank.llm_batch import _select
    dbp = _seed_db(tmp_path, n=3, completeness=[20, 90, 55])   # sku0=20 sku1=90 sku2=55
    c = db_mod.connect(dbp)
    picked = [r["title"] for r in _select(c, 3)]
    comps = [c.execute("SELECT completeness FROM products WHERE title=? AND sku=?",
                       ("Emperador Marble Tile", f"s{i}")).fetchone()[0] for i in range(3)]
    # _select must return in completeness-DESC order: the 90 before the 55 before the 20
    ids_by_comp = [r["id"] for r in c.execute("SELECT id, completeness FROM products ORDER BY completeness DESC")]
    got = [r["id"] for r in _select(c, 3)]
    assert got == ids_by_comp                            # value-first drain order
