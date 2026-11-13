"""Auth, RBAC masking, scope, watchlists, workflow and admin through the real FastAPI app (in-process)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from marketflow.db import init_sqlite, make_engine
from marketflow.decision import service
from marketflow.internal_data import import_internal
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
    s = Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}", jwt_secret=SECRET, inbox_dir=str(tmp_path / "inbox"))
    e = make_engine(s.database_url)
    init_sqlite(e)
    repo = AppRepository(e)
    f = tmp_path / "inv.csv"; f.write_text(CSV, encoding="utf-8")
    assert import_internal(f, repo, s, tp).loaded == 2
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
    assert apple["competitors"] == {"listings": 1, "unconfirmed": 0, "out_of_stock": 0, "lowest_unit_price": "700.00", "lowest_pack_price": "8400.00"}
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


def test_cannot_remove_the_last_active_admin(world):
    h, aid = login(world, "admin"), world.users["admin"]
    other = world.repo.create_user("admin2@x.com", "a2", "ADMIN", hash_password(PW))
    assert world.client.patch(f"/admin/users/{other}", headers=h, json={"role": "MD"}).status_code == 200    # two admins: fine
    # one admin left now; a second admin (made via DB) tries to deactivate the only other admin, then is itself last
    other2 = world.repo.create_user("admin3@x.com", "a3", "ADMIN", hash_password(PW))
    h3 = login(world, "admin3")
    assert world.client.patch(f"/admin/users/{aid}", headers=h3, json={"active": False}).status_code == 200
    r = world.client.patch(f"/admin/users/{other2}", headers=h3, json={"role": "BUYER"})
    assert r.status_code == 409                                                                               # self-demotion blocked
    assert world.repo.update_user(other2, "BUYER", None) is False                                              # and the repo guard holds
    assert world.repo.user_by_id(other2)["role"] == "ADMIN"


def test_cannot_remove_the_last_active_admin(world):
    h = login(world, "admin")
    second = world.client.post("/admin/users", headers=h, json={"email": "a2@x.com", "name": "A2", "role": "ADMIN", "password": "a-long-password"}).json()["user_id"]
    # the second admin cannot strip the first (only) other admin once self-demotion is the sole path left
    h2 = {"Authorization": "Bearer " + world.client.post("/auth/login", json={"email": "a2@x.com", "password": "a-long-password"}).json()["access_token"]}
    assert world.client.patch(f"/admin/users/{world.users['admin']}", headers=h2, json={"active": False}).status_code == 200   # a2 remains
    assert world.client.patch(f"/admin/users/{second}", headers=h2, json={"role": "MD"}).status_code == 409                    # self
    h3 = world.repo.create_user("a3@x.com", "A3", "ADMIN", hash_password(PW))
    assert world.repo.update_user(second, "MD", None) is True                                                                 # a3 is still an admin
    assert world.repo.update_user(h3, None, False) is False                                                                   # would leave none
    assert world.repo.update_user(h3, "BUYER", None) is False
    assert world.repo.update_user(h3, "ADMIN", True) is True


def test_a_stranger_cannot_lock_out_the_real_user(world):
    attacker = TestClient(world.client.app, client=("203.0.113.9", 4000))
    for _ in range(5):
        assert attacker.post("/auth/login", json={"email": "admin@x.com", "password": "bad"}).status_code == 401
    assert attacker.post("/auth/login", json={"email": "admin@x.com", "password": PW}).status_code == 429      # attacker is stopped
    assert world.client.post("/auth/login", json={"email": "admin@x.com", "password": PW}).status_code == 200  # real user is not


def test_login_throttle_limits_and_memory():
    from datetime import datetime
    from marketflow.web.security import LoginThrottle
    t0, t = datetime(2026, 1, 1), LoginThrottle()
    for i in range(20):                                                    # one client spraying 20 different accounts
        assert not t.blocked(f"u{i}@x", "1.1.1.1", t0)
        t.fail(f"u{i}@x", "1.1.1.1", t0)
    assert t.blocked("fresh@x", "1.1.1.1", t0) and not t.blocked("fresh@x", "2.2.2.2", t0)
    t = LoginThrottle()
    for i in range(30):                                                    # many clients, one account
        t.fail("v@x", f"10.0.0.{i}", t0)
    assert t.blocked("v@x", "99.9.9.9", t0)
    assert not t.blocked("v@x", "99.9.9.9", t0 + LoginThrottle.WINDOW)    # all expire after the window
    t = LoginThrottle(); t.MAX_KEYS = 50
    for i in range(500):
        t.fail(f"r{i}@x", f"c{i}", t0)
    assert len(t._hits) <= 50                                              # bounded under a random-email flood


def test_matrix_rows_carry_a_trend_for_every_role_without_cost_data(world):
    with world.e.begin() as c:                                             # add history: two earlier inventory days + benchmarks + a competitor snapshot
        for day, price in (("2026-11-01", 10400), ("2026-11-02", 10200)):
            c.execute(text("INSERT INTO int_operations_inventory (recorded_date, std_item_id, internal_sku, channel_name, purchase_cost, fixed_cost, "
                           "logistics_cost, channel_fee_rate, promo_cost, vat_inclusive, pack_std_units, current_selling_price, stock_qty, daily_sales_qty, "
                           "min_price_floor, lifecycle_metric) SELECT :d, std_item_id, internal_sku, channel_name, purchase_cost, fixed_cost, logistics_cost, "
                           "channel_fee_rate, promo_cost, vat_inclusive, pack_std_units, :p, stock_qty, daily_sales_qty, min_price_floor, lifecycle_metric "
                           "FROM int_operations_inventory WHERE internal_sku = 'APPLE-1' AND recorded_date = '2026-11-03'"), {"d": day, "p": price})
        c.execute(text("INSERT INTO ext_public_benchmarks (recorded_date,source_type,std_item_id,raw_item_name,avg_price,min_price,max_price,normalized_price,extra_metrics) "
                       "VALUES ('2026-11-01','KAMIS',1,'사과/후지',50000,50000,50000,500,'{}'), ('2025-01-01','KAMIS',1,'사과/후지',1,1,1,1,'{}')"))
    for who in ("md_fruit", "buyer"):
        row = next(r for r in world.client.get("/matrix", headers=login(world, who)).json()["rows"] if r["item_name"] == "Apple")
        t = row["trend"]
        assert [p[0] for p in t["ours"]] == ["2026-11-01", "2026-11-02", "2026-11-03"] and t["ours"][0][1] == "10400"
        assert [p[0] for p in t["wholesale"]] == ["2026-11-01", "2026-11-03"] and t["wholesale"][0][1] == "500.00"   # 2025 row is outside the 14-day window
        assert len(t["competitor"]) == 1 and t["competitor"][0][1] == "8400.00"        # 700 per unit x 12-unit pack
    assert keys_anywhere(world.client.get("/matrix", headers=login(world, "buyer")).json(), COST_KEYS) == set()


def test_margin_trend_is_cost_data_md_gets_it_buyer_does_not(world):
    with world.e.begin() as c:
        c.execute(text("INSERT INTO int_operations_inventory (recorded_date, std_item_id, internal_sku, channel_name, purchase_cost, fixed_cost, "
                       "logistics_cost, channel_fee_rate, promo_cost, vat_inclusive, pack_std_units, current_selling_price, stock_qty, daily_sales_qty, "
                       "min_price_floor, lifecycle_metric) SELECT '2026-11-02', std_item_id, internal_sku, channel_name, purchase_cost, fixed_cost, logistics_cost, "
                       "channel_fee_rate, promo_cost, vat_inclusive, pack_std_units, 11000, stock_qty, daily_sales_qty, min_price_floor, lifecycle_metric "
                       "FROM int_operations_inventory WHERE internal_sku = 'APPLE-1' AND recorded_date = '2026-11-03'"))
    md = next(r for r in world.client.get("/matrix", headers=login(world, "md_fruit")).json()["rows"] if r["item_name"] == "Apple")
    series = md["internal"]["margin_trend"]
    assert [p[0] for p in series] == ["2026-11-02", "2026-11-03"]
    assert float(series[0][1]) > float(series[1][1])                       # a higher price the day before -> a higher margin
    assert series[1][1] == md["internal"]["margin_rate"]                   # the latest point is the current margin
    buyer = world.client.get("/matrix", headers=login(world, "buyer")).json()
    assert "margin_trend" not in str(buyer)


def test_trend_history_is_fetched_once_per_table_not_once_per_row(world):
    from sqlalchemy import event
    seen = []
    def spy(conn, cursor, statement, *a):
        if "AND recorded_date >= " in statement or "AND scraped_at < " in statement:      # the history queries (they have an upper bound)
            seen.append(statement.split("FROM")[1].split()[0])
    event.listen(world.e, "before_cursor_execute", spy)
    try:
        rows = world.client.get("/matrix", headers=login(world, "admin")).json()["rows"]
    finally:
        event.remove(world.e, "before_cursor_execute", spy)
    assert len(rows) == 2 and sorted(seen) == ["ext_public_benchmarks", "ext_retail_prices", "int_operations_inventory"]


def test_matrix_runs_a_fixed_number_of_queries_regardless_of_item_count(world):
    from sqlalchemy import event
    def count():
        n = []
        def spy(conn, cursor, statement, *a):
            if "ext_" in statement or "int_operations_inventory" in statement:
                n.append(statement)
        event.listen(world.e, "before_cursor_execute", spy)
        try:
            world.client.get("/matrix", headers=login(world, "admin"))
        finally:
            event.remove(world.e, "before_cursor_execute", spy)
        return len(n)
    base = count()
    with world.e.begin() as c:       # a third inventory row for the same item (another channel)
        c.execute(text("INSERT INTO int_operations_inventory (recorded_date, std_item_id, internal_sku, channel_name, purchase_cost, fixed_cost, "
                       "logistics_cost, channel_fee_rate, promo_cost, vat_inclusive, pack_std_units, current_selling_price, stock_qty, daily_sales_qty, "
                       "min_price_floor, lifecycle_metric) SELECT recorded_date, std_item_id, internal_sku, 'OtherChannel', purchase_cost, fixed_cost, "
                       "logistics_cost, channel_fee_rate, promo_cost, vat_inclusive, pack_std_units, current_selling_price, stock_qty, daily_sales_qty, "
                       "min_price_floor, lifecycle_metric FROM int_operations_inventory WHERE internal_sku = 'APPLE-1'"))
    assert count() == base


# ---- market intelligence ----------------------------------------------------------------------------------
MI_CSV = "week,category,keyword,searches\n" + "".join(
    f"2026-{m:02d}-{d:02d},{c},{k},{v}\n"
    for c, k, vals in [("Fresh", "dalgi", [5, 5, 6, 8, 14, 20]), ("Dairy", "milk", [90, 90, 95, 90, 91, 90]), ("", "mystery", [1, 2, 3, 4, 5, 6])]
    for (m, d), v in zip([(9, 7), (9, 14), (9, 21), (9, 28), (10, 5), (10, 12)], vals))
MI_DEMAND = "week,category,gmv\n" + "".join(f"2026-{m:02d}-{d:02d},{c},{g}\n" for c, base in [("Fresh", 1000), ("Dairy", 3000)]
                                          for i, ((m, d), g) in enumerate(zip([(9, 7), (9, 14), (9, 21), (9, 28), (10, 5), (10, 12)], [base + 100 * i * (1 if c == "Fresh" else -1) for i in range(6)])))


def upload(w, who, kind, body, **q):
    params = "&".join(f"{k}={v}" for k, v in {"signal_type": kind, **q}.items())
    return w.client.post(f"/mi/import?{params}", content=body, headers={**login(w, who), "Content-Type": "text/csv"})


def test_mi_import_is_admin_only_and_reports_bad_rows(world):
    assert upload(world, "md_fruit", "SEARCH", MI_CSV).status_code == 403
    assert upload(world, "admin", "WEATHER", MI_CSV).status_code == 422
    r = upload(world, "admin", "SEARCH", MI_CSV + "not-a-week,Fresh,x,1\n")
    assert r.status_code == 200 and r.json()["loaded"] == 18 and r.json()["errors"][0]["line"] == 20 and r.json()["error_count"] == 1
    assert any(e["action"] == "MI_IMPORT" for e in world.repo.audit_entries())
    assert upload(world, "admin", "SEARCH", b"x" * (5 * 1024 * 1024 + 1)).status_code == 413


def test_mi_report_is_filtered_to_the_callers_categories(world):
    assert upload(world, "admin", "SEARCH", MI_CSV).status_code == 200 and upload(world, "admin", "BUYER", MI_DEMAND).status_code == 200
    admin = world.client.get("/mi/report", headers=login(world, "admin")).json()
    md = world.client.get("/mi/report", headers=login(world, "md_fruit")).json()
    assert {h["category"] for h in admin["health"]} == {"Fresh", "Dairy"} and {r["keyword"] for r in admin["radar"]} >= {"dalgi", "mystery"}
    assert {h["category"] for h in md["health"]} == {"Fresh"} and {r["keyword"] for r in md["radar"]} == {"dalgi"}     # no Dairy, no category-less rows
    assert next(h for h in md["health"] if h["category"] == "Fresh")["share_pct"] == next(h for h in admin["health"] if h["category"] == "Fresh")["share_pct"]
    assert world.client.get("/mi/report", headers=login(world, "nobody")).json()["health"] == []
    assert world.client.get("/mi/report").status_code == 401
    buyer = world.client.get("/mi/report", headers=login(world, "buyer"))
    assert buyer.status_code == 200 and not keys_anywhere(buyer.json(), COST_KEYS)
    seed = next(r for r in md["radar"] if r["keyword"] == "dalgi")
    assert seed["stage"] == "SEED" and seed["search_series"][-1] == ["2026-10-12", "20"]


def test_mi_report_uses_inventory_for_supply_cover(world):
    upload(world, "admin", "BUYER", MI_DEMAND)
    fresh = next(h for h in world.client.get("/mi/report", headers=login(world, "admin")).json()["health"] if h["category"] == "Fresh")
    # inventory: stock 100+300 over daily sales 10+100 = 3.6 days
    assert fresh["supply"]["cover_days"] == 3.6 and fresh["supply"]["status"] == "BALANCED"


def test_mi_report_language_parameter(world):
    upload(world, "admin", "SEARCH", MI_CSV)
    h = login(world, "admin")
    ko = world.client.get("/mi/report?lang=ko", headers=h).json()
    en = world.client.get("/mi/report", headers=h).json()
    assert "검색이" in next(r for r in ko["radar"] if r["keyword"] == "dalgi")["changed"] and "Searches" in next(r for r in en["radar"] if r["keyword"] == "dalgi")["changed"]
    assert world.client.get("/mi/report?lang=fr", headers=h).status_code == 422


# ---- crawlers -----------------------------------------------------------------------------------------------
def test_crawler_pages_are_for_md_and_admin_and_only_admin_can_change_them(world):
    assert world.client.get("/crawlers").status_code == 401
    assert world.client.get("/crawlers", headers=login(world, "buyer")).status_code == 403
    md = login(world, "md_fruit")
    listing = world.client.get("/crawlers", headers=md).json()["crawlers"]
    assert [c["id"] for c in listing][:3] == ["report", "buyer", "news"]
    assert world.client.get("/crawlers/news", headers=md).json()["fields"][0]["key"] == "feeds"
    assert world.client.get("/crawlers/nope", headers=md).status_code == 404
    assert world.client.put("/crawlers/news", json={"enabled": False}, headers=md).status_code == 403
    assert world.client.post("/crawlers/report/run", headers=md).status_code == 403
    assert world.client.post("/crawlers/report/upload?filename=a.csv", content=b"x", headers=md).status_code == 403


def test_admin_edits_schedule_and_settings_with_validation_and_audit(world):
    h = login(world, "admin")
    ok = world.client.put("/crawlers/news", headers=h, json={"enabled": False, "times": ["06:15", "18:00"],
                          "config": {"feeds": ["https://news.example/rss"], "lookback_days": 7}})
    assert ok.status_code == 200 and ok.json()["enabled"] is False and ok.json()["times"] == ["06:15", "18:00"]
    assert ok.json()["config"]["feeds"] == ["https://news.example/rss"] and ok.json()["state"] == "DISABLED"
    for bad in ({"times": ["25:00"]}, {"config": {"feeds": ["http://127.0.0.1/x"]}}, {"config": {"lookback_days": 0}}, {"config": {"evil": 1}}):
        assert world.client.put("/crawlers/news", headers=h, json=bad).status_code == 422
    assert world.client.get("/crawlers/news", headers=h).json()["times"] == ["06:15", "18:00"]           # rejected edits changed nothing
    assert any(e["action"] == "CRAWLER_UPDATED" for e in world.repo.audit_entries())


def test_manual_run_is_queued_once_for_the_worker(world):
    h = login(world, "admin")
    first = world.client.post("/crawlers/report/run", headers=h)
    assert first.status_code == 202 and first.json()["status"] == "QUEUED"
    assert world.client.post("/crawlers/report/run", headers=h).status_code == 409
    detail = world.client.get("/crawlers/report", headers=h).json()
    assert detail["runs"][0]["status"] == "QUEUED" and detail["state"] == "QUEUED"


def test_upload_to_a_file_crawler_imports_immediately_and_feeds_the_radar(world):
    h = login(world, "admin")
    r = world.client.post("/crawlers/report/upload?filename=../../etc/passwd.csv", content=MI_CSV.encode(), headers=h)
    assert r.status_code == 200 and r.json()["status"] == "OK" and r.json()["stored"] == 18
    saved = list((world.e.url.database and __import__("pathlib").Path(world.e.url.database).parent / "inbox" / "report" / "processed").iterdir())
    assert len(saved) == 1 and "passwd" not in saved[0].name and saved[0].parent.name == "processed"        # client file names never reach the disk
    assert "dalgi" in {x["keyword"] for x in world.client.get("/mi/report", headers=h).json()["radar"]}
    assert world.client.get("/crawlers/report", headers=h).json()["data_as_of"] == "2026-10-12"
    assert world.client.post("/crawlers/report/upload?filename=a.exe", content=b"x", headers=h).status_code == 422
    assert world.client.post("/crawlers/news/upload?filename=a.csv", content=b"x", headers=h).status_code == 422       # not a file crawler
    assert world.client.post("/crawlers/report/upload?filename=a.csv", content=b"x" * (5 * 1024 * 1024 + 1), headers=h).status_code == 413
    assert any(e["action"] == "CRAWLER_UPLOAD" for e in world.repo.audit_entries())
