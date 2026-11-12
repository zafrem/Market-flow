"""Revised price/margin specification, P0 (SRS F-08): disposal cost, price-match simulation with BEP, effective purchase
price, human-confirmed SAME matches, alerts without figures, audited comparison export."""
import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from marketflow import cli, matching
from marketflow.db import init_sqlite, make_engine
from marketflow.decision import service
from marketflow.decision.calc import disposal_cost, net_margin, price_floor, simulate_price_match
from marketflow.decision.config import DecisionConfig, load_decision_config
from marketflow.decision.service import Actor, WorkflowError
from marketflow.erp import import_erp
from marketflow.repository import SqlRepository
from marketflow.scrape.extract import extract_listings, parse_bonus
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.web.app import create_app
from marketflow.web.repo import AppRepository
from marketflow.web.security import hash_password
from marketflow.rules import current_rules

T0 = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
D = Decimal
MD, BUYER, ADMIN = Actor("kim", "MD"), Actor("lee", "BUYER"), Actor("root", "ADMIN")
CSV = """internal_sku,item_name,channel_name,purchase_cost,fixed_cost,logistics_cost,channel_fee_rate,current_selling_price,stock_qty,daily_sales_qty,remaining_days,pack_std_units,disposal_cost
APPLE-1,Apple,SHOPA,5000,500,1000,11.5,10000,100,10,,12,
"""
COST = {"purchase": D(5000), "packaging": D(500), "logistics": D(1000), "promo": D(0), "fee_pct": D("11.5"), "disposal": D(0)}


# ---- margin formula: disposal cost ---------------------------------------------------------------------------
def test_disposal_cost_reduces_the_margin_and_raises_the_floor():
    base, _ = net_margin(10000, D(5000), D(500), D(1000), D(0), D("11.5"), True, D("0.10"))
    with_disposal, rate = net_margin(10000, D(5000), D(500), D(1000), D(0), D("11.5"), True, D("0.10"), disposal=D(250))
    assert base - with_disposal == D("250.00") and rate == D("13.10")
    cfg = DecisionConfig()
    assert price_floor(D(5000), D(500), D(1000), D(0), D("11.5"), True, cfg, disposal=D(250)) > price_floor(D(5000), D(500), D(1000), D(0), D("11.5"), True, cfg)


def test_disposal_uses_the_sku_figure_else_the_category_rate():
    assert disposal_cost(D(5000), D(120), D(5)) == D(120)            # SKU data wins
    assert disposal_cost(D(5000), None, D(5)) == D(250)              # category rate x purchase cost
    assert disposal_cost(D(5000), D(0), D(5)) == D(0)                # an explicit zero is a real figure
    assert load_decision_config({"disposal_rate_pct": 4, "Fresh": {"disposal_rate_pct": 6}}, "Fresh").disposal_rate_pct == D(6)
    with pytest.raises(ValueError, match="require_confirmed_match"):
        load_decision_config({"require_confirmed_match": "yes"})


# ---- price-match simulation and BEP --------------------------------------------------------------------------
def test_bep_is_the_volume_that_earns_todays_total_margin():
    s = simulate_price_match(10000, 9000, D(70), COST, True, D("0.10"))
    m_now, _ = net_margin(10000, D(5000), D(500), D(1000), D(0), D("11.5"), True, D("0.10"))
    m_new, _ = net_margin(9000, D(5000), D(500), D(1000), D(0), D("11.5"), True, D("0.10"))
    assert s["bep_status"] == "OK" and D(s["expected_margin_amount"]) == m_new
    assert int(s["bep_units"]) == int((D(70) * m_now / m_new).to_integral_value(rounding="ROUND_CEILING"))
    assert int(s["bep_units"]) > 70                                   # a cut must sell more to break even on total margin


