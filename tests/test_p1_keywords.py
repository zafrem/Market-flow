"""Revised specification, P1 keyword side (SRS F-08): PII filter, category classifier, synonym dictionary,
period-over-period / year-over-year change, search-sales gap diagnosis."""
import io
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from marketflow import cli
from marketflow.db import init_sqlite, make_engine
from marketflow.internal_data import import_internal
from marketflow.mi.classify import Classifier, evaluate
from marketflow.mi.config import load_mi_config
from marketflow.mi.importer import import_signals
from marketflow.mi.messages import MESSAGES
from marketflow.mi.privacy import contains_pii
from marketflow.mi.service import mi_report
from marketflow.mi.store import MiStore
from marketflow.repository import SqlRepository
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.web.app import create_app
from marketflow.web.repo import AppRepository
from marketflow.web.security import hash_password

T0 = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
NOW = T0.replace(tzinfo=None)
D = Decimal


@pytest.fixture()
def world(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path / 'k.db'}", jwt_secret="j" * 48, inbox_dir=str(tmp_path / "inbox"))
    e = make_engine(s.database_url)
    init_sqlite(e)
    return type("W", (), dict(s=s, e=e, store=MiStore(e), tmp=tmp_path, tp=FixedTimeProvider(T0)))


def write(w, name, body):
    p = w.tmp / name
    p.write_text(body, encoding="utf-8")
    return p


# ---- privacy ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["010-1234-5678", "문의 01012345678", "a.b+c@example.com", "900101-1234567", "서울시 강남구 역삼동", "테헤란로 152", "02-123-4567"])
def test_personal_data_is_recognised(text):
    assert contains_pii(text)


@pytest.mark.parametrize("text", ["사과 1.2kg", "한우 1등급 2팩", "딸기", "2026 W43", "3000원 쿠폰", "무항생제 계란 30구"])
def test_ordinary_keywords_are_not_flagged(text):
    assert not contains_pii(text)


def test_import_drops_rows_with_personal_data_and_never_stores_them(world):
    f = write(world, "s.csv", "week,keyword,category,searches\n2026-10-26,딸기,Fresh,100\n2026-10-26,010-1234-5678,Fresh,5\n2026-10-26,서울시 강남구 역삼동 맛집,Fresh,7\n")
    res = import_signals(f, world.store, "SEARCH")
    assert (res.loaded, res.pii_skipped) == (1, 2)
    kws = {r["keyword"] for r in world.store.observations(5)}
    assert kws == {"딸기"}


# ---- classifier -----------------------------------------------------------------------------------------------
def test_classifier_uses_labels_catalog_and_abstains_when_unsure():
    c = Classifier({"샤인머스켓": Counter({"Fresh": 3})}, {"사과": "Fresh", "apple": "Fresh", "세제": "Household"})
    assert c.classify("샤인머스켓").basis == "LABEL" and c.category_for("샤인머스켓") == "Fresh"
    assert c.classify("사과").basis == "CATALOG" and c.category_for("사과") == "Fresh"
    assert c.classify("햇 사과 선물세트").basis == "CONTAINS" and c.category_for("햇 사과 선물세트") == "Fresh"
    assert c.category_for("두리안") is None                                                        # unknown: left unclassified, not guessed
    mixed = Classifier({"배": Counter({"Fresh": 1, "Household": 1})}, {})
    assert mixed.category_for("배") is None                                                        # labels disagree: abstain
    tie = Classifier({}, {"사과": "Fresh", "과즙": "Beverage"})
    assert tie.category_for("사과 과즙") is None                                                    # two item names of equal length disagree: abstain


def test_import_fills_missing_categories_when_confident(world):
    f = write(world, "s.csv", "week,keyword,category,searches\n2026-10-26,사과 선물세트,,100\n2026-10-26,두리안,,50\n2026-10-26,양파,Fresh,30\n")
    res = import_signals(f, world.store, "SEARCH")
    cats = {r["keyword"]: r["category_l"] for r in world.store.observations(5)}
    assert cats == {"사과 선물세트": "Fresh", "두리안": "", "양파": "Fresh"} and (res.classified, res.unclassified) == (1, 1)


