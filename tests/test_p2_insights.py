"""P2 analyses: failed promotions, prolonged discounts, new products, sales-response scenarios."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from marketflow.db import init_sqlite, make_engine
from marketflow.decision.service import Actor
from marketflow.insights import InsightConfig, elasticity_basis, evaluate_promotion, load_insight_config, new_products, project_scenarios, prolonged_discounts, promotion_results
from marketflow.promotions import add_promotion
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.web.app import create_app
from marketflow.web.repo import AppRepository
from marketflow.web.security import hash_password

T0 = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 3)
MD = Actor("kim@x.com", "MD")
CFG = InsightConfig()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'i.db'}", jwt_secret="j" * 48, inbox_dir=str(tmp_path / "inbox"))
    e = make_engine(s.database_url); init_sqlite(e)
    with e.begin() as c:
        c.execute(text("INSERT INTO master_products (category_l, category_m, item_name, standard_unit) VALUES ('Dairy','Milk','Milk','l')"))
    return type("E", (), dict(s=s, e=e, tp=FixedTimeProvider(T0), repo=AppRepository(e)))


def sales(env, item, start, days, fn, sku="APPLE-1", price=10000):
    with env.e.begin() as c:
        for i in range(days):
            d = start + timedelta(days=i)
            c.execute(text("INSERT INTO int_operations_inventory (recorded_date, std_item_id, internal_sku, channel_name, purchase_cost, fixed_cost, logistics_cost, channel_fee_rate, "
                           "promo_cost, vat_inclusive, pack_std_units, current_selling_price, stock_qty, daily_sales_qty, min_price_floor, lifecycle_metric) VALUES "
                           "(:d,:i,:s,'SHOPA',5000,500,1000,11.5,0,1,1,:p,100,:q,6000,'{}')"), {"d": d.isoformat(), "i": item, "s": sku, "p": price, "q": fn(d)})


def promo(env, name, s, e_, pct, cat="Fresh"):
    return add_promotion(env.repo, MD, env.tp.now(), name, s, e_, cat, "", pct)


def test_a_promotion_that_lifts_sales_is_ok_and_one_that_does_not_is_failed(env):
    # 60 days of flat 10 units/day; the first promotion doubles it, the second changes nothing
    sales(env, 1, TODAY - timedelta(days=59), 60, lambda d: 20 if date(2026, 10, 20) <= d <= date(2026, 10, 24) else 10)
    promo(env, "Big", date(2026, 10, 20), date(2026, 10, 24), 20)
    promo(env, "Dud", date(2026, 10, 27), date(2026, 10, 31), 20)
    res = {r["name"]: r for r in promotion_results(env.repo, TODAY, CFG)}
    assert res["Big"]["status"] == "OK" and res["Big"]["uplift_pct"] == 100.0 and res["Big"]["actual_units"] == 100.0 and res["Big"]["baseline_units"] == 50.0
    assert res["Dud"]["status"] == "FAILED" and res["Dud"]["uplift_pct"] == 0.0
    assert "no seasonal adjustment" in res["Dud"]["baseline_rule"]


def test_too_little_data_gives_no_verdict(env):
    sales(env, 1, TODAY - timedelta(days=3), 4, lambda d: 10)
    promo(env, "New", TODAY - timedelta(days=2), TODAY, 10)
    r = promotion_results(env.repo, TODAY, CFG)[0]
    assert r["status"] == "INSUFFICIENT_DATA" and r["uplift_pct"] is None


def test_scenarios_need_enough_promotions_and_show_their_basis(env):
    sales(env, 1, TODAY - timedelta(days=89), 90, lambda d: 10 * (1 + (0.4 if date(2026, 9, 1) <= d <= date(2026, 9, 5) else 0.8 if date(2026, 9, 15) <= d <= date(2026, 9, 19)
                                                                      else 0.2 if date(2026, 10, 1) <= d <= date(2026, 10, 5) else 0)))
    promo(env, "A", date(2026, 9, 1), date(2026, 9, 5), 20); promo(env, "B", date(2026, 9, 15), date(2026, 9, 19), 20)
    cfg = InsightConfig(min_promotions_for_scenarios=3)
    res = promotion_results(env.repo, TODAY, cfg, 120)
    assert elasticity_basis(res, "Fresh", cfg)["status"] == "INSUFFICIENT_DATA"
    promo(env, "C", date(2026, 10, 1), date(2026, 10, 5), 20)
    basis = elasticity_basis(promotion_results(env.repo, TODAY, cfg, 120), "Fresh", cfg)
    assert basis["status"] == "OK" and basis["promotions_used"] == 3 and basis["span"] == ["2026-09-01", "2026-10-05"]
    assert basis["elasticity"] == {"pessimistic": 1.5, "base": 2.0, "optimistic": 3.0}        # uplift 20/40/80% over a 20% cut
    proj = project_scenarios(basis, weekly_units=70, current_price=10000, new_price=9000, unit_margin_now=2000, unit_margin_new=1000)
    assert proj["price_cut_pct"] == 10.0 and proj["scenarios"]["base"]["weekly_units"] == 84.0 and proj["scenarios"]["base"]["weekly_margin"] == 84000
    assert proj["scenarios"]["base"]["vs_now"] == 84000 - 140000
    assert project_scenarios({"status": "INSUFFICIENT_DATA", "promotions_used": 1, "needed": 3}, 70, 10000, 9000, 1, 1)["status"] == "INSUFFICIENT_DATA"


def retail(env, platform, code, title, day, sale, orig, item=1):
    with env.e.begin() as c:
        c.execute(text("INSERT INTO ext_retail_prices (scraped_at, platform, std_item_id, match_confidence, channel_item_code, product_title, original_price, sale_price, "
                       "normalized_unit_price, region_code) VALUES (:t,:p,:i,0.9,:c,:ti,:o,:s,100,'KR')"),
                  {"t": datetime.combine(day, datetime.min.time()).isoformat(sep=" "), "p": platform, "i": item, "c": code, "ti": title, "o": orig, "s": sale})


def test_prolonged_discount_needs_enough_observed_days_and_share(env):
    for i in range(30):
        d = TODAY - timedelta(days=i)
        retail(env, "SHOPA", "LONG", "사과 3kg", d, 8000 if i % 5 else 10000, 10000)       # discounted on 24 of 30 days
        retail(env, "SHOPA", "RARE", "배 3kg", d, 8000 if i % 5 == 0 else 10000, 10000)    # 6 of 30
    for i in range(5):
        retail(env, "SHOPA", "SHORT", "귤 1kg", TODAY - timedelta(days=i), 5000, 10000)    # always discounted but only 5 days seen
    out = prolonged_discounts(env.repo, TODAY, CFG)
    assert [o["channel_item_code"] for o in out] == ["LONG"] and out[0]["days_observed"] == 30 and out[0]["days_discounted"] == 24 and out[0]["share_pct"] == 80.0


def test_new_products_exclude_renames_and_replaced_codes_and_unwatched_platforms(env):
    for i in range(40):
        d = TODAY - timedelta(days=i)
        retail(env, "SHOPA", "OLD1", "사과 3kg", d, 9000, 10000)
        if i > 20:
            retail(env, "SHOPA", "GONE", "복숭아 2kg", d, 9000, 10000, item=2)        # disappeared three weeks ago
    d = TODAY - timedelta(days=3)
    retail(env, "SHOPA", "NEW1", "[특가] 딸기 500g", d, 9000, 10000, item=3)           # genuinely new
    retail(env, "SHOPA", "REN1", "사과 2kg 특대", TODAY - timedelta(days=2), 9000, 10000)   # same product, new pack: same letters? no, extra word -> new
    retail(env, "SHOPA", "REN2", "[행사] 사과 5kg", d, 9000, 10000)                    # same letters as OLD1 once decoration and digits go: a rename
    retail(env, "SHOPA", "REPL", "백도 2kg", d, 9000, 10000, item=2)                  # replaces GONE for the same catalog item
    retail(env, "FRESHB", "X1", "참외 1kg", d, 9000, 10000, item=4)                    # platform watched for only 3 days: nothing there counts as new
    got = {n["channel_item_code"] for n in new_products(env.repo, TODAY, CFG)}
    assert got == {"NEW1", "REN1"}


def test_config_rejects_bad_values():
    assert load_insight_config({"min_uplift_pct": 8}).min_uplift_pct == 8.0
    with pytest.raises(ValueError):
        load_insight_config({"min_promo_days": "many"})


def test_api_scope_roles_and_scenarios(env):
    sales(env, 1, TODAY - timedelta(days=59), 60, lambda d: 10)
    promo(env, "Dud", date(2026, 10, 27), date(2026, 10, 31), 20); promo(env, "DairyDay", date(2026, 10, 27), date(2026, 10, 31), 10, "Dairy")
    for email, role, cats in (("kim@x.com", "MD", [("Fresh", "Fruit")]), ("b@x.com", "BUYER", [("Fresh", "Fruit")])):
        env.repo.set_assignments(env.repo.create_user(email, email[:3], role, hash_password("correct-horse-battery")), cats)
    c = TestClient(create_app(env.s, env.e, env.tp))
    h = lambda e: {"Authorization": "Bearer " + c.post("/auth/login", json={"email": e, "password": "correct-horse-battery"}).json()["access_token"]}   # noqa: E731
    assert c.get("/insights", headers=h("b@x.com")).status_code == 403
    body = c.get("/insights", headers=h("kim@x.com")).json()
    assert [p["name"] for p in body["promotions"]] == ["Dud"] and set(body["response"]) == {"Fresh"}            # Dairy is outside kim's scope
    assert body["response"]["Fresh"]["status"] == "INSUFFICIENT_DATA"
    q = {"internal_sku": "APPLE-1", "channel_name": "SHOPA", "price": 9000}
    r = c.get("/scenarios", params=q, headers=h("kim@x.com"))
    assert r.status_code == 200 and r.json()["status"] == "INSUFFICIENT_DATA"
    assert c.get("/scenarios", params={**q, "internal_sku": "NOPE"}, headers=h("kim@x.com")).status_code == 404
    assert c.get("/scenarios", params={**q, "price": 0}, headers=h("kim@x.com")).status_code == 422
    assert c.get("/scenarios", params=q, headers=h("b@x.com")).status_code == 403
