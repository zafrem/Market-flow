"""Demo data and the clear tool: plausible data into an empty store only, clearing keeps users and the catalog, ADMIN only."""
import time
from datetime import date

import pytest
from sqlalchemy import text

from marketflow import demo
from marketflow.db import init_sqlite, make_engine
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from test_api import T0, login, world  # noqa: F401  (fixture)


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    e = make_engine(f"sqlite:///{tmp_path_factory.mktemp('demo') / 'd.db'}"); init_sqlite(e)
    summary = demo.load_demo(e, Settings(), FixedTimeProvider(T0))
    return e, summary


def test_demo_fills_every_area_with_plausible_figures(loaded):
    e, summary = loaded
    n = demo.counts(e)
    for table in ("ext_public_benchmarks", "ext_retail_prices", "int_operations_inventory", "inventory_revisions", "promotions",
                  "mi_observations", "price_recommendations", "product_channel_map"):
        assert n[table] > 0, table
    assert n["master_products"] == 19 and summary["recommendations"] == n["price_recommendations"]
    with e.connect() as c:
        # every internal price sits above the cost it was built from and not absurdly far above the market
        bad = c.execute(text("SELECT COUNT(*) FROM int_operations_inventory WHERE current_selling_price <= purchase_cost / 10")).scalar()
        assert bad == 0
        status = dict(c.execute(text("SELECT status, COUNT(*) FROM price_recommendations GROUP BY status")).all())
        assert status                                                         # decisions ran on the generated data
        unconfirmed = c.execute(text("SELECT COUNT(*) FROM product_channel_map WHERE match_grade <> 'SAME' OR reviewed_at IS NULL")).scalar()
        assert unconfirmed >= 2                                               # a few listings wait for a person to grade them
        days = c.execute(text("SELECT COUNT(DISTINCT recorded_date) FROM int_operations_inventory")).scalar()
        assert days == demo.HISTORY_DAYS


def test_demo_refuses_a_database_that_already_has_data(loaded):
    e, _ = loaded
    with pytest.raises(ValueError, match="already holds data"):
        demo.load_demo(e, Settings(), FixedTimeProvider(T0))


def test_clear_removes_operational_data_and_demo_items_but_keeps_users_and_audit(loaded):
    e, _ = loaded
    with e.begin() as c:
        c.execute(text("INSERT INTO users (email, name, role, password_hash) VALUES ('a@x.io','A','ADMIN','h')"))
        c.execute(text("INSERT INTO audit_log (ts, actor, action) VALUES ('t','a','X')"))
    removed = demo.clear_data(e)
    assert removed["ext_retail_prices"] > 0
    n = demo.counts(e)
    assert not demo.has_operational_data(e) and n["users"] == 1 and n["master_products"] == 8
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM audit_log")).scalar() >= 1
    demo.load_demo(e, Settings(), FixedTimeProvider(T0))                       # and it can be loaded again after a clear
    assert demo.has_operational_data(e)


# ---- API ---------------------------------------------------------------------------------------------------
def test_admin_endpoints_are_admin_only(world):
    for who in ("md_fruit", "buyer"):
        h = login(world, who)
        assert world.client.get("/admin/system", headers=h).status_code == 403
        assert world.client.post("/admin/demo", headers=h).status_code == 403
        assert world.client.post("/admin/clear", headers=h, json={"confirm": "DELETE"}).status_code == 403


def test_system_status_has_counts_and_no_secrets(world):
    r = world.client.get("/admin/system", headers=login(world, "admin"))
    body = r.json()
    assert r.status_code == 200 and body["store"] == world.e.dialect.name and body["local"] and body["counts"]["users"] == 5
    assert set(body["integrations"]) == {"kamis", "garak", "openai", "hive", "webhook"} and all(isinstance(v, bool) for v in body["integrations"].values())
    assert "jwt" not in r.text.lower()


def test_clear_needs_the_word_delete_and_is_audited(world):
    h = login(world, "admin")
    assert world.client.post("/admin/clear", headers=h, json={"confirm": "yes"}).status_code == 422
    assert demo.has_operational_data(world.e)
    r = world.client.post("/admin/clear", headers=h, json={"confirm": "DELETE"})
    assert r.status_code == 200 and r.json()["removed"]["ext_retail_prices"] == 1
    assert not demo.has_operational_data(world.e) and demo.counts(world.e)["users"] == 5
    assert any(a["action"] == "DATA_CLEARED" for a in world.client.get("/admin/audit", headers=h).json()["entries"])


def test_demo_job_refused_when_data_exists_then_runs_in_the_background(world, monkeypatch):
    h = login(world, "admin")
    assert world.client.post("/admin/demo", headers=h).status_code == 409           # the fixture already holds data
    world.client.post("/admin/clear", headers=h, json={"confirm": "DELETE"})
    monkeypatch.setattr(demo, "load_demo", lambda *a, **k: (time.sleep(0.2), {"inventory_rows": 1})[1])   # keep this test fast
    assert world.client.post("/admin/demo", headers=h).status_code == 202
    assert world.client.get("/admin/system", headers=h).json()["job"]["state"] == "RUNNING"
    assert world.client.post("/admin/demo", headers=h).status_code == 409           # one at a time
    assert world.client.post("/admin/clear", headers=h, json={"confirm": "DELETE"}).status_code == 409
    for _ in range(40):
        if world.client.get("/admin/system", headers=h).json()["job"]["state"] != "RUNNING":
            break
        time.sleep(0.1)
    job = world.client.get("/admin/system", headers=h).json()["job"]
    assert job["state"] == "DONE" and job["result"] == {"inventory_rows": 1}


def test_user_list_includes_categories(world):
    users = {u["email"]: u for u in world.client.get("/admin/users", headers=login(world, "admin")).json()["users"]}
    assert users["md_fruit@x.com"]["categories"] == ["Fresh/Fruit"] and users["admin@x.com"]["categories"] == []