def test_classifier_evaluation_reports_accuracy_and_coverage():
    labelled = {k: Counter({"Fresh": 1}) for k in ("사과", "양파")} | {"세제": Counter({"Household": 1})}
    out = evaluate(labelled, {"사과": "Fresh", "양파": "Fresh", "세제": "Household"})
    assert out["keywords"] == 3 and out["accuracy"] == 1.0 and out["coverage"] == 1.0
    assert evaluate({}, {})["accuracy"] is None


# ---- synonyms ---------------------------------------------------------------------------------------------------
def seed_search(w, rows):
    w.store.upsert_observations([{"signal_type": "SEARCH", "period_start": p, "category_l": "Fresh", "keyword": k, "metric": m, "value": v, "source": ""}
                                 for p, k, m, v in rows])


def test_synonyms_fold_aliases_into_the_canonical_keyword(world):
    p = date(2026, 10, 26)
    seed_search(world, [(p, "샤인머스캣", "searches", 100), (p, "샤인머스켓", "searches", 300), (p, "청포도", "searches", 50)])
    world.store.set_synonym("샤인머스캣", "샤인머스켓", "kim", NOW)
    assert world.store.synonym_impact("샤인머스캣") == 1
    got = {(r["keyword"], r["value"]) for r in world.store.observations(5)}
    assert got == {("샤인머스켓", 400.0), ("청포도", 50.0)}
    assert world.store.remove_synonym("샤인머스캣") and len(world.store.observations(5)) == 3


def test_synonym_rules(world):
    st = world.store
    with pytest.raises(ValueError):
        st.set_synonym("a", "A", "kim", NOW)                       # same word
    st.set_synonym("b", "c", "kim", NOW)
    with pytest.raises(ValueError, match="itself an alias"):
        st.set_synonym("x", "b", "kim", NOW)                       # no chains: canonical must not be an alias
    with pytest.raises(ValueError, match="canonical keyword of other aliases"):
        st.set_synonym("c", "d", "kim", NOW)
    st.set_synonym("b", "e", "kim", NOW)                           # re-pointing updates, not duplicates
    assert st.synonyms() == {"b": "e"}


def test_report_shows_wow_and_yoy_with_synonyms_applied(world):
    cfg_weeks = [date(2026, 10, 26) - timedelta(weeks=i) for i in range(0, 7)][::-1]
    rows = [(w, "grower", "searches", 100 + 10 * i) for i, w in enumerate(cfg_weeks)]
    rows += [(date(2026, 10, 26) - timedelta(weeks=52), "grower", "searches", 80)]             # same week last year
    seed_search(world, rows)
    rep = mi_report(world.store, world.s, None, True, "en", NOW)
    g = next(r for r in rep["radar"] if r["keyword"] == "grower")
    assert g["wow"] == pytest.approx(160 / 150 - 1, abs=1e-4) and g["yoy"] == pytest.approx(160 / 80 - 1, abs=1e-4)
    assert rep["synonyms_applied"] == 0


# ---- API ------------------------------------------------------------------------------------------------------------
@pytest.fixture()
def api(world, monkeypatch):
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)
    repo = AppRepository(world.e)
    for key, role, cats in [("admin", "ADMIN", []), ("md", "MD", [("Fresh", "Fruit")]), ("buyer", "BUYER", [("Fresh", "Fruit")])]:
        repo.set_assignments(repo.create_user(f"{key}@x.com", key, role, hash_password("correct-horse-battery")), cats)
    client = TestClient(create_app(world.s, world.e, world.tp))
    hdr = lambda who: {"Authorization": "Bearer " + client.post("/auth/login", json={"email": f"{who}@x.com", "password": "correct-horse-battery"}).json()["access_token"]}   # noqa: E731
    return type("A", (), dict(c=client, h=hdr, repo=repo, w=world))


