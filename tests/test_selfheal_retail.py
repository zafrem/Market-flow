"""SRS §6.3 F-02-1/F-02-2: mutated class names -> selectors fail -> fault -> LLM fallback -> valid rows in the DB."""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from conftest import FIX
from marketflow.db import init_sqlite, make_engine
from marketflow.llm import MockLLM
from marketflow.scrape.extract import ScrapeExtractionFault, extract_listings
from marketflow.scrape.retail import ComplianceError
from marketflow.scrape.selfheal import SelfHealingExtractor, validate_listing
from marketflow.time_provider import FixedTimeProvider
from marketflow.transport import FixtureTransport
from marketflow.rules import current_rules
from marketflow.worker import run_retail

TP = FixedTimeProvider(datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc))
ORIGINAL = (FIX / "html" / "shop_a.html").read_text(encoding="utf-8")
MUTATED = (FIX / "html" / "shop_a_mutated.html").read_text(encoding="utf-8")
URL = "https://shop-a.example/fruit"
LLM_ITEMS = {"items": [
    {"channel_item_code": "A1", "product_title": "성주 사과 1.2kg(600g x 2)", "sale_price": 15900, "original_price": 19900, "badge_info": "로켓프레시"},
    {"channel_item_code": "A2", "product_title": "국산 양파 3kg", "sale_price": 4980, "is_out_of_stock": True},
    {"channel_item_code": "A3", "product_title": "수입 두리안 1kg", "sale_price": 12000},
    {"channel_item_code": "A4", "product_title": "사과 선물세트 박스", "sale_price": 30000},
]}


@pytest.fixture()
def cleared(isolated_rules):
    """Config for tests: URL set and legal review recorded (the production default is blocked)."""
    isolated_rules.promote(["retail", "SHOPA", "urls"], [URL], "test")
    isolated_rules.promote(["retail", "SHOPA", "legal_review_ref"], "LEGAL-TEST-1", "test")
    return isolated_rules


@pytest.fixture()
def engine(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    init_sqlite(e)
    return e


def stored(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT channel_item_code, product_title, sale_price, original_price, normalized_unit_price, "
                              "is_out_of_stock, badge_info, extraction_source, region_code, scraped_at, match_confidence "
                              "FROM ext_retail_prices ORDER BY channel_item_code")).mappings().all()


def t(html):
    return FixtureTransport({"shop-a.example": html})


async def test_selectors_path_extracts_matches_and_normalizes(cleared, engine):
    llm = MockLLM(LLM_ITEMS)
    res = (await run_retail(engine, t(ORIGINAL), TP, llm))["SHOPA"]
    assert llm.calls == 0 and res.source == "SELECTOR" and not res.candidate_pending
    assert res.stored == 2
    assert res.unmatched == ["수입 두리안 1kg"]               # not in catalog -> review queue
    assert res.unparseable == ["사과 선물세트 박스"]           # no resolvable quantity -> review queue
    a1, a2 = stored(engine)
    assert (a1["product_title"], a1["sale_price"], a1["original_price"], a1["badge_info"]) == ("성주 사과 1.2kg(600g x 2)", 15900, 19900, "로켓프레시")
    assert float(a1["normalized_unit_price"]) == 1325.0 and a1["extraction_source"] == "SELECTOR"
    assert a1["region_code"] == "SEOUL" and a1["scraped_at"].startswith("2026-11-03 01:00") and float(a1["match_confidence"]) == 0.9
    assert (a2["is_out_of_stock"], a2["original_price"], float(a2["normalized_unit_price"])) == (1, 4980, 166.0)


async def test_mutated_page_faults_then_llm_fallback_stores_valid_rows(cleared, engine):
    with pytest.raises(ScrapeExtractionFault):                       # F-02-1: base selectors really are broken
        extract_listings(MUTATED, current_rules()["retail"]["SHOPA"]["selectors"], current_rules())
    llm = MockLLM(LLM_ITEMS)
    res = (await run_retail(engine, t(MUTATED), TP, llm))["SHOPA"]
    assert llm.calls == 1 and res.source == "LLM" and res.stored == 2 and res.quarantined == 0
    rows = stored(engine)
    assert {r["extraction_source"] for r in rows} == {"LLM"}
    assert [(r["channel_item_code"], r["sale_price"]) for r in rows] == [("A1", 15900), ("A2", 4980)]
    assert res.candidate_pending                                     # verified new selectors await approval


async def test_run_cache_prevents_repeat_llm_calls_in_same_run(cleared, engine):
    cleared.promote(["retail", "SHOPA", "urls"], [URL, URL + "?page=2", URL + "?page=3"], "test")
    llm = MockLLM(LLM_ITEMS)
    res = (await run_retail(engine, t(MUTATED), TP, llm))["SHOPA"]
    assert llm.calls == 1 and res.stored == 6                        # pages 2-3 healed from cached candidate


async def test_candidate_not_persisted_until_human_approval(cleared, engine, tmp_path):
    llm = MockLLM(LLM_ITEMS)
    ex = SelfHealingExtractor(llm)
    await run_retail(engine, t(MUTATED), TP, llm, extractor=ex)
    persisted = json.loads((tmp_path / "overrides.json").read_text())["retail"]["SHOPA"]
    assert "selectors" not in persisted                              # only the test's urls/legal ref; fix is pending
    assert "SHOPA" in ex.pending


