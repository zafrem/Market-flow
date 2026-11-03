"""SRS §6.3 scenarios for F-03-2 / F-04-1 / F-04-2 / F-04-3 plus the guards from the v1.2 algorithm."""
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from marketflow.decision.config import DecisionConfig
from marketflow.decision.engine import Context, evaluate

NOW = datetime(2026, 11, 3, 1, 0)
CFG = DecisionConfig()


def row(**o):
    r = dict(std_item_id=1, internal_sku="APPLE-1", channel_name="SHOPA", purchase_cost=D(5000), fixed_cost=D(500),
             logistics_cost=D(1000), channel_fee_rate=D("11.5"), promo_cost=D(0), vat_inclusive=1, pack_std_units=D(1),
             current_selling_price=10000, stock_qty=1000, daily_sales_qty=50, min_price_floor=8186,
             lifecycle_metric={"remaining_days": None}, category_l="Fresh")
    r.update(o)
    return r


def comp(price, oos=False, age_h=1, conf="0.9", code="c1"):
    return dict(platform="P", channel_item_code=code, normalized_unit_price=D(price), is_out_of_stock=oos,
                match_confidence=D(conf), scraped_at=NOW - timedelta(hours=age_h))


def ctx(comps=(), sales=(50, 50, 50), wholesale=None, last=None):
    return Context(list(comps), list(sales), wholesale, last)


def test_competitor_drop_40pct_never_breaches_floor_and_warns_red():     # SRS F-03-2 / F-04-1
    rec = evaluate(row(), ctx([comp(6000)]), CFG, NOW)                    # 10,000 -> competitor at 6,000 (-40%)
    assert rec.flags["REVERSE_MARGIN_WARNING"] == "RED"
    assert rec.suggested_price >= 8186 and rec.suggested_price == 10000   # keeps current, no automatic cut
    assert rec.reason_code == "REVERSE_MARGIN" and rec.status == "SUPPRESSED"


def test_reverse_margin_when_current_already_below_floor_proposes_floor():
    rec = evaluate(row(current_selling_price=8000), ctx([comp(6000)]), CFG, NOW)
    assert rec.suggested_price == 8186 and rec.status == "PENDING" and "REVERSE_MARGIN_WARNING" in rec.flags


def test_competitive_match_is_step_capped_and_rounded():
    rec = evaluate(row(min_price_floor=5000), ctx([comp(8000)]), CFG, NOW)    # wants 8,000, cap is 10%
    assert (rec.suggested_price, rec.reason_code, rec.status) == (9000, "COMPETITIVE_MATCH", "PENDING")
    rec = evaluate(row(min_price_floor=5000), ctx([comp(9433)]), CFG, NOW)
    assert rec.suggested_price == 9430                                    # 10-won tick
    rec = evaluate(row(min_price_floor=5000), ctx([comp(12500)]), CFG, NOW)
    assert rec.suggested_price == 11000                                   # upward moves are capped too


def test_target_just_above_floor_is_clamped_to_floor_not_warned():
    rec = evaluate(row(current_selling_price=9000, min_price_floor=8186), ctx([comp(8200)]), CFG, NOW)
    assert rec.suggested_price == 8200 and "REVERSE_MARGIN_WARNING" not in rec.flags


def test_lowest_in_stock_competitor_is_the_reference_and_oos_is_ignored():
    rec = evaluate(row(min_price_floor=5000), ctx([comp(9500, code="a"), comp(9000, code="b"), comp(100, oos=True, code="c")]), CFG, NOW)
    assert rec.flags["metrics"]["competitor_reference"] == "9000.00" and rec.suggested_price == 9000


def test_pack_size_conversion_and_undercut():
    r = row(pack_std_units=D(3), min_price_floor=5000, current_selling_price=9500)
    assert evaluate(r, ctx([comp(3000)]), CFG, NOW).flags["metrics"]["competitor_reference"] == "9000.00"
    rec = evaluate(r, ctx([comp(3000)]), replace(CFG, undercut_pct=D(2)), NOW)
    assert rec.suggested_price == 8820                                    # 9000 * 0.98, inside the 10% step window
    capped = evaluate(row(pack_std_units=D(3), min_price_floor=5000), ctx([comp(3000)]), replace(CFG, undercut_pct=D(2)), NOW)
    assert capped.suggested_price == 9000                                 # from 10,000 the step cap binds first


def test_stale_and_low_confidence_competitors_are_ignored():
    assert evaluate(row(), ctx([comp(8000, age_h=30)]), CFG, NOW) is None
    assert evaluate(row(), ctx([comp(8000, conf="0.79")]), CFG, NOW) is None
    assert evaluate(row(), ctx([comp(8000, conf="0.80")]), CFG, NOW) is not None


def test_no_change_and_healthy_returns_none():
    assert evaluate(row(), ctx([comp(10000)]), CFG, NOW) is None
    assert evaluate(row(), ctx(), CFG, NOW) is None


def test_spoilage_srs_example_triggers_flag_and_flash_discount():       # SRS F-04-2
    r = row(stock_qty=500, lifecycle_metric={"remaining_days": 5})
    rec = evaluate(r, ctx(sales=(50, 50, 50)), CFG, NOW)
    assert rec.flags["SPOILAGE_RISK_FLAG"] is True and rec.reason_code == "SPOILAGE_RISK"
    assert rec.flags["metrics"]["run_rate_days"] == "10.00" and "FLASH_COUPON" in rec.flags["actions"]
    assert rec.suggested_price == 9000 and rec.status == "PENDING"


