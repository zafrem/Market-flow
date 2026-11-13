"""The unattended system: a simulated day through the real worker wiring, review queue, selector approval, alerts."""
import io
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import FIX
from marketflow import cli, curation
from marketflow.db import init_sqlite, make_engine
from marketflow.decision.service import Actor, WorkflowError
from marketflow.internal_data import import_internal
from marketflow.llm import MockLLM
from marketflow.notify import Notifier
from marketflow.ops_repo import Repo
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.transport import FixtureTransport
from marketflow.web.app import create_app
from marketflow.web.security import hash_password
from marketflow.crawlers.base import to_time
from marketflow.crawlers.service import CrawlerService
from marketflow.schedule import next_run
from marketflow.worker import build_scheduler, run_retail

START = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)            # 2026-11-03 05:00 KST
ORIGINAL = (FIX / "html" / "shop_a.html").read_text(encoding="utf-8")
MUTATED = (FIX / "html" / "shop_a_mutated.html").read_text(encoding="utf-8")
LLM_ITEMS = {"items": [
    {"channel_item_code": "A1", "product_title": "성주 사과 1.2kg(600g x 2)", "sale_price": 15900, "original_price": 19900},
    {"channel_item_code": "A2", "product_title": "국산 양파 3kg", "sale_price": 4980, "is_out_of_stock": True},
    {"channel_item_code": "A3", "product_title": "수입 두리안 1kg", "sale_price": 12000},
    {"channel_item_code": "A4", "product_title": "사과 선물세트 박스", "sale_price": 30000}]}
CSV = """internal_sku,item_name,channel_name,purchase_cost,fixed_cost,logistics_cost,channel_fee_rate,current_selling_price,stock_qty,daily_sales_qty,remaining_days,pack_std_units
APPLE-1,Apple,SHOPA,5000,500,1000,11.5,10000,100,10,,12
ONION-1,Onion,SHOPA,2000,300,700,11.5,5000,300,100,2,10
"""
HOOK = "https://hooks.example/services/T/B/SECRET"


class Webhook:
    def __init__(self):
        self.posts = []

    async def __call__(self, url, payload):
        self.posts.append(payload["text"])
        return 200


@pytest.fixture()
def world(tmp_path, isolated_rules):
    isolated_rules.promote(["retail", "SHOPA", "urls"], ["https://shop-a.example/fruit"], "test")
    isolated_rules.promote(["retail", "SHOPA", "legal_review_ref"], "LEGAL-1", "test")
    tp = FixedTimeProvider(START)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'a.db'}", kamis_api_key="K", kamis_api_id="ID", alert_webhook_url=HOOK, jwt_secret="j" * 40)
    e = make_engine(s.database_url); init_sqlite(e)
    repo = Repo(e)
    f = tmp_path / "inv.csv"; f.write_text(CSV, encoding="utf-8")
    assert import_internal(f, repo, s, tp, date(2026, 11, 3)).loaded == 2
    hook, sleeps = Webhook(), []

    async def sleep(sec):
        sleeps.append(sec); tp.set(tp.now() + timedelta(seconds=sec))

    def make(html, llm=None, kamis=None):
        tr = FixtureTransport({"kamis.or.kr": kamis or FIX / "kamis_daily.json", "shop-a.example": html})
        notifier = Notifier(HOOK, repo, tp, post=hook, sleep=sleep)
        llm = llm or MockLLM(LLM_ITEMS)
        sched = build_scheduler(e, s, tp, notifier, sleep=sleep)
        svc = CrawlerService(e, s, tp, tr, lambda: llm, notifier)
        for cid in svc.registry:                                          # this day only exercises the price crawlers
            if cid not in {"benchmark_kamis", "competitor"}:
                svc.update(cid, "test", False, None, None)
        return Day(sched, svc, tp), tr
    return type("W", (), dict(e=e, s=s, tp=tp, repo=repo, hook=hook, make=staticmethod(make), rules=isolated_rules, tmp=tmp_path))


class Day:
    """The worker's two loops on one simulated clock: fixed-time jobs (decide) and per-crawler schedules."""
    def __init__(self, sched, svc, tp):
        self.sched, self.svc, self.tp = sched, svc, tp

    async def step(self):
        sched_when, _ = self.sched.next_due()
        crawler_when = min(next_run(self.tp.now(), [to_time(t) for t in self.svc.effective(c)["times"]])
                           for c in self.svc.registry.values() if self.svc.effective(c)["enabled"])
        if crawler_when <= sched_when:
            self.tp.set(crawler_when)
            return await self.svc.dispatch_once()
        return await self.sched.step()


def count(w, table):
    with w.e.connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