def test_synonym_api_is_admin_only_audited_and_reports_impact(api):
    seed_search(api.w, [(date(2026, 10, 26), "샤인머스캣", "searches", 100)])
    assert api.c.put("/mi/synonyms/샤인머스캣", json={"canonical": "샤인머스켓"}, headers=api.h("md")).status_code == 403
    r = api.c.put("/mi/synonyms/샤인머스캣", json={"canonical": "샤인머스켓"}, headers=api.h("admin"))
    assert r.status_code == 200 and r.json()["affected_keywords"] == 1
    assert api.c.put("/mi/synonyms/zzz", json={"canonical": "zzz"}, headers=api.h("admin")).status_code == 422
    assert api.c.get("/mi/synonyms", headers=api.h("md")).json()["synonyms"][0]["canonical"] == "샤인머스켓"
    assert api.c.get("/mi/synonyms", headers=api.h("buyer")).status_code == 403
    assert api.c.delete("/mi/synonyms/샤인머스캣", headers=api.h("admin")).status_code == 200
    assert api.c.delete("/mi/synonyms/샤인머스캣", headers=api.h("admin")).status_code == 404
    assert {"SYNONYM_SET", "SYNONYM_REMOVED"} <= {a["action"] for a in api.repo.audit_entries()}
    assert api.c.get("/mi/classifier", headers=api.h("md")).status_code == 403
    assert "accuracy" in api.c.get("/mi/classifier", headers=api.h("admin")).json()


def test_api_import_reports_privacy_and_classification(api):
    body = "week,keyword,category,searches\n2026-10-26,사과 세트,,10\n2026-10-26,010-1111-2222,Fresh,1\n"
    r = api.c.post("/mi/import?signal_type=SEARCH", content=body.encode(), headers=api.h("admin"))
    assert r.status_code == 200 and (r.json()["loaded"], r.json()["pii_skipped"], r.json()["classified"]) == (1, 1, 1)


def test_cli_synonyms_and_error_file(world):
    out = io.StringIO()
    run = lambda *argv: cli.main(list(argv), settings=world.s, tp=world.tp, out=out)               # noqa: E731
    assert run("synonym", "b", "c") == 0 and run("synonym", "x", "b") == 2 and "itself an alias" in out.getvalue()
    assert run("synonyms") == 0 and "b -> c" in out.getvalue()
    bad = write(world, "bad.csv", "week,keyword,searches\nnot-a-date,딸기,5\n2026-10-26,딸기,abc\n2026-10-26,딸기,7\n")
    errs = world.tmp / "errors.csv"
    run("import-mi", "--errors-file", str(errs), str(bad), "--type", "SEARCH")
    lines = errs.read_text(encoding="utf-8-sig").strip().splitlines()
    assert lines[0] == "line,field,reason" and len(lines) == 3


# ---- search-sales gap -----------------------------------------------------------------------------------------------
INV = """internal_sku,item_name,channel_name,purchase_cost,fixed_cost,logistics_cost,channel_fee_rate,current_selling_price,stock_qty,daily_sales_qty,remaining_days,pack_std_units
APPLE-1,Apple,SHOPA,5000,500,1000,11.5,10000,0,10,,12
ONION-1,Onion,SHOPA,2000,300,700,11.5,5000,300,10,,10
CAB-1,Napa Cabbage,SHOPA,1000,300,700,11.5,4000,300,10,,10
"""


