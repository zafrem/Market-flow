"""Data-quality measures and alerts (SRS F-08-N5 / N6) and the re-check list for confirmed matches."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from marketflow import cli
from marketflow.crawlers.service import CrawlerService
from marketflow.db import init_sqlite, make_engine
from marketflow.ops_repo import Repo, ReviewItem
from marketflow.quality import alerts_from_quality, cycle_hours, load_thresholds, quality_report
from marketflow.repository import SqlRepository
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.web.app import create_app
from marketflow.web.repo import AppRepository
from marketflow.web.security import hash_password

T0 = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)
NOW = T0.replace(tzinfo=None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)
    tp = FixedTimeProvider(T0)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'q.db'}", jwt_secret="j" * 48, inbox_dir=str(tmp_path / "inbox"), public_url="https://mf.example.com")
    e = make_engine(s.database_url)
    init_sqlite(e)
    return type("E", (), dict(tp=tp, s=s, e=e, repo=AppRepository(e), svc=CrawlerService(e, s, tp)))


def test_cycle_is_the_longest_wait_between_runs():
    assert cycle_hours(["06:00", "09:00"]) == 21.0           # 09:00 -> 06:00 next day
    assert cycle_hours(["08:00", "13:00", "19:00"]) == 13.0
    assert cycle_hours(["07:30"]) == 24.0 and cycle_hours([]) == 24.0


def test_thresholds_default_and_validate():
    assert load_thresholds(None).min_success_rate == 0.95
    assert load_thresholds({"max_recheck": 3}).max_recheck == 3
    with pytest.raises(ValueError, match="quality"):
        load_thresholds({"max_recheck": "many"})


def run(env, cid, status, hours_ago):
    at = NOW - timedelta(hours=hours_ago)
    rid = env.svc.store.start_run(cid, "SCHEDULED", "scheduler", at)
    env.svc.store.finish_run(rid, status, 1, "x", at)


def listing(env, code, hours_ago, platform="SHOPA"):
    SqlRepository(env.e).insert_retail([{
        "scraped_at": NOW - timedelta(hours=hours_ago), "platform": platform, "std_item_id": 1, "match_confidence": Decimal("0.9"),
        "channel_item_code": code, "product_title": "t", "original_price": 1, "sale_price": 1, "normalized_unit_price": Decimal("1"),
        "is_out_of_stock": 0, "badge_info": None, "extraction_source": "SELECTOR", "region_code": "SEOUL"}])


def test_report_measures_success_staleness_review_and_matches(env):
    for hours in (30, 50, 70):                                       # one OK, three failures in the last week
        run(env, "buyer", "FAILED", hours)
    run(env, "buyer", "OK", 5 * 24)                                  # last good run 5 days ago: far beyond twice its cycle
    run(env, "report", "OK", 2)                                      # healthy
    env.repo.add_review([ReviewItem("UNPARSEABLE_TITLE", "SHOPA", f"t{i}", "no unit") for i in range(3)], NOW)
    env.repo.add_review([ReviewItem("UNMATCHED_TITLE", "SHOPA", "durian", "no match")], NOW)
    listing(env, "SEEN", 2); listing(env, "OLD", 24 * 9)
    env.repo.save_match("SHOPA", "SEEN", 1, "SAME", "kim", NOW)      # confirmed, seen two hours ago
    env.repo.save_match("SHOPA", "OLD", 1, "SAME", "kim", NOW)       # confirmed, not seen for nine days -> re-check
    SqlRepository(env.e).propose_matches([{"platform": "SHOPA", "channel_item_code": "NEW", "std_item_id": 1, "match_confidence": Decimal("0.9")}], NOW)
    rep = quality_report(Repo(env.e), env.svc, load_thresholds({"max_recheck": 0, "max_unparseable": 2, "max_unconfirmed": 0}), NOW)
    by = {c["id"]: c for c in rep["crawlers"]}
    assert by["buyer"]["success_rate"] == 0.25                      # 1 OK (5 days ago, inside the 7-day window) and 3 failures
    assert by["report"]["success_rate"] == 1.0 and by["report"]["stale"] is False
    assert (rep["unparseable_titles"], rep["unmapped_items"], rep["unconfirmed_matches"], rep["matches_to_recheck"]) == (3, 1, 1, 1)
    codes = {(i["code"], i["subject"]) for i in rep["issues"]}
    assert ("LOW_SUCCESS_RATE", "buyer") in codes and ("STALE_DATA", "buyer") in codes
    assert ("UNPARSEABLE_TITLES", "3") in codes and ("UNCONFIRMED_MATCHES", "1") in codes and ("MATCHES_TO_RECHECK", "1") in codes
    assert all(i["code"] != "UNMAPPED_ITEMS" for i in rep["issues"])        # 1 <= default limit of 50
    assert rep["competitor_data_age_hours"] == 2.0


def test_nothing_is_flagged_for_a_quiet_new_install(env):
    rep = quality_report(Repo(env.e), env.svc, load_thresholds(None), NOW)
    assert rep["issues"] == [] and rep["competitor_data_age_hours"] is None


def test_alerts_have_a_link_and_no_money_figures(env):
    run(env, "buyer", "FAILED", 3); run(env, "buyer", "FAILED", 4)
    listing(env, "A", 1)
    env.repo.save_match("SHOPA", "A", 1, "SAME", "kim", NOW)
    rep = quality_report(Repo(env.e), env.svc, load_thresholds({"max_unconfirmed": 0}), NOW)
    alerts = alerts_from_quality(rep, "https://mf.example.com")
    assert alerts and all(a.severity == "AMBER" and a.text.endswith("https://mf.example.com") and "₩" not in a.text for a in alerts)
    assert len({a.key for a in alerts}) == len(alerts)                    # distinct keys: one alert per problem and crawler


def test_api_and_cli(env):
    repo = env.repo
    for key, role in (("admin", "ADMIN"), ("md", "MD"), ("buyer", "BUYER")):
        repo.set_assignments(repo.create_user(f"{key}@x.com", key, role, hash_password("correct-horse-battery")), [])
    c = TestClient(create_app(env.s, env.e, env.tp))
    hdr = lambda who: {"Authorization": "Bearer " + c.post("/auth/login", json={"email": f"{who}@x.com", "password": "correct-horse-battery"}).json()["access_token"]}   # noqa: E731
    assert c.get("/quality", headers=hdr("buyer")).status_code == 403
    body = c.get("/quality", headers=hdr("md")).json()
    assert {"crawlers", "issues", "unconfirmed_matches", "matches_to_recheck", "competitor_data_age_hours"} <= set(body)
    import io
    out = io.StringIO()
    assert cli.main(["quality"], settings=env.s, tp=env.tp, out=out) == 0 and "unconfirmed=0" in out.getvalue() and "buyer" in out.getvalue()
