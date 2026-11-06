"""One crawler per data item: file inboxes, the news crawler, per-crawler schedules, run history, queued manual runs."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from marketflow.crawlers.adapters import parse_feed
from marketflow.crawlers.base import clean_config, parse_times, validate_feed_url
from marketflow.crawlers.service import CrawlerService
from marketflow.db import init_sqlite, make_engine
from marketflow.mi.store import MiStore
from marketflow.notify import Alert
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider
from marketflow.transport import FixtureTransport

T0 = datetime(2026, 11, 2, 22, 40, tzinfo=timezone.utc)              # 2026-11-03 07:40 KST: just after the 07:30 inbox slot
SEARCH_CSV = "week,category,keyword,searches\n2026-10-26,Fruit,딸기,100\n2026-10-26,Fruit,사과,500\n2026-11-02,Fruit,딸기,180\n"
RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>딸기 가격 급등</title><description>겨울 &lt;b&gt;딸기&lt;/b&gt; 수요</description><pubDate>Mon, 02 Nov 2026 03:00:00 GMT</pubDate></item>
<item><title>사과 풍작</title><description>올해 사과 수확</description><pubDate>Tue, 27 Oct 2026 03:00:00 GMT</pubDate></item>
<item><title>딸기 가격 급등</title><description>duplicate in a second feed</description><pubDate>Mon, 02 Nov 2026 03:00:00 GMT</pubDate></item>
<item><title>오래된 딸기 기사</title><description>old</description><pubDate>Mon, 01 Jun 2026 03:00:00 GMT</pubDate></item>
<item><title>unrelated</title><description>nothing</description><pubDate>Mon, 02 Nov 2026 03:00:00 GMT</pubDate></item>
</channel></rss>"""


class Alerts:
    def __init__(self):
        self.sent: list[Alert] = []

    async def dispatch(self, alerts):
        self.sent += alerts


@pytest.fixture()
def world(tmp_path):
    tp = FixedTimeProvider(T0)
    s = Settings(database_url=f"sqlite:///{tmp_path / 'c.db'}", inbox_dir=str(tmp_path / "inbox"))
    e = make_engine(s.database_url); init_sqlite(e)
    notifier = Alerts()

    def make(routes=None):
        return CrawlerService(e, s, tp, FixtureTransport(routes or {}), lambda: None, notifier)
    svc = make()
    return type("W", (), dict(e=e, s=s, tp=tp, svc=svc, make=staticmethod(make), notifier=notifier, mi=MiStore(e), inbox=tmp_path / "inbox"))


def runs(w, cid):
    return w.svc.store.runs(cid)


# ---- registry -----------------------------------------------------------------------------------------------
def test_every_data_item_has_its_own_crawler(world):
    ids = list(world.svc.registry)
    assert ids == ["report", "buyer", "news", "competitor", "flyer", "newproduct", "benchmark_kamis", "benchmark_garak"]
    assert {c.id: c.data_signal for c in world.svc.registry.values() if c.data_signal} == {
        "report": "SEARCH", "buyer": "BUYER", "news": "MEDIA", "flyer": "PROMOTION", "newproduct": "PRODUCT"}


def test_unconfigured_crawlers_say_why(world):
    by = {c["id"]: c for c in world.svc.list()}
    assert by["news"]["state"] == "NOT_CONFIGURED" and "feed" in by["news"]["not_configured"]
    assert by["benchmark_kamis"]["state"] == "NOT_CONFIGURED" and "kamis" in by["benchmark_kamis"]["not_configured"].lower()
    assert by["report"]["state"] == "NEVER_RUN" and by["report"]["times"] == ["07:30"]


# ---- file inbox ---------------------------------------------------------------------------------------------
async def test_inbox_crawler_imports_new_files_and_files_them_away(world):
    (world.inbox / "report").mkdir(parents=True)
    (world.inbox / "report" / "week44.csv").write_text(SEARCH_CSV, encoding="utf-8")
    res = await world.svc.run("report", "MANUAL", "kim@x.com")
    assert res["status"] == "OK" and res["stored"] == 3 and "1 file(s)" in res["message"]
    assert not (world.inbox / "report" / "week44.csv").exists() and len(list((world.inbox / "report" / "processed").iterdir())) == 1
    assert len(world.mi.observations(10)) == 3 and world.svc.detail("report")["data_as_of"] == "2026-11-02"
    again = await world.svc.run("report", "MANUAL", "kim@x.com")
    assert again["status"] == "OK" and again["stored"] == 0 and "No new files" in again["message"]