def test_bep_edge_cases():
    assert simulate_price_match(10000, 7000, D(70), COST, True, D("0.10"))["bep_status"] == "UNACHIEVABLE"      # cut below cost
    up = simulate_price_match(10000, 11000, D(70), COST, True, D("0.10"))
    assert up["bep_status"] == "NOT_A_CUT" and up["bep_units"] is None
    assert simulate_price_match(7500, 7000, D(70), COST, True, D("0.10"))["bep_status"] in {"UNACHIEVABLE", "NO_CURRENT_MARGIN"}
    assert simulate_price_match(10000, 10000, D(70), COST, True, D("0.10"))["bep_status"] == "NOT_A_CUT"


# ---- ERP import: disposal column ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path):
    tp = FixedTimeProvider(T0)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'p0.db'}", jwt_secret="j" * 48, inbox_dir=str(tmp_path / "inbox"),
                 decision={"disposal_rate_pct": 2})
    e = make_engine(s.database_url)
    init_sqlite(e)
    repo = AppRepository(e)
    f = tmp_path / "inv.csv"; f.write_text(CSV, encoding="utf-8")
    assert import_erp(f, repo, s, tp).loaded == 1
    return type("E", (), dict(tp=tp, s=s, e=e, repo=repo, tmp=tmp_path))


def listing(env, code="X1", price=8400, **extra):
    SqlRepository(env.e).insert_retail([{
        "scraped_at": T0.replace(tzinfo=None) - timedelta(hours=1), "platform": "SHOPA", "std_item_id": 1,
        "match_confidence": D("0.9"), "channel_item_code": code, "product_title": "사과 1.2kg", "original_price": price,
        "sale_price": price, "normalized_unit_price": D(price) / 12, "is_out_of_stock": 0, "badge_info": None,
        "extraction_source": "SELECTOR", "region_code": "SEOUL", **extra}])


def test_erp_disposal_column_is_stored_and_blank_means_category_rate(env):
    inv = env.repo.inventory_on(T0.date())
    assert inv[0]["disposal_cost"] is None                              # blank cell -> NULL -> category rate (2% of 5000 = 100)
    f = env.tmp / "inv2.csv"
    f.write_text(CSV.replace("12,\n", "12,333\n"), encoding="utf-8")
    assert import_erp(f, env.repo, env.s, env.tp).loaded == 1
    assert env.repo.inventory_on(T0.date())[0]["disposal_cost"] == D("333")
    # the floor was computed with the disposal cost included
    floor_with = env.repo.inventory_on(T0.date())[0]["min_price_floor"]
    no_disposal = price_floor(D(5000), D(500), D(1000), D(0), D("11.5"), True, DecisionConfig())
    assert floor_with > no_disposal


def test_recommendation_carries_the_match_simulation(env):
    listing(env)
    env.repo.save_match("SHOPA", "X1", 1, "SAME", "kim", T0.replace(tzinfo=None))
    made = service.run_decisions(env.repo, env.s, env.tp)
    sim = made[0][1].flags["metrics"]["match_simulation"]
    assert sim["target_price"] == 8400 and sim["bep_status"] == "OK" and int(sim["bep_units"]) > 70
    assert "margin_amount" in made[0][1].flags["metrics"]
    # disposal (2% of 5000 = 100) is part of the margin shown on the recommendation
    plain = net_margin(10000, D(5000), D(500), D(1000), D(0), D("11.5"), True, D("0.10"))[0]
    assert D(made[0][1].flags["metrics"]["margin_amount"]) == plain - 100


# ---- confirmed SAME matches only ---------------------------------------------------------------------------
@pytest.mark.unconfirmed
def test_unconfirmed_listing_does_not_drive_a_recommendation(env):
    listing(env)
    assert env.repo.competitor_snapshots(1, datetime(2026, 1, 1), confirmed_only=True) == []
    made = service.run_decisions(env.repo, env.s, env.tp)
    assert all(r.reason_code != "COMPETITIVE_MATCH" for _, r in made)


