"""Each crawling process, end to end: settings -> collector -> parser rules -> ingest -> SQLite."""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from conftest import FIX
from marketflow.db import init_sqlite, make_engine
from marketflow.seed import ALIASES, ITEMS
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.transport import FixtureTransport
from marketflow.worker import run_jobs

TP = FixedTimeProvider(datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc))  # 2026-11-03 KST
GARAK_URL = "https://garak.example/api"
CONF = Settings(kamis_api_key="K", kamis_api_id="ID", garak_url=GARAK_URL, garak_api_key="G")


@pytest.fixture()
def engine(tmp_path):
    return make_engine(f"sqlite:///{tmp_path / 'mf.db'}")


def rows(engine, source=None):
    q = "SELECT source_type, raw_item_name, recorded_date, avg_price, min_price, max_price, normalized_price, extra_metrics " \
        "FROM ext_public_benchmarks" + (" WHERE source_type = :s" if source else "") + " ORDER BY raw_item_name"
    with engine.connect() as c:
        return c.execute(text(q), {"s": source} if source else {}).mappings().all()


def transport():
    return FixtureTransport({"kamis.or.kr": FIX / "kamis_daily.json", GARAK_URL: FIX / "garak_daily.json"})


async def test_kamis_crawl_stores_normalized_rows(engine):
    s = Settings(kamis_api_key="K", kamis_api_id="ID")
    t = transport()
    res = (await run_jobs(engine, s, t, TP))["KAMIS"]
    assert (res.stored, res.unmapped, res.unconvertible) == (2, ["두리안/상품"], ["무/상품"])
    got = {r["raw_item_name"]: r for r in rows(engine, "KAMIS")}
    assert set(got) == {"사과/후지/상품", "양파/양파/상품"}
    apple = got["사과/후지/상품"]
    assert apple["recorded_date"] == "2026-11-03"                 # KST date, not UTC
    assert Decimal(str(apple["normalized_price"])) == Decimal("523.00")   # 52,300 per 10kg -> per 100g
    assert Decimal(str(got["양파/양파/상품"]["normalized_price"])) == Decimal("145.00")
    assert json.loads(apple["extra_metrics"]) == {"price_basis": "single", "unit": "10kg"}


async def test_kamis_request_carries_tokens_from_settings(engine):
    t = transport()
    await run_jobs(engine, Settings(kamis_api_key="MYKEY", kamis_api_id="MYID", kamis_product_cls="01"), t, TP)
    params = t.calls[0][1]
    assert params["p_cert_key"] == "MYKEY" and params["p_cert_id"] == "MYID"
    assert params["p_product_cls_code"] == "01" and params["action"] == "dailySalesList"


async def test_garak_crawl_stores_range_and_volume(engine):
    t = transport()
    res = (await run_jobs(engine, Settings(garak_url=GARAK_URL, garak_api_key="GK"), t, TP))["GARAK"]
    assert res.stored == 2
    assert t.calls[0] == (GARAK_URL, {"serviceKey": "GK"})
    got = {r["raw_item_name"]: r for r in rows(engine, "GARAK")}
    apple = got["사과/후지/특"]
    assert (Decimal(str(apple["min_price"])), Decimal(str(apple["max_price"]))) == (45000, 80000)
    assert Decimal(str(apple["normalized_price"])) == Decimal("400.00")   # 60,000 per 15kg
    assert json.loads(apple["extra_metrics"])["inflow_kg"] == "12000"
    green = got["대파/상"]
    assert Decimal(str(green["min_price"])) == Decimal(str(green["max_price"])) == 2100


async def test_both_sources_and_rerun_is_idempotent(engine):
    await run_jobs(engine, CONF, transport(), TP)
    await run_jobs(engine, CONF, transport(), TP)
    assert len(rows(engine)) == 4 and len(rows(engine, "KAMIS")) == 2 and len(rows(engine, "GARAK")) == 2


