"""Auth, RBAC masking, scope, watchlists, workflow and admin through the real FastAPI app (in-process)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from marketflow.db import init_sqlite, make_engine
from marketflow.decision import service
from marketflow.erp import import_erp
from marketflow.repository import SqlRepository
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.web.app import COST_KEYS, create_app
from marketflow.web.repo import AppRepository
from marketflow.web.security import hash_password, make_token, read_token, verify_password

T0 = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
SECRET = "s" * 48
PW = "correct-horse-battery"
CSV = """internal_sku,item_name,channel_name,purchase_cost,fixed_cost,logistics_cost,channel_fee_rate,current_selling_price,stock_qty,daily_sales_qty,remaining_days,pack_std_units
APPLE-1,Apple,SHOPA,5000,500,1000,11.5,10000,100,10,,12
ONION-1,Onion,SHOPA,2000,300,700,11.5,5000,300,100,2,10
"""


def keys_anywhere(obj, wanted):
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in wanted:
                found.add(k)
            found |= keys_anywhere(v, wanted)
    elif isinstance(obj, list):
        for v in obj:
            found |= keys_anywhere(v, wanted)
    return found


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    """Production uses n=2**14; tests use a tiny work factor (verify reads params from each stored hash)."""
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)


@pytest.fixture()
def world(tmp_path):
    tp = FixedTimeProvider(T0)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}", jwt_secret=SECRET)
    e = make_engine(s.database_url)
    init_sqlite(e)
    repo = AppRepository(e)
    f = tmp_path / "inv.csv"; f.write_text(CSV, encoding="utf-8")
    assert import_erp(f, repo, s, tp).loaded == 2
    SqlRepository(e).insert_retail([{
        "scraped_at": T0.replace(tzinfo=None) - timedelta(hours=1), "platform": "SHOPA", "std_item_id": 1,
        "match_confidence": Decimal("0.9"), "channel_item_code": "X1", "product_title": "사과 1.2kg", "original_price": 9000,
        "sale_price": 8400, "normalized_unit_price": Decimal("700"), "is_out_of_stock": 0, "badge_info": None,
        "extraction_source": "SELECTOR", "region_code": "SEOUL"}])
    with e.begin() as c:
        c.execute(text("INSERT INTO ext_public_benchmarks (recorded_date,source_type,std_item_id,raw_item_name,avg_price,min_price,max_price,normalized_price,extra_metrics) "
                       "VALUES ('2026-11-03','KAMIS',1,'사과/후지',52300,52300,52300,523,'{}')"))
    users = {}
    for key, role, cats in [("admin", "ADMIN", []), ("md_fruit", "MD", [("Fresh", "Fruit")]), ("md_veg", "MD", [("Fresh", "Vegetable")]),
                            ("buyer", "BUYER", [("Fresh", "Fruit")]), ("nobody", "MD", [])]:
        uid = repo.create_user(f"{key}@x.com", key, role, hash_password(PW))
        repo.set_assignments(uid, cats)
        users[key] = uid
    service.run_decisions(repo, s, tp)
    client = TestClient(create_app(s, e, tp))
    return type("W", (), dict(client=client, repo=repo, tp=tp, s=s, e=e, users=users))


def login(w, who):
    r = w.client.post("/auth/login", json={"email": f"{who}@x.com", "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


# ---- primitives ---------------------------------------------------------------------------------
def test_password_hashing_and_token_primitives():
    h = hash_password(PW)
    assert verify_password(PW, h) and not verify_password("wrong", h) and h != hash_password(PW)
    assert not verify_password(PW, "garbage") and not verify_password(PW, "bcrypt$1$2$3$aa$bb")
    now = T0
    tok = make_token(SECRET, 7, now, 60)
    assert read_token(SECRET, tok, now) == 7 and read_token(SECRET, tok, now + timedelta(minutes=61)) is None
    assert read_token("x" * 48, tok, now) is None and read_token(SECRET, tok + "x", now) is None


def test_refuses_to_start_without_a_strong_secret(tmp_path):
    for bad in ("", "short"):
        with pytest.raises(RuntimeError, match="jwt_secret"):
            create_app(Settings(jwt_secret=bad, database_url=f"sqlite:///{tmp_path / 'x.db'}"))


# ---- authentication -------------------------------------------------------------------------------
def test_login_ok_and_generic_failure(world):
    r = world.client.post("/auth/login", json={"email": "ADMIN@x.com", "password": PW})     # email is case-insensitive
    assert r.status_code == 200 and r.json()["user"]["role"] == "ADMIN" and password_not_echoed(r)
    bad_pw = world.client.post("/auth/login", json={"email": "admin@x.com", "password": "nope"})
    no_user = world.client.post("/auth/login", json={"email": "ghost@x.com", "password": "nope"})
    assert bad_pw.status_code == no_user.status_code == 401 and bad_pw.json() == no_user.json()   # no user enumeration


def password_not_echoed(r):
    return PW not in r.text and "password_hash" not in r.text


def test_protected_routes_need_a_valid_token(world):
    c = world.client
    for path in ("/matrix", "/recommendations", "/watchlist", "/preferences", "/auth/me", "/admin/users", "/admin/audit"):
        assert c.get(path).status_code == 401, path
    assert c.get("/matrix", headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401
    assert c.get("/matrix", headers={"Authorization": "Basic Zm9vOmJhcg=="}).status_code == 401
    assert c.get("/health").status_code == 200


def test_expired_token_rejected(world):
    h = login(world, "admin")
    assert world.client.get("/auth/me", headers=h).status_code == 200
    world.tp.set(T0 + timedelta(minutes=61))
    assert world.client.get("/auth/me", headers=h).status_code == 401


def test_login_throttle_after_five_failures_then_recovers(world):
    for _ in range(5):
        assert world.client.post("/auth/login", json={"email": "admin@x.com", "password": "bad"}).status_code == 401
    r = world.client.post("/auth/login", json={"email": "admin@x.com", "password": PW})
    assert r.status_code == 429 and "Retry-After" in r.headers                   # even the right password is refused
    world.tp.set(T0 + timedelta(minutes=16))
    assert world.client.post("/auth/login", json={"email": "admin@x.com", "password": PW}).status_code == 200


def test_deactivation_and_role_change_apply_immediately(world):
    h = login(world, "md_fruit")
    assert world.client.get("/auth/me", headers=h).json()["role"] == "MD"
    world.repo.set_role(world.users["md_fruit"], "BUYER")
    assert world.client.get("/auth/me", headers=h).json()["role"] == "BUYER"      # no stale role in the token
    world.repo.set_active(world.users["md_fruit"], False)
    assert world.client.get("/auth/me", headers=h).status_code == 401
    assert world.client.post("/auth/login", json={"email": "md_fruit@x.com", "password": PW}).status_code == 401


# ---- scope & masking ---------------------------------------------------------------------------------------
def matrix_items(w, who, query=""):
    r = w.client.get("/matrix" + query, headers=login(w, who))
    assert r.status_code == 200
    return r.json()["rows"]


def test_matrix_scoped_by_category_assignment(world):
    assert {r["item_name"] for r in matrix_items(world, "admin")} == {"Apple", "Onion"}
    assert [r["item_name"] for r in matrix_items(world, "md_fruit")] == ["Apple"]
    assert [r["item_name"] for r in matrix_items(world, "md_veg")] == ["Onion"]
    assert matrix_items(world, "nobody") == []                                  # no assignment -> sees nothing


def test_three_way_matrix_content(world):
    apple = matrix_items(world, "md_fruit")[0]
    assert apple["public_benchmark"] == {"recorded_date": "2026-11-03", "unit": "100g", "normalized_price": "523.00"}
    assert apple["internal"]["current_selling_price"] == 10000 and apple["internal"]["margin_rate"] == "15.85"
    assert apple["competitors"] == {"listings": 1, "out_of_stock": 0, "lowest_unit_price": "700.00", "lowest_pack_price": "8400.00"}
    onion = matrix_items(world, "md_veg")[0]
    assert onion["public_benchmark"] is None and onion["competitors"]["listings"] == 0 and onion["remaining_days"] == 2


def test_buyer_never_receives_cost_or_margin_fields(world):
    h = login(world, "buyer")
    for path in ("/matrix", "/recommendations"):
        body = world.client.get(path, headers=h).json()
        assert keys_anywhere(body, COST_KEYS | {"LOW_MARGIN"}) == set(), path
    row = world.client.get("/matrix", headers=h).json()["rows"][0]
    assert row["internal"] == {"current_selling_price": 10000} and row["competitors"]["lowest_pack_price"] == "8400.00"
    assert keys_anywhere(world.client.get("/matrix", headers=login(world, "md_fruit")).json(), COST_KEYS) == COST_KEYS


def test_watchlist_filter_and_scope_enforced(world):
    h = login(world, "md_fruit")
    assert world.client.put("/watchlist", headers=h, json={"std_item_ids": [1]}).json() == {"std_item_ids": [1]}
    assert world.client.get("/matrix?watchlist=true", headers=h).json()["rows"][0]["item_name"] == "Apple"
    assert world.client.put("/watchlist", headers=h, json={"std_item_ids": []}).status_code == 200
    assert world.client.get("/matrix?watchlist=true", headers=h).json()["rows"] == []
    assert world.client.put("/watchlist", headers=h, json={"std_item_ids": [3]}).status_code == 404   # Onion: outside scope
    assert world.client.put("/watchlist", headers=h, json={"std_item_ids": [999]}).status_code == 404


def test_preferences_roundtrip_and_validation(world):
    h = login(world, "md_fruit")
    assert world.client.get("/preferences", headers=h).json()["alert_margin_bep"] == "10.00"
    r = world.client.put("/preferences", headers=h, json={"alert_margin_bep": 12.5, "ui_layout": {"cols": ["a", "b"]}})
    assert r.json() == {"alert_margin_bep": "12.50", "ui_layout": {"cols": ["a", "b"]}}
    assert world.client.put("/preferences", headers=h, json={"alert_margin_bep": 150}).status_code == 422
    assert world.client.get("/preferences", headers=login(world, "md_veg")).json()["alert_margin_bep"] == "10.00"   # per user


# ---- recommendation workflow ------------------------------------------------------------------------------------------
def rec_id(w, item):
    return next(r["rec_id"] for r in w.client.get("/recommendations", headers=login(w, "admin")).json()["recommendations"] if r["item_name"] == item)


def test_recommendations_are_scoped(world):
    assert {r["item_name"] for r in world.client.get("/recommendations", headers=login(world, "md_fruit")).json()["recommendations"]} == {"Apple"}
    assert world.client.get("/recommendations?status=BOGUS", headers=login(world, "admin")).status_code == 422


def test_workflow_permissions_and_lifecycle(world):
    apple = rec_id(world, "Apple")
    post = lambda who, action, rid=apple: world.client.post(f"/recommendations/{rid}/{action}", headers=login(world, who))
    assert post("buyer", "approve").status_code == 403                           # BUYER in scope but may not decide
    assert post("md_veg", "approve").status_code == 404                          # other category: looks nonexistent
    assert post("md_fruit", "apply").status_code == 409                          # must approve first
    assert post("md_fruit", "approve").json() == {"rec_id": apple, "status": "APPROVED"}
    assert post("md_fruit", "apply").json()["status"] == "APPLIED"
    with world.e.connect() as c:
        assert c.execute(text("SELECT current_selling_price FROM int_operations_inventory WHERE internal_sku='APPLE-1'")).scalar() == 9000
    assert post("admin", "revert").json()["status"] == "REVERTED"
    assert post("admin", "explode").status_code == 422
    assert post("admin", "approve", 9999).status_code == 404
    actors = [(a["actor"], a["action"]) for a in world.repo.audit_entries()]
    assert actors == [("md_fruit@x.com", "RECOMMENDATION_APPROVED"), ("md_fruit@x.com", "PRICE_APPLIED"), ("admin@x.com", "PRICE_REVERTED")]


# ---- admin -----------------------------------------------------------------------------------------------------------------------
def test_admin_endpoints_are_admin_only(world):
    for who in ("md_fruit", "buyer"):
        h = login(world, who)
        assert world.client.get("/admin/users", headers=h).status_code == 403
        assert world.client.get("/admin/audit", headers=h).status_code == 403
        assert world.client.post("/admin/users", headers=h, json={"email": "a@b.co", "name": "n", "role": "ADMIN", "password": PW}).status_code == 403


def test_admin_creates_user_who_can_log_in_with_scope(world):
    h = login(world, "admin")
    r = world.client.post("/admin/users", headers=h, json={"email": "new@x.com", "name": "New", "role": "MD", "password": "a-long-password"})
    assert r.status_code == 201
    uid = r.json()["user_id"]
    assert world.client.put(f"/admin/users/{uid}/assignments", headers=h, json=[{"category_l": "Fresh", "category_m": "Vegetable"}]).status_code == 200
    tok = world.client.post("/auth/login", json={"email": "new@x.com", "password": "a-long-password"}).json()["access_token"]
    rows = world.client.get("/matrix", headers={"Authorization": "Bearer " + tok}).json()["rows"]
    assert [r["item_name"] for r in rows] == ["Onion"]
    assert password_not_echoed(world.client.get("/admin/users", headers=h))
    assert [e["action"] for e in world.client.get("/admin/audit", headers=h).json()["entries"]] == ["USER_CREATED", "ASSIGNMENTS_SET"]


def test_admin_validation_and_self_protection(world):
    h = login(world, "admin")
    body = {"email": "dup@x.com", "name": "D", "role": "MD", "password": "a-long-password"}
    assert world.client.post("/admin/users", headers=h, json=body).status_code == 201
    assert world.client.post("/admin/users", headers=h, json=body).status_code == 409
    assert world.client.post("/admin/users", headers=h, json={**body, "email": "w@x.com", "password": "short"}).status_code == 422
    assert world.client.post("/admin/users", headers=h, json={**body, "email": "not-an-email"}).status_code == 422
    assert world.client.post("/admin/users", headers=h, json={**body, "email": "r@x.com", "role": "ROOT"}).status_code == 422
    me = world.users["admin"]
    assert world.client.patch(f"/admin/users/{me}", headers=h, json={"active": False}).status_code == 409
    assert world.client.patch(f"/admin/users/{me}", headers=h, json={"role": "MD"}).status_code == 409
    assert world.client.patch("/admin/users/999", headers=h, json={"active": False}).status_code == 404


# ---- static & headers -----------------------------------------------------------------------------------------------------
def test_dashboard_and_security_headers(world):
    r = world.client.get("/")
    assert r.status_code == 200 and "MarketFlow" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    assert world.client.get("/static/app.js").status_code == 200 and world.client.get("/openapi.json").status_code == 404
    assert "<script>" not in r.text.replace('<script src="/static/app.js"></script>', "")   # no inline script (CSP-safe)