@pytest.fixture()
def gap_world(world):
    d = date(2026, 10, 27)                                     # inside the search week starting 2026-10-26
    f = write(world, "inv.csv", INV)
    assert import_internal(f, AppRepository(world.e), world.s, world.tp, d).loaded == 3
    repo = SqlRepository(world.e)
    # a confirmed competitor sells the onion pack (10 x 100g) at 3,000 effective while we charge 5,000
    repo.insert_retail([{"scraped_at": NOW - timedelta(hours=1), "platform": "SHOPB", "std_item_id": 3, "match_confidence": D("0.9"),
                         "channel_item_code": "O1", "product_title": "양파 1kg", "original_price": 3000, "sale_price": 3000,
                         "normalized_unit_price": D("300"), "is_out_of_stock": 0, "badge_info": None, "extraction_source": "SELECTOR", "region_code": "SEOUL"}])
    world.store.save_match("SHOPB", "O1", 3, "SAME", "kim", NOW)
    p = date(2026, 10, 26)
    rows = []
    cand = {"사과 세트": 9000, "두리안": 8000, "양파 특가": 7000, "배추 김치": 6000}
    for i, (k, v) in enumerate(cand.items()):
        rows += [(p, k, "searches", v), (p, k, "buyers", v * (0.010 + 0.002 * i))]
    for i in range(8):
        rows += [(p, f"filler{i}", "searches", 100 + 50 * i), (p, f"filler{i}", "buyers", (100 + 50 * i) * 0.12)]
    seed_search(world, rows)
    return world


def test_gap_diagnosis_orders_the_causes(gap_world):
    rep = mi_report(gap_world.store, gap_world.s, None, False, "en", NOW)
    by = {g["keyword"]: g for g in rep["gap"]}
    assert set(by) == {"사과 세트", "두리안", "양파 특가", "배추 김치"}                       # fillers are neither high-volume nor low-conversion
    assert by["두리안"]["primary_cause"] == "NO_PRODUCT"
    assert by["사과 세트"]["primary_cause"] == "OUT_OF_STOCK" and by["사과 세트"]["evidence"]["stockout_ratio"] == 1.0
    assert by["양파 특가"]["primary_cause"] == "PRICE_DISADVANTAGE" and by["양파 특가"]["evidence"]["price_gap_pct"] == pytest.approx(66.7, abs=0.1)
    assert by["배추 김치"]["primary_cause"] == "OTHER"
    assert all({"changed", "why", "action"} <= set(g) for g in rep["gap"])
    assert "out of stock" in by["사과 세트"]["why"] or "stock" in by["사과 세트"]["why"]


def test_gap_secondary_causes_and_korean_text(gap_world):
    # make the onion also out of stock: stock-out is first in order, price disadvantage becomes contributing
    from sqlalchemy import text
    with gap_world.e.begin() as c:
        c.execute(text("UPDATE int_operations_inventory SET stock_qty = 0 WHERE internal_sku='ONION-1'"))
    rep = mi_report(gap_world.store, gap_world.s, None, False, "ko", NOW)
    onion = next(g for g in rep["gap"] if g["keyword"] == "양파 특가")
    assert onion["primary_cause"] == "OUT_OF_STOCK" and onion["contributing_causes"] == ["PRICE_DISADVANTAGE"]
    assert "품절" in onion["why"] and "가격 열위" in onion["why"]


def test_gap_needs_enough_keywords(gap_world):
    from sqlalchemy import text
    with gap_world.e.begin() as c:
        c.execute(text("DELETE FROM mi_observations WHERE keyword LIKE 'filler%'"))
    assert mi_report(gap_world.store, gap_world.s, None, False, "en", NOW)["gap"] == []          # 4 keywords < gap_min_keywords


def test_gap_is_filtered_by_category_scope(gap_world):
    rep = mi_report(gap_world.store, gap_world.s, {"Other"}, False, "en", NOW)
    assert rep["gap"] == []


def test_new_gap_messages_exist_in_both_languages():
    en = {k for k in MESSAGES["en"] if k.startswith("gap.")}
    assert en and en == {k for k in MESSAGES["ko"] if k.startswith("gap.")}
    for k in en:
        import re
        assert set(re.findall(r"{(\w+)}", MESSAGES["en"][k])) == set(re.findall(r"{(\w+)}", MESSAGES["ko"][k])), k
