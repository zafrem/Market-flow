"""Import -> (scraped competitor data) -> decide -> approve -> apply -> revert, with audit trail and CLI."""
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from marketflow import cli
from marketflow.db import init_sqlite, make_engine
from marketflow.decision import service
from marketflow.decision.service import Actor, WorkflowError
from marketflow.erp import import_erp
from marketflow.ops_repo import OpsRepository
from marketflow.repository import SqlRepository
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider

T0 = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
CSV = """internal_sku,item_name,channel_name,purchase_cost,fixed_cost,logistics_cost,channel_fee_rate,current_selling_price,stock_qty,daily_sales_qty,remaining_days,pack_std_units
APPLE-1,Apple,SHOPA,5000,500,1000,11.5,10000,100,10,,12
ONION-1,Onion,SHOPA,2000,300,700,11.5,5000,300,100,2,10
"""
MD, BUYER = Actor("kim", "MD"), Actor("lee", "BUYER")


@pytest.fixture()
def env(tmp_path):
    tp = FixedTimeProvider(T0)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'w.db'}")
    e = make_engine(s.database_url)
    init_sqlite(e)
    f = tmp_path / "inv.csv"; f.write_text(CSV, encoding="utf-8")
    repo = OpsRepository(e)
    assert import_erp(f, repo, s, tp).loaded == 2
    # competitor sells the apple pack for 8,400 (700 per 100g x 12) - scraped an hour ago
    SqlRepository(e).insert_retail([{
        "scraped_at": T0.replace(tzinfo=None) - timedelta(hours=1), "platform": "SHOPA", "std_item_id": 1,
        "match_confidence": Decimal("0.9"), "channel_item_code": "X1", "product_title": "사과 1.2kg", "original_price": 9000,
        "sale_price": 8400, "normalized_unit_price": Decimal("700"), "is_out_of_stock": 0, "badge_info": None,
        "extraction_source": "SELECTOR", "region_code": "SEOUL"}])
    return repo, tp, e, s, f


def price(e, sku):
    with e.connect() as c:
        return c.execute(text("SELECT current_selling_price FROM int_operations_inventory WHERE internal_sku=:s"), {"s": sku}).scalar()


def test_decide_creates_expected_recommendations(env):
    repo, tp, e, s, _ = env
    made = service.run_decisions(repo, s, tp)
    by_sku = {r.internal_sku: r for _, r in made}
    apple, onion = by_sku["APPLE-1"], by_sku["ONION-1"]
    assert (apple.reason_code, apple.suggested_price, apple.status) == ("COMPETITIVE_MATCH", 9000, "PENDING")  # 8,400 capped at -10%
    assert (onion.reason_code, onion.status, onion.suggested_price) == ("SPOILAGE_RISK", "PENDING", 4500)       # run-rate 3d > D-2? 2<=3
    assert onion.flags["SPOILAGE_RISK_FLAG"] and len(repo.list_recommendations("PENDING")) == 2


def test_decide_is_deduplicated_on_rerun(env):
    repo, tp, _, s, _ = env
    assert len(service.run_decisions(repo, s, tp)) == 2
    assert service.run_decisions(repo, s, tp) == []
    assert len(repo.list_recommendations()) == 2


def test_full_lifecycle_with_audit_and_cooldown(env):
    repo, tp, e, s, _ = env
    rid = next(i for i, r in service.run_decisions(repo, s, tp) if r.internal_sku == "APPLE-1")
    with pytest.raises(PermissionError):
        service.approve(repo, tp, rid, BUYER)
    with pytest.raises(WorkflowError, match="expected"):
        service.apply(repo, tp, rid, MD)                      # cannot apply before approval
    service.approve(repo, tp, rid, MD)
    assert price(e, "APPLE-1") == 10000                       # approval alone changes nothing
    service.apply(repo, tp, rid, MD)
    assert price(e, "APPLE-1") == 9000 and repo.get_recommendation(rid)["status"] == "APPLIED"
    tp.set(T0 + timedelta(hours=1))                           # inside the 6h cool-down: no new apple recommendation
    assert [r.internal_sku for _, r in service.run_decisions(repo, s, tp)] == []
    service.revert(repo, tp, rid, MD)
    assert price(e, "APPLE-1") == 10000 and repo.get_recommendation(rid)["status"] == "REVERTED"
    with pytest.raises(WorkflowError):
        service.revert(repo, tp, rid, MD)                     # cannot revert twice
    assert [a["action"] for a in repo.audit_entries()] == ["RECOMMENDATION_APPROVED", "PRICE_APPLIED", "PRICE_REVERTED"]
    assert [a["actor"] for a in repo.audit_entries()] == ["kim", "kim", "kim"]


