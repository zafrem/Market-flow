from decimal import Decimal as D

import pytest

from marketflow.decision.calc import INF, net_margin, price_floor, round_price, run_rate, spoilage_risk
from marketflow.decision.config import DecisionConfig, load_decision_config

CFG = DecisionConfig()
V = CFG.vat_rate


def test_net_margin_vat_inclusive():
    # 10,000 incl. VAT -> 9,090.91 net; costs 5000+500+1000 + 11.5% of 10,000
    amt, rate = net_margin(10000, D(5000), D(500), D(1000), D(0), D("11.5"), True, V)
    assert (amt, rate) == (D("1440.91"), D("15.85"))


def test_net_margin_vat_exclusive_and_promo():
    amt, rate = net_margin(10000, D(5000), D(500), D(1000), D(200), D("10"), False, V)
    assert (amt, rate) == (D("2300.00"), D("23.00"))


def test_negative_margin_is_negative():
    assert net_margin(5000, D(5000), D(500), D(1000), D(0), D("11.5"), True, V)[0] < 0


@pytest.mark.parametrize("fee,vat,target", [("11.5", True, "0"), ("11.5", True, "10"), ("0", False, "0"),
                                            ("25", True, "5"), ("8.3", False, "20")])
def test_floor_is_exactly_the_minimal_integer_meeting_target(fee, vat, target):
    cfg = DecisionConfig(target_min_margin_pct=D(target))
    costs, t, f = D(5000) + D(500) + D(1000) + D(150), D(target) / 100, D(fee) / 100

    def meets(p):                      # exact (unrounded) arithmetic, independent of the implementation's search
        pn = D(p) / (1 + V) if vat else D(p)
        return pn - costs - D(p) * f >= t * pn

    fl = price_floor(D(5000), D(500), D(1000), D(150), D(fee), vat, cfg)
    assert meets(fl) and not meets(fl - 1)


def test_floor_breakeven_known_value():
    assert price_floor(D(5000), D(500), D(1000), D(0), D("11.5"), True, CFG) == 8186


def test_floor_infeasible_raises():
    with pytest.raises(ValueError, match="no feasible"):
        price_floor(D(100), D(0), D(0), D(0), D("95"), True, CFG)


def test_run_rate_srs_example():
    assert run_rate(500, [50, 50, 50]) == D(10)
    assert run_rate(500, [60, 50, 40, 999]) == D(10)          # only the newest 3 rows count


def test_run_rate_edge_cases():
    assert run_rate(500, [0, 0, 0]) == INF and run_rate(500, []) == INF
    assert run_rate(100, [40, 60]) == D(2)                    # fewer than 3 days: average what exists


def test_spoilage_srs_example_and_boundaries():
    rr = run_rate(500, [50, 50, 50])
    assert spoilage_risk(500, 5, rr, D(0)) is True           # D-day 5 <= run-rate 10
    assert spoilage_risk(500, 10, rr, D(0)) is True          # equal counts as risk (<=)
    assert spoilage_risk(500, 11, rr, D(0)) is False
    assert spoilage_risk(500, 30, INF, D(0)) is True         # nothing sells -> will spoil
    assert spoilage_risk(0, 1, INF, D(0)) is False          # no stock, nothing to spoil
    assert spoilage_risk(500, None, rr, D(0)) is False


def test_buffer_extends_risk_window():
    assert spoilage_risk(500, 12, D(10), D(2)) is True and spoilage_risk(500, 13, D(10), D(2)) is False


def test_round_price():
    assert round_price(D("9433"), 10) == 9430 and round_price(D("9435"), 10) == 9440 and round_price(D("9434.9"), 10) == 9430


def test_config_override_and_validation():
    cfg = load_decision_config({"max_step_pct": 5, "Fresh": {"max_step_pct": 20, "round_to": 100}}, "Fresh")
    assert (cfg.max_step_pct, cfg.round_to) == (D(20), 100)
    assert load_decision_config({"max_step_pct": 5}, "Ambient").max_step_pct == D(5)
    for bad in ({"nope": 1}, {"undercut_pct": 9}, {"max_step_pct": 0}, {"vacuum_min_pct": 20}):
        with pytest.raises(ValueError):
            load_decision_config(bad)