async def test_rerun_with_new_price_updates_in_place(engine):
    await run_jobs(engine, Settings(kamis_api_key="K", kamis_api_id="ID"), transport(), TP)
    changed = (FIX / "kamis_daily.json").read_text().replace("52,300", "60,000")
    t = FixtureTransport({"kamis.or.kr": changed})
    await run_jobs(engine, Settings(kamis_api_key="K", kamis_api_id="ID"), t, TP)
    apple = next(r for r in rows(engine, "KAMIS") if r["raw_item_name"].startswith("사과"))
    assert Decimal(str(apple["normalized_price"])) == Decimal("600.00") and len(rows(engine, "KAMIS")) == 2


async def test_source_without_token_is_skipped_with_hint(engine, caplog):
    with caplog.at_level(logging.WARNING):
        results = await run_jobs(engine, Settings(garak_url=GARAK_URL), transport(), TP)
    assert set(results) == {"GARAK"} and "KAMIS skipped" in caplog.text


async def test_no_tokens_configured_crawls_nothing(engine):
    t = transport()
    assert await run_jobs(engine, Settings(), t, TP) == {} and t.calls == []


async def test_one_failing_source_does_not_block_the_other(engine):
    class Flaky:
        calls = []
        async def get_text(self, url, params=None):
            if "kamis" in url:
                raise ConnectionError("kamis down")
            return (FIX / "garak_daily.json").read_text()
    results = await run_jobs(engine, CONF, Flaky(), TP)
    assert isinstance(results["KAMIS"], ConnectionError) and results["GARAK"].stored == 2
    assert len(rows(engine, "GARAK")) == 2 and rows(engine, "KAMIS") == []


async def test_bad_token_response_reported_as_failure_not_silent_success(engine):
    t = FixtureTransport({"kamis.or.kr": json.dumps({"data": ["200"]})})   # KAMIS auth-error code
    res = await run_jobs(engine, Settings(kamis_api_key="bad", kamis_api_id="x"), t, TP)
    assert type(res["KAMIS"]).__name__ == "KamisError" and rows(engine) == []


async def test_empty_day_is_not_an_error(engine):
    t = FixtureTransport({"kamis.or.kr": json.dumps({"data": ["001"]})})
    res = await run_jobs(engine, Settings(kamis_api_key="K", kamis_api_id="ID"), t, TP)
    assert res["KAMIS"].stored == 0


async def test_schema_drift_stores_nothing_then_rule_promotion_heals_it(engine, isolated_rules):
    drifted = (FIX / "kamis_daily.json").read_text().replace('"dpr1"', '"price_today"')
    s = Settings(kamis_api_key="K", kamis_api_id="ID")
    t = FixtureTransport({"kamis.or.kr": drifted})
    assert (await run_jobs(engine, s, t, TP))["KAMIS"].stored == 0
    isolated_rules.promote(["sources", "KAMIS", "fields", "price"], "price_today", "tester")
    assert (await run_jobs(engine, s, t, TP))["KAMIS"].stored == 2


def test_sqlite_bootstrap_is_idempotent_and_seeds_catalog(engine):
    init_sqlite(engine); init_sqlite(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM master_products")).scalar() == len(ITEMS)
        assert c.execute(text("SELECT COUNT(*) FROM master_product_aliases")).scalar() == 2 * len(ALIASES)


def test_python_seed_matches_mysql_seed_sql():
    from marketflow.settings import ROOT
    sql = (ROOT / "db" / "init" / "002_seed.sql").read_text(encoding="utf-8")
    for _, _, name, unit in ITEMS:
        assert f"'{name}'" in sql and unit in sql
    for alias, item in ALIASES.items():
        assert f"'{alias}'" in sql and f"'{item}'" in sql


def test_relative_sqlite_url_anchored_at_project_root(tmp_path):
    from marketflow.db import resolve_database_url
    from marketflow.settings import ROOT
    assert resolve_database_url("sqlite:///data/a.db") == f"sqlite:///{ROOT / 'data' / 'a.db'}"
    assert resolve_database_url("sqlite:///:memory:") == "sqlite:///:memory:"