async def test_simulated_day_runs_everything_unattended(world):
    sch, tr = world.make(ORIGINAL)
    ran = [await sch.step() for _ in range(8)]
    assert ran == [["benchmark_kamis"], ["competitor"], ["decide"], ["benchmark_kamis"], ["competitor"], ["decide"], ["competitor"], ["decide"]]
    assert count(world, "ext_public_benchmarks") == 2                  # KAMIS apple+onion, idempotent across 2 runs
    assert count(world, "ext_retail_prices") == 6                      # 3 scrapes x 2 matched listings (append-only snapshots)
    recs = world.repo.list_recommendations()
    assert sorted(r["reason_code"] for r in recs) == ["COMPETITIVE_MATCH", "SPOILAGE_RISK"]    # created once, then de-duplicated
    spoil = [t for t in world.hook.posts if "Spoilage risk ONION-1/SHOPA" in t]
    assert len(spoil) == 1 and spoil[0].startswith(":large_orange_circle:")                   # alerted exactly once all day
    assert all("SECRET" not in t for t in world.hook.posts)


async def test_review_queue_filled_and_deduplicated_across_runs(world):
    sch, _ = world.make(ORIGINAL)
    for _ in range(6):
        await sch.step()
    items = {(r["kind"], r["source"], r["item_key"]) for r in world.repo.list_review()}
    assert items == {("UNMATCHED_TITLE", "SHOPA", "수입 두리안 1kg"), ("UNPARSEABLE_TITLE", "SHOPA", "사과 선물세트 박스"),
                     ("UNMAPPED_BENCHMARK", "KAMIS", "두리안/상품"), ("UNCONVERTIBLE_BENCHMARK", "KAMIS", "무/상품")}
    assert count(world, "review_queue") == 4                           # three retail runs did not multiply them


async def test_failed_job_raises_alert_and_day_continues(world):
    sch, tr = world.make(ORIGINAL)
    original_get = tr.get_text

    async def flaky(url, params=None):
        if "shop-a" in url:
            raise ConnectionError("shop down")
        return await original_get(url, params)
    tr.get_text = flaky
    await sch.step(); await sch.step(); await sch.step()               # kamis, competitor (fails), decide
    assert any("Retail scrape SHOPA failed (ConnectionError)" in t for t in world.hook.posts)
    assert count(world, "ext_public_benchmarks") == 2 and world.repo.list_recommendations()


async def test_selector_fault_heals_via_llm_and_candidate_waits_for_a_human(world):
    sch, _ = world.make(MUTATED)
    await sch.step(); await sch.step()                                  # kamis, competitor on a redesigned page
    with world.e.connect() as c:
        assert c.execute(text("SELECT DISTINCT extraction_source FROM ext_retail_prices")).scalars().all() == ["LLM"]
    cands = world.repo.list_candidates()
    assert len(cands) == 1 and cands[0]["selectors"]["item"] == "article.card_x9" and cands[0]["status"] == "PENDING"
    assert any("New selectors proposed for SHOPA" in t for t in world.hook.posts)
    ov = world.tmp / "overrides.json"
    import json
    assert "selectors" not in json.loads(ov.read_text())["retail"]["SHOPA"]       # nothing persisted yet
    # a person approves; the next scrape (fresh extractor, no run cache) needs no LLM
    curation.approve_selector(world.repo, world.tp, cands[0]["cand_id"], Actor("kim@x.com", "MD"))
    llm = MockLLM(LLM_ITEMS)
    results = await run_retail(world.e, FixtureTransport({"shop-a.example": MUTATED}), world.tp, llm)
    assert llm.calls == 0 and results["SHOPA"].source == "SELECTOR"


async def test_repeated_selector_fault_escalates_once_per_window(world):
    broken = "<html><body><p>maintenance</p></body></html>"             # LLM also finds nothing -> ScrapeExtractionFault
    sch, _ = world.make(broken, llm=MockLLM())
    for _ in range(3):                                                  # faults accumulate across scheduled retail runs
        while "competitor" not in await sch.step():
            pass
    assert sum("ESCALATE" in t for t in world.hook.posts) == 1


def test_curation_permissions_and_lifecycle(world):
    cid = world.repo.add_candidate("SHOPA", {"item": "article.card_x9", "fields": {"title": {"css": "h3.t_k2"}, "sale_price": {"css": "b.s_q"}}},
                                   datetime(2026, 11, 3))
    assert world.repo.add_candidate("SHOPA", world.repo.list_candidates()[0]["selectors"], datetime(2026, 11, 3)) is None   # dedup
    with pytest.raises(PermissionError):
        curation.approve_selector(world.repo, world.tp, cid, Actor("lee", "BUYER"))
    curation.approve_selector(world.repo, world.tp, cid, Actor("kim", "MD"))
    assert world.repo.list_candidates(None)[0]["status"] == "APPROVED"
    with pytest.raises(WorkflowError, match="already APPROVED"):
        curation.reject_selector(world.repo, world.tp, cid, Actor("kim", "MD"))
    with pytest.raises(WorkflowError, match="not found"):
        curation.approve_selector(world.repo, world.tp, 999, Actor("kim", "MD"))
    assert [a["action"] for a in world.repo.audit_entries()][-1] == "SELECTOR_APPROVED"


