"""Operational paths on real MySQL: ERP import -> decide -> approve/apply -> export, review queue, alerts, API login.
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
from marketflow.erp import import_erp
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
CLEAN = ["audit_log", "alert_log", "review_queue", "selector_candidates", "price_recommendations", "user_preferences",
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
    assert import_erp(f, repo, s, tp).loaded == 2
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