@pytest.mark.unconfirmed
def test_confirming_a_same_match_makes_it_count_and_grades_are_respected(env):
    listing(env)
    matching.set_match(env.repo, env.tp, MD, "SHOPA", "X1", 1, "SIMILAR")
    assert env.repo.competitor_snapshots(1, datetime(2026, 1, 1), confirmed_only=True) == []        # similar is not "the same product"
    matching.set_match(env.repo, env.tp, MD, "SHOPA", "X1", 1, "SAME")
    assert len(env.repo.competitor_snapshots(1, datetime(2026, 1, 1), confirmed_only=True)) == 1
    made = service.run_decisions(env.repo, env.s, env.tp)
    assert [r.reason_code for _, r in made] == ["COMPETITIVE_MATCH"]
    actions = [(a["action"], a["actor"]) for a in env.repo.audit_entries()]
    assert actions.count(("MATCH_SET", "kim")) == 2


@pytest.mark.unconfirmed
def test_the_rule_can_be_switched_off_per_deployment(env):
    listing(env)
    s = Settings(database_url=env.s.database_url, decision={"require_confirmed_match": False})
    assert [r.reason_code for _, r in service.run_decisions(env.repo, s, env.tp)] == ["COMPETITIVE_MATCH"]


@pytest.mark.unconfirmed
def test_matching_permissions_and_validation(env):
    listing(env)
    with pytest.raises(PermissionError):
        matching.set_match(env.repo, env.tp, BUYER, "SHOPA", "X1", 1, "SAME")
    with pytest.raises(WorkflowError, match="no scraped listing"):
        matching.set_match(env.repo, env.tp, MD, "SHOPA", "NOPE", 1, "SAME")
    with pytest.raises(WorkflowError, match="not in the catalog"):
        matching.set_match(env.repo, env.tp, MD, "SHOPA", "X1", 999, "SAME")
    with pytest.raises(WorkflowError, match="grade"):
        matching.set_match(env.repo, env.tp, MD, "SHOPA", "X1", 1, "MAYBE")
    matching.set_match(env.repo, env.tp, MD, "SHOPA", "X1", 1, "SAME")
    matching.remove_match(env.repo, env.tp, MD, "SHOPA", "X1")
    assert env.repo.confirmed_listings() == set()
    with pytest.raises(WorkflowError):
        matching.remove_match(env.repo, env.tp, MD, "SHOPA", "X1")


@pytest.mark.unconfirmed
def test_cli_lists_and_confirms_matches(env):
    listing(env)
    SqlRepository(env.e).propose_matches([{"platform": "SHOPA", "channel_item_code": "X1", "std_item_id": 1, "match_confidence": D("0.9")}], T0)
    out = io.StringIO()
    assert cli.main(["matches", "--unconfirmed"], settings=env.s, tp=env.tp, out=out) == 0 and "UNCONFIRMED" in out.getvalue()
    assert cli.main(["match", "SHOPA", "X1", "1", "--as", "lee:BUYER"], settings=env.s, tp=env.tp, out=io.StringIO()) == 2
    assert cli.main(["match", "SHOPA", "X1", "1", "--as", "kim:MD"], settings=env.s, tp=env.tp, out=io.StringIO()) == 0
    out = io.StringIO()
    cli.main(["matches"], settings=env.s, tp=env.tp, out=out)
    assert "confirmed" in out.getvalue() and "UNCONFIRMED" not in out.getvalue()


# ---- effective purchase price --------------------------------------------------------------------------------
def test_bonus_text_parsing():
    assert parse_bonus("1+1") == 1.0 and parse_bonus("2 + 1 행사") == 0.5 and parse_bonus("") == 0 and parse_bonus(None) == 0 and parse_bonus("0+1") == 0


def test_extractor_reads_coupon_shipping_and_bonus_when_selectors_exist():
    html = ('<ul><li class="i"><b class="t">사과 1.2kg</b><i class="p">9,000원</i><u class="c">1,000원 쿠폰</u>'
            '<s class="s">3,000원</s><em class="b">1+1</em></li></ul>')
    sel = {"item": "li.i", "fields": {"title": {"css": ".t"}, "sale_price": {"css": ".p"}, "coupon": {"css": ".c"},
                                      "shipping_fee": {"css": ".s"}, "bonus": {"css": ".b"}}}
    got = extract_listings(html, sel, current_rules())[0]
    assert (got["coupon_discount"], got["shipping_fee"], got["bonus_qty"]) == (1000, 3000, 1.0)