def test_reject_and_unknown_id(env):
    repo, tp, _, s, _ = env
    rid = service.run_decisions(repo, s, tp)[0][0]
    service.reject(repo, tp, rid, MD)
    assert repo.get_recommendation(rid)["status"] == "REJECTED"
    with pytest.raises(WorkflowError, match="not found"):
        service.approve(repo, tp, 999, MD)


def test_cooldown_uses_apply_time_and_expires(env):
    repo, tp, e, s, _ = env
    rid = next(i for i, r in service.run_decisions(repo, s, tp) if r.internal_sku == "APPLE-1")
    service.approve(repo, tp, rid, MD); service.apply(repo, tp, rid, MD)
    SqlRepository(e).insert_retail([{
        "scraped_at": T0.replace(tzinfo=None) + timedelta(hours=6), "platform": "SHOPA", "std_item_id": 1,
        "match_confidence": Decimal("0.9"), "channel_item_code": "X1", "product_title": "사과 1.2kg", "original_price": 9000,
        "sale_price": 9120, "normalized_unit_price": Decimal("760"), "is_out_of_stock": 0, "badge_info": None,
        "extraction_source": "SELECTOR", "region_code": "SEOUL"}])
    tp.set(T0 + timedelta(hours=7))
    again = [r for _, r in service.run_decisions(repo, s, tp) if r.internal_sku == "APPLE-1"]
    assert len(again) == 1                                    # cool-down measured from apply time has expired at +7h
    assert (again[0].current_price, again[0].suggested_price, again[0].reason_code) == (9000, 9120, "COMPETITIVE_MATCH")   # 760 x 12


def test_cli_end_to_end(env):
    repo, tp, e, s, f = env
    out = io.StringIO()
    def run(*a):
        out.seek(0); out.truncate()
        code = cli.main(list(a), settings=s, tp=tp, out=out)
        return code, out.getvalue()
    code, txt = run("decide")
    assert code == 0 and "created=2" in txt and "COMPETITIVE_MATCH" in txt and "SPOILAGE_RISK_FLAG" in txt
    code, txt = run("recs", "--status", "PENDING")
    assert "#1 PENDING" in txt or "#2 PENDING" in txt
    assert run("approve", "1", "--as", "lee:BUYER")[0] == 2 and "refused" in out.getvalue()
    assert run("approve", "1", "--as", "kim:MD")[0] == 0
    assert run("apply", "1", "--as", "kim:MD")[0] == 0
    code, txt = run("audit")
    assert "PRICE_APPLIED" in txt
    code, txt = run("import-erp", str(f))
    assert code == 0 and "loaded=2 errors=0" in txt


def _user(e, email, role, thr, cats=(), active=1):
    with e.begin() as c:
        uid = c.execute(text("INSERT INTO users (email,name,role,password_hash,active) VALUES (:e,:e,:r,'x',:a)"),
                        {"e": email, "r": role, "a": active}).lastrowid
        c.execute(text("INSERT INTO user_preferences (user_id, alert_margin_bep) VALUES (:u,:t)"), {"u": uid, "t": thr})
        for cat in cats:
            c.execute(text("INSERT INTO user_category_assignments (user_id,category_l,category_m) VALUES (:u,:c,'')"), {"u": uid, "c": cat})


def test_alert_threshold_is_max_over_scoped_active_md_and_admin(env):
    repo, _, e, _, _ = env
    cat = repo.item_exists(1)["category_l"]
    assert repo.alert_threshold(cat) is None                       # nobody configured -> system default
    _user(e, "md1@x", "MD", 12, [cat]); _user(e, "md2@x", "MD", 20, ["Other"])
    _user(e, "buyer@x", "BUYER", 90, [cat]); _user(e, "gone@x", "MD", 80, [cat], active=0)
    assert repo.alert_threshold(cat) == Decimal("12")              # md2 other category, buyer/inactive ignored
    _user(e, "admin@x", "ADMIN", 15)
    assert repo.alert_threshold(cat) == Decimal("15")


def test_user_threshold_drives_low_margin_flag(env):
    repo, tp, e, s, _ = env
    cat = repo.item_exists(1)["category_l"]
    _user(e, "md@x", "MD", 100, [cat])                             # alert on any margin <= 100%
    made = {r.internal_sku: r for _, r in service.run_decisions(repo, s, tp)}
    assert made["APPLE-1"].flags.get("LOW_MARGIN") is True