async def test_a_broken_file_is_parked_in_failed_and_the_run_fails_loudly(world):
    (world.inbox / "buyer").mkdir(parents=True)
    (world.inbox / "buyer" / "bad.csv").write_text("category,gmv\nFruit,1\n", encoding="utf-8")            # no period column
    (world.inbox / "buyer" / "good.csv").write_text("month,category,gmv\n202510,Fruit,1000\n", encoding="utf-8")
    (world.inbox / "buyer" / "notes.txt").write_text("ignored", encoding="utf-8")
    res = await world.svc.run("buyer", "MANUAL", "kim")
    assert res["status"] == "FAILED" and res["stored"] == 1 and "bad.csv" in res["message"] and "no period column" in res["message"]
    assert [f.name[16:] for f in (world.inbox / "buyer" / "failed").iterdir()] == ["bad.csv"] and (world.inbox / "buyer" / "notes.txt").exists()
    assert any(a.key == "CRAWLER_FAILED:buyer" for a in world.notifier.sent)


async def test_symlinks_in_the_inbox_are_never_followed(world, tmp_path):
    (world.inbox / "flyer").mkdir(parents=True)
    secret = tmp_path / "secret.csv"; secret.write_text("week,keyword,count\n2026-11-02,x,1\n", encoding="utf-8")
    (world.inbox / "flyer" / "link.csv").symlink_to(secret)
    res = await world.svc.run("flyer", "MANUAL", "kim")
    assert res["stored"] == 0 and world.mi.observations(10) == []


# ---- news ---------------------------------------------------------------------------------------------------
def test_feed_parsing_handles_rss_atom_and_html_in_descriptions():
    items = parse_feed(RSS)
    assert items[0][0] == "딸기 가격 급등" and "<b>" not in items[0][1] and items[0][2] == datetime(2026, 11, 2, 3, 0)
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><summary>s</summary><updated>2026-11-02T03:00:00Z</updated></entry></feed>'
    assert parse_feed(atom) == [("A", "s", datetime(2026, 11, 2, 3, 0))]
    with pytest.raises(Exception):                                                           # entity-expansion bombs are refused
        parse_feed('<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>')


async def test_news_crawler_counts_mentions_per_week_for_tracked_keywords(world):
    world.mi.upsert_observations([{"signal_type": "SEARCH", "period_start": __import__("datetime").date(2026, 11, 2), "category_l": "Fruit", "keyword": k,
                                   "metric": "searches", "value": v, "source": ""} for k, v in [("딸기", 180), ("사과", 500)]])
    svc = world.make({"feed-a.example": RSS, "feed-b.example": RSS})
    svc.update("news", "kim", None, None, {"feeds": ["https://feed-a.example/rss", "https://feed-b.example/rss"], "extra_keywords": ["두리안"]})
    res = await svc.run("news", "MANUAL", "kim")
    assert res["status"] == "OK" and "2 feed(s)" in res["message"] and "3 article(s)" in res["message"]
    got = {(str(r["period_start"]), r["category_l"], r["keyword"]): r["value"] for r in world.mi.observations(10) if r["signal_type"] == "MEDIA"}
    assert got == {("2026-11-02", "Fruit", "딸기"): 1, ("2026-10-26", "Fruit", "사과"): 1}       # duplicate title and the June article are not counted


async def test_news_counts_only_grow_when_the_feed_window_rolls_forward(world):
    svc = world.make({"feed-a.example": RSS})
    svc.update("news", "kim", None, None, {"feeds": ["https://feed-a.example/rss"], "extra_keywords": ["딸기"]})
    await svc.run("news", "MANUAL", "kim")
    world.mi.upsert_observations([{"signal_type": "MEDIA", "period_start": __import__("datetime").date(2026, 11, 2), "category_l": "", "keyword": "딸기",
                                   "metric": "count", "value": 9, "source": "news"}])
    await svc.run("news", "MANUAL", "kim")
    assert [r["value"] for r in world.mi.observations(10) if r["signal_type"] == "MEDIA"] == [9]       # 1 never overwrites the earlier 9


async def test_news_feed_failures(world):
    svc = world.make({"feed-a.example": RSS})                                       # feed-b has no route -> fails
    svc.update("news", "kim", None, None, {"feeds": ["https://feed-a.example/rss", "https://feed-b.example/rss"], "extra_keywords": ["딸기"]})
    res = await svc.run("news", "MANUAL", "kim")
    assert res["status"] == "FAILED" and "1 failed" in res["message"] and res["stored"] == 1       # partial success is kept but flagged
    svc.update("news", "kim", None, None, {"feeds": ["https://feed-b.example/rss"]})
    res = await svc.run("news", "MANUAL", "kim")
    assert res["status"] == "FAILED" and "all 1 feed(s) failed" in res["message"]
    other = world.make(); other.update("news", "kim", None, None, {"feeds": []})       # no feeds at all: SKIPPED with a reason, not FAILED
    assert (await other.run("news", "MANUAL", "kim"))["status"] == "SKIPPED"


