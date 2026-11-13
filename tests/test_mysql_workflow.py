"""Operational paths on real MySQL: internal-data import -> decide -> approve/apply -> export, review queue, alerts, API login.
Needs MARKETFLOW_TEST_DB_URL (database initialised from db/init/*.sql)."""
import os
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from marketflow.db import make_engine
from marketflow.decision import service
from marketflow.decision.service import Actor
from marketflow.internal_data import import_internal
from marketflow.notify import Alert
from marketflow.ops_repo import OpsRepository, Repo, ReviewItem
from marketflow.repository import SqlRepository
from marketflow.settings import Settings
from marketflow.web.app import create_app
from marketflow.web.security import hash_password
from marketflow.web.repo import AppRepository
from test_workflow_e2e import CSV, MD, T0, _user
from marketflow.time_provider import FixedTimeProvider

URL = os.getenv("MARKETFLOW_TEST_DB_URL")
pytestmark = [pytest.mark.mysql, pytest.mark.skipif(not URL, reason="MARKETFLOW_TEST_DB_URL not set")]
CLEAN = ["product_channel_map", "audit_log", "alert_log", "review_queue", "selector_candidates", "price_recommendations", "user_preferences",
         "user_category_assignments", "user_watchlists", "users", "ext_retail_prices", "int_operations_inventory"]


@pytest.fixture()
def env(tmp_path):
    e = make_engine(URL)
    with e.begin() as c:
        for t in CLEAN:
            c.execute(text(f"DELETE FROM {t}"))
    s, tp = Settings(database_url=URL, jwt_secret="j" * 40), FixedTimeProvider(T0)
    f = tmp_path / "inv.csv"; f.write_text(CSV, encoding="utf-8")
    repo = OpsRepository(e)
    assert import_internal(f, repo, s, tp).loaded == 2
    SqlRepository(e).insert_retail([{
        "scraped_at": T0.replace(tzinfo=None) - timedelta(hours=1), "platform": "SHOPA", "std_item_id": 1,
        "match_confidence": Decimal("0.9"), "channel_item_code": "X1", "product_title": "사과 1.2kg", "original_price": 9000,
        "sale_price": 8400, "normalized_unit_price": Decimal("700"), "is_out_of_stock": 0, "badge_info": None,
        "extraction_source": "SELECTOR", "region_code": "SEOUL"}])
    return repo, tp, e, s


def test_decide_approve_apply_export_revert(env):
    repo, tp, e, s = env
    made = {r.internal_sku: r for _, r in service.run_decisions(repo, s, tp)}
    assert made["APPLE-1"].reason_code == "COMPETITIVE_MATCH" and made["APPLE-1"].suggested_price == 9000
    assert service.run_decisions(repo, s, tp) == []                      # de-duplicated on rerun
    rid = next(r["rec_id"] for r in repo.list_recommendations() if r["std_item_id"] == 1)
    service.approve(repo, tp, rid, MD); service.apply(repo, tp, rid, MD)
    rows = service.price_changes(repo, T0.replace(tzinfo=None).replace(hour=0))
    assert [(r["internal_sku"], r["new_price"], r["applied_by"]) for r in rows] == [("APPLE-1", 9000, "kim")]
    service.revert(repo, tp, rid, MD)
    assert {"PRICE_APPLIED", "PRICE_REVERTED"} <= {x["action"] for x in repo.audit_entries()}
    assert repo.get_recommendation(rid)["status"] == "REVERTED"


def test_alert_threshold_query(env):
    repo, _, e, _ = env
    cat = repo.item_exists(1)["category_l"]
    assert repo.alert_threshold(cat) is None
    _user(e, "md1@x", "MD", 12, [cat]); _user(e, "md2@x", "MD", 20, ["Other"]); _user(e, "b@x", "BUYER", 90, [cat])
    _user(e, "gone@x", "MD", 80, [cat], active=0)
    assert repo.alert_threshold(cat) == Decimal("12")
    _user(e, "admin@x", "ADMIN", 15)
    assert repo.alert_threshold(cat) == Decimal("15")


def test_review_queue_and_alert_log(env):
    _, tp, e, s = env
    repo = Repo(e)
    now = T0.replace(tzinfo=None)
    assert repo.add_review([ReviewItem("UNMATCHED_TITLE", "SHOPA", "수입 두리안 1kg", "no match")], now) == 1
    assert repo.add_review([ReviewItem("UNMATCHED_TITLE", "SHOPA", "수입 두리안 1kg", "no match")], now) == 0   # de-duplicated
    items = repo.list_review()
    assert len(items) == 1 and items[0]["item_key"] == "수입 두리안 1kg"
    assert repo.resolve_review(items[0]["review_id"], "kim", now) and repo.list_review() == []
    repo.log_alert("LOW_MARGIN:A/B", "SENT", 1, None, "msg 한글", now)
    assert repo.recently_sent("LOW_MARGIN:A/B", now - timedelta(hours=6))