def test_spoilage_at_floor_cannot_discount_further_but_still_warns():
    r = row(stock_qty=500, lifecycle_metric={"remaining_days": 5}, current_selling_price=8186)
    rec = evaluate(r, ctx(), CFG, NOW)
    assert rec.suggested_price == 8186 and rec.status == "SUPPRESSED" and rec.flags["SPOILAGE_RISK_FLAG"]


def test_spoilage_follows_competitor_down_but_not_below_floor():
    r = row(stock_qty=500, lifecycle_metric={"remaining_days": 5}, min_price_floor=8500)
    assert evaluate(r, ctx([comp(8700)]), CFG, NOW).suggested_price == 8700
    assert evaluate(r, ctx([comp(5000)]), CFG, NOW).suggested_price == 8500


def test_supply_vacuum_scales_5_to_15_percent():                         # SRS F-04-3
    r = row()                                                             # run-rate 20 days, no expiry pressure
    all_oos = evaluate(r, ctx([comp(9000, oos=True)]), CFG, NOW)
    assert (all_oos.reason_code, all_oos.suggested_price) == ("SUPPLY_VACUUM", 11500)   # +15%
    half = evaluate(r, ctx([comp(10000, code="a"), comp(9000, oos=True, code="b")]), CFG, NOW)
    assert half.suggested_price == 11000 and half.flags["vacuum_pct"] == "10.00"       # +10%


def test_vacuum_blocked_by_spoilage_low_runrate_or_no_stockout():
    oos = [comp(9000, oos=True)]
    spoiling = row(stock_qty=500, lifecycle_metric={"remaining_days": 5})
    assert evaluate(spoiling, ctx(oos), CFG, NOW).reason_code == "SPOILAGE_RISK"
    assert evaluate(row(stock_qty=300), ctx(oos, sales=(50, 50, 50)), CFG, NOW) is None     # run-rate 6 days <= 7
    assert evaluate(row(), ctx([comp(10000)]), CFG, NOW) is None


def test_wholesale_rise_blocks_price_cuts_only():
    r = row(min_price_floor=5000)
    guarded = evaluate(r, ctx([comp(9000)], wholesale=D("8")), CFG, NOW)
    assert guarded.reason_code == "WHOLESALE_GUARD" and guarded.suggested_price == 10000 and guarded.status == "SUPPRESSED"
    assert evaluate(r, ctx([comp(9000)], wholesale=D("3")), CFG, NOW).suggested_price == 9000
    assert evaluate(r, ctx([comp(11000)], wholesale=D("8")), CFG, NOW).suggested_price == 11000   # raises still allowed


def test_cooldown_after_applied_change():
    r = row(min_price_floor=5000)
    assert evaluate(r, ctx([comp(9000)], last=NOW - timedelta(hours=2)), CFG, NOW) is None
    assert evaluate(r, ctx([comp(9000)], last=NOW - timedelta(hours=7)), CFG, NOW) is not None


def test_cooldown_holds_the_price_but_not_the_safety_alerts():
    recent = NOW - timedelta(hours=2)
    # spoilage during cool-down: no price move, but the flag and alert still come through
    spoiling = evaluate(row(stock_qty=300, lifecycle_metric={"remaining_days": 2}, daily_sales_qty=100),
                        ctx([], sales=(100, 100, 100), last=recent), CFG, NOW)
    assert spoiling.flags["SPOILAGE_RISK_FLAG"] and spoiling.suggested_price == 10000
    assert spoiling.status == "SUPPRESSED" and spoiling.reason_code == "COOLDOWN_HOLD" and int(spoiling.flags["COOLDOWN_HOLD"]) < 10000
    # reverse margin during cool-down: still red
    rev = evaluate(row(), ctx([comp(5000)], last=recent), CFG, NOW)
    assert rev.flags["REVERSE_MARGIN_WARNING"] == "RED" and rev.suggested_price == 10000
    # a plain competitive cut stays fully blocked, as before
    assert evaluate(row(min_price_floor=5000), ctx([comp(9000)], last=recent), CFG, NOW) is None


def test_low_margin_alert_even_without_a_price_change():
    rec = evaluate(row(current_selling_price=8300, min_price_floor=8186), ctx(), CFG, NOW)
    assert rec.flags["LOW_MARGIN"] is True and rec.status == "SUPPRESSED" and rec.reason_code == "LOW_MARGIN"
    assert D(rec.flags["metrics"]["margin_rate"]) <= 10


def test_suggestion_never_below_floor_property():
    for current in (8186, 8500, 10000, 15000):
        for c in (1, 3000, 8000, 9999, 20000):
            for spoil in (None, 3):
                rec = evaluate(row(current_selling_price=current, stock_qty=500, lifecycle_metric={"remaining_days": spoil}),
                               ctx([comp(c)]), CFG, NOW)
                if rec is not None and current >= 8186:
                    assert rec.suggested_price >= 8186, (current, c, spoil, rec)