def test_config_validation():
    assert parse_times(["12:00", "07:30", "07:30"]) == ["07:30", "12:00"]
    for bad in (["7:30"], ["24:00"], "07:30", ["07:30"] * 13, [5]):
        with pytest.raises(ValueError):
            parse_times(bad)
    for url in ("ftp://x.example/a", "http://localhost/x", "http://127.0.0.1/x", "http://10.0.0.5/x", "http://169.254.169.254/latest", "http://[::1]/x", "http://printer.local/x", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            validate_feed_url(url)
    assert validate_feed_url(" https://news.example/rss ") == "https://news.example/rss"
    from marketflow.crawlers.adapters import NewsCrawler
    f = NewsCrawler.fields
    assert clean_config(f, {"lookback_days": 7, "extra_keywords": "a\n\n b "})["extra_keywords"] == ["a", "b"]
    for bad in ({"lookback_days": 0}, {"lookback_days": 999}, {"lookback_days": True}, {"nope": 1}, {"feeds": ["http://localhost/"]}):
        with pytest.raises(ValueError):
            clean_config(f, bad)


# ---- scheduling / history -------------------------------------------------------------------------------------
async def test_each_crawler_runs_on_its_own_schedule_once_per_slot(world):
    ran = await world.svc.dispatch_once()                       # 07:40 KST: the 07:30 inbox slot is due, 06:00/09:00 benchmarks are not configured
    assert {"report", "buyer", "flyer", "newproduct"} <= set(ran) and "competitor" not in ran
    assert await world.svc.dispatch_once() == []                # the same slot never runs twice
    world.tp.set(T0 + timedelta(minutes=10))
    assert await world.svc.dispatch_once() == []
    assert all(r["trigger_kind"] == "SCHEDULED" for r in runs(world, "report"))


async def test_schedule_edits_take_effect_without_a_restart(world):
    await world.svc.dispatch_once()
    world.svc.update("report", "kim", None, ["07:30", "08:00"], None)         # a new 08:00 slot (KST) ...
    assert "report" not in await world.svc.dispatch_once()                    # ... is not due yet
    world.tp.set(T0 + timedelta(minutes=21))                                  # 08:01 KST
    assert "report" in await world.svc.dispatch_once()
    world.svc.update("buyer", "kim", False, None, None)
    world.tp.set(T0 + timedelta(days=1, minutes=21))
    assert "buyer" not in await world.svc.dispatch_once()                     # disabled crawlers do not run on schedule


async def test_a_slot_missed_for_hours_is_skipped_not_run_late(world):
    world.tp.set(datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc))            # 15:00 KST: the 07:30 slot is 7.5h old
    ran = await world.svc.dispatch_once()
    assert not {"report", "buyer", "flyer", "newproduct"} & set(ran)                 # 7.5h late: skipped
    assert {"news", "competitor"} <= set(ran)                                        # the 12:00 and 13:00 slots are within the 6h catch-up


async def test_manual_runs_are_queued_once_and_picked_up_by_the_worker(world):
    qid = world.svc.store.queue_run("report", "kim@x.com", world.tp.now().replace(tzinfo=None))
    assert qid and world.svc.store.queue_run("report", "lee@x.com", world.tp.now().replace(tzinfo=None)) is None          # one at a time
    assert runs(world, "report")[0]["status"] == "QUEUED"
    assert "report" in await world.svc.dispatch_once()
    r = runs(world, "report")
    assert [x["trigger_kind"] for x in r][-1] == "MANUAL" and r[-1]["requested_by"] == "kim@x.com" and r[-1]["status"] == "OK"


async def test_a_crawler_never_runs_twice_at_once_and_dead_runs_are_reaped(world):
    now = world.tp.now().replace(tzinfo=None)
    rid = world.svc.store.start_run("report", "MANUAL", "kim", now)
    assert world.svc.store.start_run("report", "MANUAL", "lee", now) is None and await world.svc.run("report", "MANUAL", "lee") is None
    world.tp.set(T0 + timedelta(hours=3))
    await world.svc.dispatch_once()
    stuck = next(r for r in runs(world, "report") if r["run_id"] == rid)
    assert stuck["status"] == "FAILED" and "interrupted" in stuck["message"]


async def test_run_failures_are_recorded_not_raised(world):
    svc = world.make()
    async def boom(cfg): raise ConnectionError("down")
    svc.registry["report"].run = boom
    res = await svc.run("report", "MANUAL", "kim")
    assert res["status"] == "FAILED" and "ConnectionError" in res["message"]
    assert runs(world, "report")[0]["status"] == "FAILED" and svc.summary(svc.registry["report"], runs(world, "report")[0])["state"] == "FAILING"