def test_invalid_candidate_cannot_be_approved_and_stays_pending(world):
    cid = world.repo.add_candidate("SHOPA", {"item": "li..[", "fields": {"title": {"css": "a"}, "sale_price": {"css": "b"}}}, datetime(2026, 11, 3))
    with pytest.raises(WorkflowError, match="no longer valid"):
        curation.approve_selector(world.repo, world.tp, cid, Actor("kim", "MD"))
    assert world.repo.list_candidates()[0]["status"] == "PENDING"


def test_review_resolution_flow(world):
    from marketflow.ops_repo import ReviewItem
    world.repo.add_review([ReviewItem("UNMATCHED_TITLE", "SHOPA", "x", "r")], datetime(2026, 11, 3))
    rid = world.repo.list_review()[0]["review_id"]
    with pytest.raises(PermissionError):
        curation.resolve_review(world.repo, world.tp, rid, Actor("lee", "BUYER"))
    curation.resolve_review(world.repo, world.tp, rid, Actor("kim", "MD"))
    assert world.repo.list_review() == [] and world.repo.list_review(None)[0]["resolved_by"] == "kim"
    with pytest.raises(WorkflowError):
        curation.resolve_review(world.repo, world.tp, rid, Actor("kim", "MD"))
    world.repo.add_review([ReviewItem("UNMATCHED_TITLE", "SHOPA", "x", "r")], datetime(2026, 11, 4))   # same key may reopen after resolve
    assert len(world.repo.list_review()) == 1


def test_cli_review_and_selectors(world):
    from marketflow.ops_repo import ReviewItem
    world.repo.add_review([ReviewItem("UNPARSEABLE_TITLE", "SHOPA", "사과 선물세트 박스", "quantity unknown")], datetime(2026, 11, 3))
    out = io.StringIO(); assert cli.main(["review"], settings=world.s, tp=world.tp, out=out) == 0
    assert "UNPARSEABLE_TITLE" in out.getvalue() and "사과 선물세트 박스" in out.getvalue()
    out = io.StringIO(); assert cli.main(["resolve", "1", "--as", "lee:BUYER"], settings=world.s, tp=world.tp, out=out) == 2
    out = io.StringIO(); assert cli.main(["resolve", "1", "--as", "kim:MD"], settings=world.s, tp=world.tp, out=out) == 0
    out = io.StringIO(); cli.main(["review"], settings=world.s, tp=world.tp, out=out); assert out.getvalue() == ""


# ---- API ---------------------------------------------------------------------------------------------------------------
@pytest.fixture()
def api(world, monkeypatch):
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)
    from marketflow.web.repo import AppRepository
    for email, role in (("md@x.com", "MD"), ("buyer@x.com", "BUYER")):
        AppRepository(world.e).create_user(email, email, role, hash_password("a-long-password"))
    c = TestClient(create_app(world.s, world.e, world.tp))
    hdr = lambda email: {"Authorization": "Bearer " + c.post("/auth/login", json={"email": email, "password": "a-long-password"}).json()["access_token"]}
    return type("A", (), dict(c=c, md=hdr("md@x.com"), buyer=hdr("buyer@x.com")))


def test_api_review_and_selector_endpoints(world, api):
    from marketflow.ops_repo import ReviewItem
    world.repo.add_review([ReviewItem("UNMATCHED_TITLE", "SHOPA", "수입 두리안 1kg", "no match")], datetime(2026, 11, 3))
    cid = world.repo.add_candidate("SHOPA", {"item": "article.card_x9", "fields": {"title": {"css": "h3.t_k2"}, "sale_price": {"css": "b.s_q"}}}, datetime(2026, 11, 3))
    for path in ("/review", "/selectors"):
        assert api.c.get(path).status_code == 401 and api.c.get(path, headers=api.buyer).status_code == 403
    assert api.c.get("/review", headers=api.md).json()["items"][0]["item_key"] == "수입 두리안 1kg"
    assert api.c.post("/review/1/resolve", headers=api.buyer).status_code == 403
    assert api.c.post("/review/1/resolve", headers=api.md).status_code == 200
    assert api.c.post("/review/1/resolve", headers=api.md).status_code == 409
    assert api.c.get("/selectors", headers=api.md).json()["candidates"][0]["status"] == "PENDING"
    assert api.c.post(f"/selectors/{cid}/approve", headers=api.buyer).status_code == 403
    assert api.c.post(f"/selectors/{cid}/approve", headers=api.md).status_code == 200
    assert api.c.post(f"/selectors/{cid}/reject", headers=api.md).status_code == 409
    assert api.c.post(f"/selectors/{cid}/explode", headers=api.md).status_code == 422