@pytest.mark.unconfirmed
def test_effective_unit_price_drives_comparison(env):
    # shown 8,400 (700 / 100g); coupon 1,000 and shipping 3,000 and 1+1: pays 10,400 for 2 packs -> 5,200 per pack-equivalent
    listing(env, effective_unit_price=D("433.33"), coupon_discount=1000, shipping_fee=3000, bonus_qty=D(1))
    env.repo.save_match("SHOPA", "X1", 1, "SAME", "kim", T0.replace(tzinfo=None))
    snap = env.repo.competitor_snapshots(1, datetime(2026, 1, 1), confirmed_only=True)[0]
    assert snap["normalized_unit_price"] == D("433.33") and snap["listed_unit_price"] == D("700")
    plain = SqlRepository(env.e)
    listing(env, code="X2")                                              # no extra terms: effective == shown
    row = env.repo.competitor_snapshots(1, datetime(2026, 1, 1))
    x2 = next(r for r in row if r["channel_item_code"] == "X2")
    assert x2["normalized_unit_price"] == x2["listed_unit_price"]


# ---- API: matches, comparison sheet, export, alert text --------------------------------------------------------
@pytest.fixture()
def api(env):
    for key, role, cats in [("admin", "ADMIN", []), ("md", "MD", [("Fresh", "Fruit")]), ("md_veg", "MD", [("Fresh", "Vegetable")]),
                            ("buyer", "BUYER", [("Fresh", "Fruit")])]:
        env.repo.set_assignments(env.repo.create_user(f"{key}@x.com", key, role, hash_password("correct-horse-battery")), cats)
    client = TestClient(create_app(env.s, env.e, env.tp))

    def hdr(who):
        r = client.post("/auth/login", json={"email": f"{who}@x.com", "password": "correct-horse-battery"})
        return {"Authorization": "Bearer " + r.json()["access_token"]}
    return type("A", (), dict(c=client, h=hdr, env=env))


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)


@pytest.mark.unconfirmed
def test_matches_api_scope_roles_and_audit(api):
    env = api.env
    listing(env)
    SqlRepository(env.e).propose_matches([{"platform": "SHOPA", "channel_item_code": "X1", "std_item_id": 1, "match_confidence": D("0.9")}], T0)
    got = api.c.get("/matches?unconfirmed=true", headers=api.h("md")).json()["matches"]
    assert [(m["channel_item_code"], m["confirmed"]) for m in got] == [("X1", False)]
    assert api.c.get("/matches", headers=api.h("md_veg")).json()["matches"] == []              # other category: not visible
    assert api.c.get("/matches", headers=api.h("buyer")).status_code == 403
    assert api.c.put("/matches/SHOPA/X1", json={"std_item_id": 1}, headers=api.h("md_veg")).status_code == 404
    assert api.c.put("/matches/SHOPA/X1", json={"std_item_id": 1, "grade": "NOPE"}, headers=api.h("md")).status_code == 422
    r = api.c.put("/matches/SHOPA/X1", json={"std_item_id": 1}, headers=api.h("md"))
    assert r.status_code == 200 and r.json()["confirmed"] is True
    assert api.c.get("/matches", headers=api.h("md")).json()["matches"][0]["confirmed"] is True
    assert api.c.delete("/matches/SHOPA/X1", headers=api.h("md_veg")).status_code == 404
    assert api.c.delete("/matches/SHOPA/X1", headers=api.h("md")).status_code == 200
    assert {"MATCH_SET", "MATCH_REMOVED"} <= {a["action"] for a in env.repo.audit_entries()}


