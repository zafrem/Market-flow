"""Promotion calendar: MD/ADMIN record campaigns, scoped by category, audited."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from marketflow.decision.service import Actor
from marketflow.promotions import active_on, add_promotion, list_promotions, remove_promotion
from marketflow.settings import Settings
from marketflow.web.app import create_app
from marketflow.web.security import hash_password
from marketflow.db import init_sqlite, make_engine
from marketflow.time_provider import FixedTimeProvider
from marketflow.web.repo import AppRepository
from datetime import datetime, timezone

T0 = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)
    tp = FixedTimeProvider(T0)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'w.db'}", jwt_secret="j" * 48, inbox_dir=str(tmp_path / "inbox"))
    e = make_engine(s.database_url)
    init_sqlite(e)
    return type("E", (), dict(tp=tp, s=s, e=e, repo=AppRepository(e)))

MD = Actor("kim@x.com", "MD")
BUYER = Actor("b@x.com", "BUYER")


def test_add_list_and_state(env):
    today = date(2026, 11, 3)
    pid = add_promotion(env.repo, MD, env.tp.now(), "Chuseok sale", today, date(2026, 11, 6), "Fresh", "SHOPA", 20)
    got = list_promotions(env.repo, today)
    assert [p["name"] for p in got] == ["Chuseok sale"] and got[0]["state"] == "ACTIVE" and float(got[0]["discount_pct"]) == 20
    assert active_on(env.repo, today, "Fresh", "SHOPA") and not active_on(env.repo, today, "Dairy", "SHOPA")
    remove_promotion(env.repo, MD, env.tp.now(), pid)
    assert list_promotions(env.repo, today) == []
    assert {"PROMOTION_ADDED", "PROMOTION_REMOVED"} <= {a["action"] for a in env.repo.audit_entries()}


def test_rules(env):
    d = date(2026, 11, 1)
    with pytest.raises(PermissionError):
        add_promotion(env.repo, BUYER, env.tp.now(), "x", d, d)
    with pytest.raises(ValueError):
        add_promotion(env.repo, MD, env.tp.now(), "x", d, date(2026, 10, 1))
    with pytest.raises(ValueError):
        add_promotion(env.repo, MD, env.tp.now(), " ", d, d)
    with pytest.raises(ValueError):
        add_promotion(env.repo, MD, env.tp.now(), "x", d, d, discount_pct=120)


def test_api_scope(env):
    app = TestClient(create_app(env.s, env.e, env.tp))
    for email, role, cats in (("kim@x.com", "MD", [("Fresh", "Fruit")]), ("adm@x.com", "ADMIN", [])):
        uid = env.repo.create_user(email, email[:3], role, hash_password("correct-horse-battery"))
        if cats:
            env.repo.set_assignments(uid, cats)
    def login(e):
        return {"Authorization": "Bearer " + app.post("/auth/login", json={"email": e, "password": "correct-horse-battery"}).json()["access_token"]}
    kim, adm = login("kim@x.com"), login("adm@x.com")
    body = {"name": "Week", "start_date": "2026-11-02", "end_date": "2026-11-09", "category_l": "Fresh"}
    assert app.post("/promotions", json=body, headers=kim).status_code == 201
    assert app.post("/promotions", json={**body, "category_l": "Dairy"}, headers=kim).status_code == 404       # outside kim's scope
    assert app.post("/promotions", json={**body, "category_l": ""}, headers=kim).status_code == 403            # everything is admin-only
    assert app.post("/promotions", json={**body, "end_date": "2026-11-01"}, headers=kim).status_code == 422
    assert [p["name"] for p in app.get("/promotions", headers=kim).json()["promotions"]] == ["Week"]
    pid = app.get("/promotions", headers=adm).json()["promotions"][0]["promo_id"]
    assert app.delete(f"/promotions/{pid}", headers=kim).status_code == 200
    assert app.get("/promotions", headers=adm).json()["promotions"] == []