async def test_after_approval_next_run_uses_selectors_without_llm(cleared, engine, tmp_path):
    llm = MockLLM(LLM_ITEMS)
    ex = SelfHealingExtractor(llm)
    await run_retail(engine, t(MUTATED), TP, llm, extractor=ex)
    ex.approve("SHOPA", "md@example.com")
    sel = json.loads((tmp_path / "overrides.json").read_text())["retail"]["SHOPA"]["selectors"]
    assert sel["item"] == "article.card_x9" and sel["fields"]["title"] == {"css": "h3.t_k2"}
    llm2 = MockLLM(LLM_ITEMS)                                        # fresh process: no run cache
    res = (await run_retail(engine, t(MUTATED), TP, llm2))["SHOPA"]
    assert llm2.calls == 0 and res.source == "SELECTOR" and res.stored == 2


async def test_synthesized_selectors_are_verified_not_trusted(cleared):
    llm = MockLLM({"items": [{"product_title": "성주 사과 1.2kg(600g x 2)", "sale_price": 15900}]})  # 1 of 4 items
    ex = SelfHealingExtractor(llm)
    healed = await ex.extract("SHOPA", MUTATED)
    assert healed.source == "LLM" and not healed.candidate_pending and not ex.pending   # candidate would yield 4 != 1


@pytest.mark.parametrize("override,why", [
    ({"sale_price": 0}, "positive integer"),
    ({"sale_price": -5}, "positive integer"),
    ({"sale_price": 25000, "original_price": 19900}, "> original_price"),
    ({"sale_price": 60000, "original_price": 60000}, "deviates"),
    ({"product_title": ""}, "empty title"),
])
def test_validate_listing_rules(override, why):
    item = {"product_title": "사과", "sale_price": 15900, "original_price": 19900, **override}
    assert why in validate_listing(item, 50, 15000)
    assert validate_listing({"product_title": "사과", "sale_price": 15900, "original_price": 19900}, 50, 15000) is None


async def test_llm_garbage_is_quarantined_not_stored(cleared, engine):
    bad = {"items": [
        {"channel_item_code": "A1", "product_title": "성주 사과 1.2kg(600g x 2)", "sale_price": 15900, "original_price": 19900},
        {"channel_item_code": "A2", "product_title": "국산 양파 3kg", "sale_price": 0},                  # invalid
        {"channel_item_code": "A3", "product_title": "수입 두리안 1kg", "sale_price": 99999},            # not on page
        {"channel_item_code": "A9", "product_title": "존재하지 않는 상품 1kg", "sale_price": 4980},       # title not on page
    ]}
    res = (await run_retail(engine, t(MUTATED), TP, MockLLM(bad)))["SHOPA"]
    assert res.quarantined == 3 and res.stored == 1
    assert [r["channel_item_code"] for r in stored(engine)] == ["A1"]


async def test_all_invalid_llm_output_raises_fault_and_stores_nothing(cleared, engine):
    res = await run_retail(engine, t(MUTATED), TP, MockLLM({"items": [{"product_title": "x", "sale_price": 0}]}))
    assert isinstance(res["SHOPA"], ScrapeExtractionFault) and stored(engine) == []
    res = await run_retail(engine, t(MUTATED), TP, MockLLM())        # empty LLM answer
    assert isinstance(res["SHOPA"], ScrapeExtractionFault)


async def test_price_jump_vs_last_accepted_is_quarantined(cleared):
    ex = SelfHealingExtractor(MockLLM(LLM_ITEMS), last_price=lambda p, c: 3000 if c == "A1" else None)  # 15900 is +430%
    healed = await ex.extract("SHOPA", MUTATED)
    assert [i["channel_item_code"] for i in healed.listings] == ["A2", "A3", "A4"]
    assert healed.quarantined and "deviates" in healed.quarantined[0][1]


async def test_consecutive_faults_escalate_on_third(cleared):
    alerts = []
    ex = SelfHealingExtractor(MockLLM(LLM_ITEMS), alert=alerts.append)
    for _ in range(3):
        await ex.extract("SHOPA", MUTATED)
    assert "ESCALATE" not in alerts[0] and any("consecutive=3 - ESCALATE" in a for a in alerts)
    await ex.extract("SHOPA", ORIGINAL)                              # healthy page resets the counter
    assert ex.consecutive_faults["SHOPA"] == 0


async def test_compliance_gate_blocks_unreviewed_scrape_connector(isolated_rules, engine):
    isolated_rules.promote(["retail", "SHOPA", "urls"], [URL], "test")      # urls set, legal_review_ref still empty
    tr = t(ORIGINAL)
    res = await run_retail(engine, tr, TP, MockLLM())
    assert isinstance(res["SHOPA"], ComplianceError) and "legal_review_ref" in str(res["SHOPA"])
    assert tr.calls == [] and stored(engine) == []                          # nothing fetched, nothing stored


async def test_platform_without_urls_is_not_run(isolated_rules, engine):
    assert await run_retail(engine, t(ORIGINAL), TP, MockLLM()) == {}


def test_invalid_selector_cannot_be_promoted(cleared):
    with pytest.raises(ValueError, match="invalid selector"):
        cleared.promote(["retail", "SHOPA", "selectors", "item"], "li..[", "attacker")
    with pytest.raises(Exception):
        cleared.promote(["retail", "SHOPA", "selectors", "fields", "sale_price"], None, "x")
