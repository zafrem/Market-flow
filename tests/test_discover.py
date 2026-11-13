"""Discovery view: search volume and new competitor products first, scoped by category, no cost data."""
import json

import pytest

from marketflow import demo
from marketflow.db import init_sqlite, make_engine
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.web.app import create_app
from marketflow.web.repo import AppRepository
from marketflow.web.security import hash_password
from fastapi.testclient import TestClient
from test_api import PW, SECRET, T0  # noqa: F401


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    tp = FixedTimeProvider(T0)
    s = Settings(database_url=f"sqlite:///{tmp_path_factory.mktemp('disc') / 'd.db'}", jwt_secret=SECRET)
    e = make_engine(s.database_url); init_sqlite(e)
    demo.load_demo(e, s, tp)
    repo = AppRepository(e)
    for key, role, cats in [("admin", "ADMIN", []), ("proc", "MD", [("Processed", "")]), ("buyer", "BUYER", [("Fresh", "")])]:
        uid = repo.create_user(f"{key}@x.com", key, role, hash_password(PW)); repo.set_assignments(uid, cats)
    client = TestClient(create_app(s, e, tp))
    def get(who, path="/discover?lang=ko"):
        tok = client.post("/auth/login", json={"email": f"{who}@x.com", "password": PW}).json()["access_token"]
        return client.get(path, headers={"Authorization": "Bearer " + tok})
    return get


def test_keywords_are_ranked_by_search_volume_and_marked_when_the_catalog_lacks_them(api):
    d = api("admin").json()
    vols = [k["searches"] for k in d["top_searches"]]
    assert vols == sorted(vols, reverse=True) and len(vols) == 10
    by = {k["keyword"]: k for k in d["top_searches"]}
    assert by["라면"]["in_catalog"] is True and by["그릭요거트"]["in_catalog"] is False
    assert d["summary"]["top_not_in_catalog"] == sum(1 for k in d["top_searches"] if not k["in_catalog"])


def test_rising_keywords_are_the_fast_growers(api):
    d = api("admin").json()
    assert d["rising"] and all(k["growth"] > 0 for k in d["rising"])
    assert [k["growth"] for k in d["rising"]] == sorted((k["growth"] for k in d["rising"]), reverse=True)
    assert {"그릭요거트", "곤약면"} & {k["keyword"] for k in d["rising"][:2]}      # the demo's fastest risers


def test_new_competitor_products_carry_price_and_whether_we_sell_the_item(api):
    d = api("admin").json()
    p = next(x for x in d["new_products"] if x["channel_item_code"] == "KURLY-NEW-GREEK")
    assert p["price"] == 7900 and p["category"] == "Processed" and p["we_sell_item"] is True and p["platform"] == "KURLY"


def test_scope_limits_every_list_and_no_cost_data_leaks(api):
    d = api("proc").json()
    assert {k["category"] for k in d["top_searches"]} == {"Processed"} and all(p["category"] == "Processed" for p in d["new_products"])
    b = api("buyer")                                                     # BUYER may read it: nothing here is cost or margin
    assert b.status_code == 200
    assert not any(w in b.text for w in ("purchase_cost", "margin", "floor"))
    assert all(p["category"] == "Fresh" for p in b.json()["new_products"])