def test_api_login_matrix_and_recommendations(env):
    repo, tp, e, s = env
    service.run_decisions(repo, s, tp)
    ar = AppRepository(e)
    uid = ar.create_user("md@x.com", "Kim", "MD", hash_password("pw-long-enough-1"))
    ar.set_assignments(uid, [(repo.item_exists(1)["category_l"], "")])
    c = TestClient(create_app(s, e, tp))
    tok = c.post("/auth/login", json={"email": "md@x.com", "password": "pw-long-enough-1"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert c.get("/auth/me", headers=h).json()["role"] == "MD"
    rows = c.get("/matrix", headers=h).json()["rows"]
    assert len(rows) == 2
    apple = next(r for r in rows if r["item_name"] == "Apple")            # history queries (IN list + GROUP BY) on real MySQL
    assert apple["trend"]["ours"] == [["2026-11-03", "10000"]]
    assert apple["trend"]["competitor"] == [["2026-11-03", "8400.00"]]    # 700 per unit x 12-unit pack
    assert apple["internal"]["margin_trend"] == [["2026-11-03", apple["internal"]["margin_rate"]]]
    assert apple["trend"]["wholesale"] == []                              # no benchmark loaded for this fixture
    recs = c.get("/recommendations", headers=h).json()["recommendations"]
    assert recs and all(isinstance(r["flags"], dict) for r in recs)
    assert c.post(f"/recommendations/{recs[0]['rec_id']}/approve", headers=h).status_code == 200


def test_crawler_settings_runs_and_mi_upserts(env):
    """The crawler/MI tables and their MySQL-specific SQL (JSON columns, ENUMs, ON DUPLICATE KEY ... GREATEST)."""
    from datetime import date
    from marketflow.crawlers.store import CrawlerStore
    from marketflow.mi.store import MiStore
    _, _, e, _ = env
    now = T0.replace(tzinfo=None)
    with e.begin() as c:
        for t in ("crawler_runs", "crawler_settings", "mi_observations"):
            c.execute(text(f"DELETE FROM {t}"))
    st = CrawlerStore(e)
    assert st.get_settings("news")["enabled"] is True
    st.save_settings("news", False, ["07:00", "18:30"], {"feeds": ["https://n.example/rss"], "lookback_days": 7}, "kim", now)
    st.save_settings("news", False, ["07:00"], {"feeds": []}, "kim", now)                       # second save updates, not inserts
    got = st.get_settings("news")
    assert got["enabled"] is False and got["times"] == ["07:00"] and got["config"] == {"feeds": []}
    st.set_last_slot("news", now)
    assert st.get_settings("news")["last_slot"] == now
    q = st.queue_run("news", "kim", now)
    assert q and st.queue_run("news", "lee", now) is None
    assert st.start_run("news", "MANUAL", "kim", now, q) == q and st.start_run("news", "SCHEDULED", "scheduler", now) is None
    st.finish_run(q, "OK", 3, "done", now)
    assert st.runs("news")[0]["status"] == "OK" and st.last_success("news")
    rid = st.start_run("news", "SCHEDULED", "scheduler", now)
    assert rid and st.reap_stale(now + timedelta(hours=3)) == 1 and st.runs("news")[0]["status"] == "FAILED"
    mi = MiStore(e)
    row = {"signal_type": "MEDIA", "period_start": date(2026, 11, 2), "category_l": "Fruit", "keyword": "딸기", "metric": "count", "value": 5, "source": "news"}
    mi.upsert_observations([row])
    mi.upsert_observations([{**row, "value": 2}], keep_max=True)
    assert [r["value"] for r in mi.observations(5)] == [5]
    mi.upsert_observations([{**row, "value": 2}])
    assert [r["value"] for r in mi.observations(5)] == [2]
    assert mi.tracked_keywords(5) == [] and mi.latest_period("MEDIA") == "2026-11-02"


@pytest.mark.unconfirmed
def test_match_confirmation_effective_price_and_disposal_on_mysql(env):
    """New P0 SQL on real MySQL: product_channel_map upserts, confirmed-only snapshots, effective price, disposal column."""
    from marketflow import matching
    repo, tp, e, s = env
    now = T0.replace(tzinfo=None)
    SqlRepository(e).insert_retail([{
        "scraped_at": now - timedelta(minutes=30), "platform": "SHOPB", "std_item_id": 1, "match_confidence": Decimal("0.9"),
        "channel_item_code": "B1", "product_title": "사과 1.2kg", "original_price": 9000, "sale_price": 8400,
        "normalized_unit_price": Decimal("700"), "is_out_of_stock": 0, "badge_info": None, "extraction_source": "SELECTOR",
        "region_code": "SEOUL", "coupon_discount": 400, "shipping_fee": 0, "bonus_qty": Decimal("0"),
        "effective_unit_price": Decimal("666.67")}])
    assert SqlRepository(e).propose_matches([{"platform": "SHOPB", "channel_item_code": "B1", "std_item_id": 1, "match_confidence": Decimal("0.9")}], now) == 1
    assert SqlRepository(e).propose_matches([{"platform": "SHOPB", "channel_item_code": "B1", "std_item_id": 1, "match_confidence": Decimal("0.9")}], now) == 0
    since = now - timedelta(days=1)
    assert repo.competitor_snapshots(1, since, confirmed_only=True) == []
    assert [m["channel_item_code"] for m in repo.list_matches(None, True)] == ["B1"] and repo.list_matches(None, True)[0]["product_title"] == "사과 1.2kg"
    matching.set_match(repo, tp, MD, "SHOPB", "B1", 1, "SIMILAR")
    assert repo.confirmed_listings() == set()
    matching.set_match(repo, tp, MD, "SHOPB", "B1", 1, "SAME")
    snap = repo.competitor_snapshots(1, since, confirmed_only=True)
    assert len(snap) == 1 and snap[0]["normalized_unit_price"] == Decimal("666.67") and snap[0]["listed_unit_price"] == Decimal("700.00")
    assert repo.get_match("SHOPB", "B1")["reviewed_by"] == "kim"
    inv = repo.inventory_on(T0.date())
    assert inv and all(r["disposal_cost"] is None for r in inv)
    matching.remove_match(repo, tp, MD, "SHOPB", "B1")
    assert repo.confirmed_listings() == set()