@pytest.mark.unconfirmed
def test_matrix_shows_simulation_to_md_but_not_buyer_and_counts_unconfirmed(api):
    env = api.env
    listing(env)
    md = api.c.get("/matrix", headers=api.h("md")).json()["rows"][0]
    assert md["competitors"]["listings"] == 0 and md["competitors"]["unconfirmed"] == 1 and md["internal"]["match_simulation"] is None
    env.repo.save_match("SHOPA", "X1", 1, "SAME", "kim", T0.replace(tzinfo=None))
    md = api.c.get("/matrix", headers=api.h("md")).json()["rows"][0]
    sim = md["internal"]["match_simulation"]
    assert md["competitors"]["unconfirmed"] == 0 and sim["target_price"] == 8400 and sim["bep_status"] == "OK" and "disposal_cost" in md["internal"]
    buyer = api.c.get("/matrix", headers=api.h("buyer")).json()["rows"][0]
    assert "match_simulation" not in buyer["internal"] and "disposal_cost" not in buyer["internal"]
    assert buyer["competitors"]["lowest_pack_price"] == "8400.00"                       # prices stay visible, margins do not


def test_export_is_audited_decider_only_and_safe(api):
    env = api.env
    listing(env)
    assert api.c.get("/matrix/export", headers=api.h("buyer")).status_code == 403
    r = api.c.get("/matrix/export", headers=api.h("md"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:4] == ["item", "category", "sku", "channel"] and rows[1][0] == "Apple" and rows[1][2] == "APPLE-1"
    entry = [a for a in env.repo.audit_entries() if a["action"] == "MATRIX_EXPORTED"]
    assert len(entry) == 1 and entry[0]["actor"] == "md@x.com"
    assert api.c.get("/matrix/export", headers=api.h("md_veg")).text.count("\n") == 1          # header only: nothing in scope


def test_export_neutralises_spreadsheet_formulas(api):
    env = api.env
    with env.e.begin() as c:
        c.execute(text("UPDATE master_products SET item_name='=HYPERLINK(1)' WHERE std_item_id=1"))
    rows = list(csv.reader(io.StringIO(api.c.get("/matrix/export", headers=api.h("md")).text)))
    assert rows[1][0] == "'=HYPERLINK(1)"


@pytest.mark.unconfirmed
async def test_scraper_stores_effective_price_and_proposes_unconfirmed_matches(env, isolated_rules):
    from conftest import FIX
    from marketflow.llm import MockLLM
    from marketflow.transport import FixtureTransport
    from marketflow.worker import run_retail
    isolated_rules.promote(["retail", "SHOPA", "urls"], ["https://shop-a.example/fruit"], "test")
    isolated_rules.promote(["retail", "SHOPA", "legal_review_ref"], "LEGAL-TEST-1", "test")
    html = (FIX / "html" / "shop_a.html").read_text(encoding="utf-8")
    await run_retail(env.e, FixtureTransport({"https://shop-a.example/fruit": html}), env.tp, MockLLM())
    with env.e.connect() as c:
        n_rows = c.execute(text("SELECT COUNT(*) FROM ext_retail_prices")).scalar()
        bad = c.execute(text("SELECT COUNT(*) FROM ext_retail_prices WHERE effective_unit_price IS NULL OR effective_unit_price <> normalized_unit_price")).scalar()
        proposed = c.execute(text("SELECT COUNT(*) FROM product_channel_map WHERE reviewed_at IS NULL AND match_method='RULE'")).scalar()
    assert n_rows > 0 and bad == 0                       # no coupon/shipping on the fixture page: effective == shown
    assert proposed > 0 and env.repo.confirmed_listings() == set()


def test_alerts_from_a_real_run_contain_no_figures(env):
    from marketflow.notify import alerts_from_recommendations
    listing(env, price=5000)                                  # competitor far below the floor -> reverse margin
    env.repo.save_match("SHOPA", "X1", 1, "SAME", "kim", T0.replace(tzinfo=None))
    made = service.run_decisions(env.repo, env.s, env.tp)
    alerts = alerts_from_recommendations(made, "https://mf.example.com")
    assert alerts and all(a.text.endswith("https://mf.example.com") for a in alerts)
    assert not any(ch in a.text for a in alerts for ch in "₩%")
