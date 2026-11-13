"""Revised specification, P1 workflow side (SRS F-08): price-change drafts with higher approval below the defense line and
an evidence snapshot, point-in-time snapshots with two clocks."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from marketflow import cli
from marketflow.db import init_sqlite, make_engine
from marketflow.decision import service
from marketflow.decision.service import Actor, WorkflowError
from marketflow.internal_data import import_internal
from marketflow.repository import SqlRepository
from marketflow.settings import Settings
from marketflow.snapshot import snapshot
from marketflow.time_provider import FixedTimeProvider
from marketflow.web.app import create_app
from marketflow.web.repo import AppRepository
from marketflow.web.security import hash_password

T0 = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
NOW = T0.replace(tzinfo=None)
CSV = """internal_sku,item_name,channel_name,purchase_cost,fixed_cost,logistics_cost,channel_fee_rate,current_selling_price,stock_qty,daily_sales_qty,remaining_days,pack_std_units
APPLE-1,Apple,SHOPA,5000,500,1000,11.5,10000,100,10,,12
ONION-1,Onion,SHOPA,2000,300,700,11.5,5000,300,10,,10
"""
KIM, LEE, ROOT, BUYER = Actor("kim@x.com", "MD"), Actor("lee@x.com", "MD"), Actor("root@x.com", "ADMIN"), Actor("b@x.com", "BUYER")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)
    tp = FixedTimeProvider(T0)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'w.db'}", jwt_secret="j" * 48, inbox_dir=str(tmp_path / "inbox"), solo_mode=False)
    e = make_engine(s.database_url)
    init_sqlite(e)
    repo = AppRepository(e)
    f = tmp_path / "inv.csv"; f.write_text(CSV, encoding="utf-8")
    assert import_internal(f, repo, s, tp).loaded == 2
    return type("E", (), dict(tp=tp, s=s, e=e, repo=repo, tmp=tmp_path))


# ---- drafts ---------------------------------------------------------------------------------------------------------
def floor_of(env, sku="APPLE-1"):
    return env.repo.inventory_latest(sku, "SHOPA")["min_price_floor"]


def test_a_draft_above_the_defense_line_needs_one_other_approver(env):
    rid = service.create_draft(env.repo, env.s, env.tp, KIM, "APPLE-1", "SHOPA", 9500, "match the market")
    rec = env.repo.get_recommendation(rid)
    assert rec["status"] == "PENDING" and rec["reason_code"] == "MANUAL_DRAFT" and rec["suggested_price"] == 9500 and rec["current_price"] == 10000
    f = rec["flags"]
    assert f["author"] == "kim@x.com" and f["REQUIRES_ADMIN"] is False and f["BELOW_DEFENSE_LINE"] is False
    ev = f["evidence"]                                               # frozen at writing time
    assert ev["note"] == "match the market" and ev["current_price"] == 10000 and ev["proposed_price"] == 9500 and ev["floor"] == floor_of(env)
    assert ev["as_of"]["inventory_date"] == "2026-11-03" and f["metrics"]["match_simulation"]["bep_status"] == "OK"
    with pytest.raises(PermissionError, match="author"):
        service.approve(env.repo, env.tp, rid, KIM, separate_duties=True)                  # no self-approval
    service.approve(env.repo, env.tp, rid, LEE, separate_duties=True)
    service.apply(env.repo, env.tp, rid, LEE)
    assert env.repo.live_price("APPLE-1", "SHOPA") == 9500
    assert {"DRAFT_CREATED", "RECOMMENDATION_APPROVED", "PRICE_APPLIED"} <= {a["action"] for a in env.repo.audit_entries()}


def test_a_draft_below_the_defense_line_needs_an_administrator(env):
    low = floor_of(env) - 100
    rid = service.create_draft(env.repo, env.s, env.tp, KIM, "APPLE-1", "SHOPA", low)
    flags = env.repo.get_recommendation(rid)["flags"]
    assert flags["BELOW_DEFENSE_LINE"] is True and flags["REQUIRES_ADMIN"] is True
    with pytest.raises(PermissionError, match="administrator"):
        service.approve(env.repo, env.tp, rid, LEE, separate_duties=True)    # another MD is not enough
    service.approve(env.repo, env.tp, rid, ROOT, separate_duties=True)
    assert env.repo.get_recommendation(rid)["status"] == "APPROVED"


def test_draft_validation_and_roles(env):
    with pytest.raises(PermissionError):
        service.create_draft(env.repo, env.s, env.tp, BUYER, "APPLE-1", "SHOPA", 9000)
    with pytest.raises(WorkflowError, match="no inventory"):
        service.create_draft(env.repo, env.s, env.tp, KIM, "NOPE", "SHOPA", 9000)
    with pytest.raises(WorkflowError, match="equals"):
        service.create_draft(env.repo, env.s, env.tp, KIM, "APPLE-1", "SHOPA", 10000)
    with pytest.raises(WorkflowError, match="positive"):
        service.create_draft(env.repo, env.s, env.tp, KIM, "APPLE-1", "SHOPA", 0)


def test_a_negative_margin_price_above_a_manual_floor_still_needs_an_admin(env):
    from sqlalchemy import text
    with env.e.begin() as c:
        c.execute(text("UPDATE int_operations_inventory SET min_price_floor = 1 WHERE internal_sku='APPLE-1'"))   # a careless floor
    rid = service.create_draft(env.repo, env.s, env.tp, KIM, "APPLE-1", "SHOPA", 7000)                  # loses money: cost 6500 + fee + VAT
    assert env.repo.get_recommendation(rid)["flags"]["REQUIRES_ADMIN"] is True


@pytest.fixture()
def api(env):
    for key, role, cats in [("admin", "ADMIN", []), ("kim", "MD", [("Fresh", "Fruit")]), ("lee", "MD", [("Fresh", "Fruit")]),
                            ("veg", "MD", [("Fresh", "Vegetable")]), ("buyer", "BUYER", [("Fresh", "Fruit")])]:
        env.repo.set_assignments(env.repo.create_user(f"{key}@x.com", key, role, hash_password("correct-horse-battery")), cats)
    client = TestClient(create_app(env.s, env.e, env.tp))
    hdr = lambda who: {"Authorization": "Bearer " + client.post("/auth/login", json={"email": f"{who}@x.com", "password": "correct-horse-battery"}).json()["access_token"]}   # noqa: E731
    return type("A", (), dict(c=client, h=hdr, env=env))


def test_draft_api_roles_scope_and_masking(api):
    body = {"internal_sku": "APPLE-1", "channel_name": "SHOPA", "price": 9500, "note": "n"}
    assert api.c.post("/recommendations/draft", json=body, headers=api.h("buyer")).status_code == 403
    assert api.c.post("/recommendations/draft", json=body, headers=api.h("veg")).status_code == 404         # outside the caller's categories
    assert api.c.post("/recommendations/draft", json={**body, "price": 10000}, headers=api.h("kim")).status_code == 409
    assert api.c.post("/recommendations/draft", json={**body, "price": -5}, headers=api.h("kim")).status_code == 422
    r = api.c.post("/recommendations/draft", json=body, headers=api.h("kim"))
    assert r.status_code == 201 and r.json()["requires_admin"] is False
    rid = r.json()["rec_id"]
    assert api.c.post(f"/recommendations/{rid}/approve", headers=api.h("kim")).status_code == 403          # author
    assert api.c.post(f"/recommendations/{rid}/approve", headers=api.h("lee")).status_code == 200
    low = api.c.post("/recommendations/draft", json={**body, "price": floor_of(api.env) - 50}, headers=api.h("kim")).json()
    assert low["requires_admin"] is True
    assert api.c.post(f"/recommendations/{low['rec_id']}/approve", headers=api.h("lee")).status_code == 403
    assert api.c.post(f"/recommendations/{low['rec_id']}/approve", headers=api.h("admin")).status_code == 200
    buyer_view = api.c.get("/recommendations", headers=api.h("buyer")).json()["recommendations"]
    assert all("evidence" not in x["flags"] and "match_simulation" not in x["flags"].get("metrics", {}) for x in buyer_view)
    md_view = api.c.get("/recommendations", headers=api.h("kim")).json()["recommendations"]
    assert any("evidence" in x["flags"] for x in md_view)


# ---- two clocks --------------------------------------------------------------------------------------------------------
def test_a_retroactive_cost_correction_does_not_change_what_was_known_before(env):
    day = date(2026, 11, 3)
    env.tp.set(T0 + timedelta(days=2))                                # on Nov 5 finance corrects Nov 3's apple cost 5000 -> 6000
    f = env.tmp / "fix.csv"; f.write_text(CSV.replace("APPLE-1,Apple,SHOPA,5000", "APPLE-1,Apple,SHOPA,6000"), encoding="utf-8")
    assert import_internal(f, env.repo, env.s, env.tp, day).loaded == 2
    before = {r["internal_sku"]: r for r in snapshot(env.repo, env.s, day, NOW + timedelta(hours=1))}      # Nov 3, 02:00 UTC
    now = {r["internal_sku"]: r for r in snapshot(env.repo, env.s, day)}
    assert before["APPLE-1"]["purchase_cost"] == "5000.00" and now["APPLE-1"]["purchase_cost"] == "6000.00"
    assert Decimal(before["APPLE-1"]["margin_rate"]) > Decimal(now["APPLE-1"]["margin_rate"])
    assert before["APPLE-1"]["known_since"].startswith("2026-11-03") and now["APPLE-1"]["known_since"].startswith("2026-11-05")
    assert snapshot(env.repo, env.s, day, NOW - timedelta(days=1)) == []                                  # nothing was known yet
    assert {r["purchase_cost"] for r in snapshot(env.repo, env.s, day, NOW + timedelta(days=3)) if r["internal_sku"] == "APPLE-1"} == {"6000.00"}
    assert env.repo.live_price("APPLE-1", "SHOPA") == 10000          # the working ledger still holds one row per SKU/day


def test_snapshot_api_cli_scope_and_audit(api):
    env = api.env
    r = api.c.get("/snapshot?date=2026-11-03", headers=api.h("kim")).json()
    assert [x["internal_sku"] for x in r["rows"]] == ["APPLE-1"]                                         # onion is a vegetable: out of scope
    assert len(api.c.get("/snapshot?date=2026-11-03&known_at=2026-11-03T02:00:00", headers=api.h("admin")).json()["rows"]) == 2
    assert api.c.get("/snapshot?date=2026-11-03", headers=api.h("buyer")).status_code == 403
    assert api.c.get("/snapshot?date=nope", headers=api.h("admin")).status_code == 422
    assert any(a["action"] == "SNAPSHOT_READ" for a in env.repo.audit_entries())
    import io
    out = io.StringIO()
    assert cli.main(["snapshot", "2026-11-03", "--known-at", "2026-11-03T02:00:00"], settings=env.s, tp=env.tp, out=out) == 0
    assert "APPLE-1/SHOPA" in out.getvalue() and "known since 2026-11-03" in out.getvalue()


def test_solo_mode_lets_one_person_propose_approve_and_apply_even_below_the_line(env):
    """The default: a single operator analyses and decides alone. The proposal is still flagged and audited."""
    low = floor_of(env) - 100
    rid = service.create_draft(env.repo, env.s, env.tp, KIM, "APPLE-1", "SHOPA", low, "clearance")
    assert env.repo.get_recommendation(rid)["flags"]["BELOW_DEFENSE_LINE"] is True
    service.approve(env.repo, env.tp, rid, KIM)                                # same person, MD role, below the line: allowed
    service.apply(env.repo, env.tp, rid, KIM)
    assert env.repo.live_price("APPLE-1", "SHOPA") == low
    assert {"DRAFT_CREATED", "RECOMMENDATION_APPROVED", "PRICE_APPLIED"} <= {a["action"] for a in env.repo.audit_entries()}


def test_solo_setting_is_on_by_default_and_overridable_from_the_environment(tmp_path):
    from marketflow.settings import load_settings
    assert Settings().solo_mode is True
    assert load_settings(tmp_path / "none.toml", env={"SOLO_MODE": "false"}).solo_mode is False


def test_api_follows_the_setting(env):
    for key, role in (("kim", "MD"),):
        env.repo.set_assignments(env.repo.create_user(f"{key}@x.com", key, role, hash_password("correct-horse-battery")), [("Fresh", "Fruit")])
    from dataclasses import replace
    solo = TestClient(create_app(replace(env.s, solo_mode=True), env.e, env.tp))
    h = {"Authorization": "Bearer " + solo.post("/auth/login", json={"email": "kim@x.com", "password": "correct-horse-battery"}).json()["access_token"]}
    rid = solo.post("/recommendations/draft", json={"internal_sku": "APPLE-1", "channel_name": "SHOPA", "price": 9000}, headers=h).json()["rec_id"]
    assert solo.post(f"/recommendations/{rid}/approve", headers=h).status_code == 200
