"""A/B decision rule — pre-committed, mechanical (no vibes)."""

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
