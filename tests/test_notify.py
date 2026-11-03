import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from marketflow.db import init_sqlite, make_engine
from marketflow.decision.engine import Recommendation
from marketflow.notify import (Alert, Notifier, alerts_from_recommendations, alerts_from_stockouts, slack_escape,
                               validate_webhook)
from marketflow.ops_repo import Repo
from marketflow.repository import SqlRepository
from marketflow.time_provider import FixedTimeProvider

T0 = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
URL = "https://hooks.example/services/T000/B000/SECRETTOKEN"


class Webhook:
    def __init__(self, statuses=None, raise_with=None):
        self.posts, self.statuses, self.raise_with = [], list(statuses or []), raise_with

    async def __call__(self, url, payload):
        self.posts.append((url, payload))
        if self.raise_with:
            raise self.raise_with
        return self.statuses.pop(0) if self.statuses else 200


@pytest.fixture()
def env(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'n.db'}")
    init_sqlite(e)
    tp, sleeps = FixedTimeProvider(T0), []

    async def sleep(s):
        sleeps.append(s)

    def make(hook, **kw):
        return Notifier(URL, Repo(e), tp, post=hook, sleep=sleep, **kw)
    return type("E", (), dict(repo=Repo(e), engine=e, tp=tp, sleeps=sleeps, make=staticmethod(make)))


A = Alert("LOW_MARGIN:APPLE-1:SHOPA", "AMBER", "Low margin APPLE-1/SHOPA: 4.2%")


async def test_sent_once_logged_and_deduplicated_within_window(env):
    hook = Webhook()
    n = env.make(hook)
    r1 = await n.dispatch([A])
    assert (r1.sent, r1.suppressed) == (1, 0) and hook.posts[0][1]["text"].startswith(":large_orange_circle: Low margin")
    r2 = await n.dispatch([A])
    assert (r2.sent, r2.suppressed) == (0, 1) and len(hook.posts) == 1
    env.tp.set(T0 + timedelta(hours=5, minutes=59))
    assert (await n.dispatch([A])).suppressed == 1
    env.tp.set(T0 + timedelta(hours=6, minutes=1))                       # window passed -> may fire again
    assert (await n.dispatch([A])).sent == 1 and len(hook.posts) == 2


async def test_duplicates_inside_one_batch_collapse(env):
    hook = Webhook()
    r = await env.make(hook).dispatch([A, A, Alert("OTHER", "INFO", "x")])
    assert (r.sent, r.suppressed) == (2, 1)


async def test_retries_with_exponential_backoff_then_succeeds(env):
    hook = Webhook(statuses=[500, 502])
    r = await env.make(hook).dispatch([A])
    assert r.sent == 1 and len(hook.posts) == 3 and env.sleeps == [1.0, 2.0]
    entry = env.repo.alert_entries()[0]
    assert (entry["status"], entry["attempts"]) == ("SENT", 3)


async def test_dead_letter_after_max_attempts_and_retried_next_time(env):
    hook = Webhook(raise_with=ConnectionError(f"cannot reach {URL}"))
    n = env.make(hook)
    r = await n.dispatch([A])
    assert (r.sent, r.dead) == (0, 1) and len(hook.posts) == 5 and env.sleeps == [1.0, 2.0, 4.0, 8.0]
    e = env.repo.alert_entries()[0]
    assert (e["status"], e["attempts"], e["last_error"]) == ("DEAD", 5, "ConnectionError")   # type only, never the message
    hook.raise_with = None
    assert (await n.dispatch([A])).sent == 1                                                # DEAD does not suppress a retry


async def test_secret_url_never_logged_or_stored(env, caplog):
    hook = Webhook(raise_with=RuntimeError(f"boom {URL}"))
    with caplog.at_level(logging.DEBUG):
        await env.make(hook).dispatch([A])
    assert "SECRETTOKEN" not in caplog.text
    assert all("SECRETTOKEN" not in str(v) for e in env.repo.alert_entries() for v in e.values())


async def test_per_run_cap_sends_one_summary(env):
    hook = Webhook()
    alerts = [Alert(f"K{i}", "INFO", f"alert {i}") for i in range(25)]
    r = await env.make(hook, max_per_run=20).dispatch(alerts)
    assert (r.sent, r.overflow) == (21, 5) and len(hook.posts) == 21           # 20 + 1 summary
    assert "5 more alert(s)" in hook.posts[-1][1]["text"]


def test_slack_markup_injection_is_escaped():
    assert slack_escape("<!channel> <@U123> <http://evil|click> a&b") == "&lt;!channel&gt; &lt;@U123&gt; &lt;http://evil|click&gt; a&amp;b"


async def test_scraped_text_in_alert_is_escaped_in_payload(env):
    hook = Webhook()
    await env.make(hook).dispatch([Alert("X", "RED", "competitor <!channel> listing")])
    assert "<!channel>" not in hook.posts[0][1]["text"] and "&lt;!channel&gt;" in hook.posts[0][1]["text"]


async def test_no_webhook_configured_logs_but_records_nothing(env, caplog):
    n = Notifier("", env.repo, env.tp, post=Webhook())
    with caplog.at_level(logging.WARNING):
        r = await n.dispatch([A])
    assert r.skipped_no_webhook == 1 and r.sent == 0 and env.repo.alert_entries() == [] and "no webhook" in caplog.text


@pytest.mark.parametrize("url,ok", [("https://hooks.slack.com/x", True), ("http://localhost:9000/x", True),
                                    ("http://hooks.example/x", False), ("ftp://x/y", False), ("not a url", False)])
def test_webhook_url_validation(url, ok):
    if ok:
        assert validate_webhook(url) == url
    else:
        with pytest.raises(ValueError):
            validate_webhook(url)


def rec(flags, **kw):
    base = dict(std_item_id=1, channel_name="SHOPA", internal_sku="APPLE-1", current_price=10000, suggested_price=10000,
                reason_code="X", status="SUPPRESSED")
    return Recommendation(**{**base, **kw}, flags={"metrics": {"floor": 8186, "margin_rate": "4.20", "remaining_days": 2,
                                                               "run_rate_days": "3.00"}, **flags})


def test_alerts_from_recommendations():
    out = alerts_from_recommendations([(7, rec({"REVERSE_MARGIN_WARNING": "RED", "LOW_MARGIN": True})),
                                       (8, rec({"SPOILAGE_RISK_FLAG": True}, suggested_price=9000))])
    assert [(a.key, a.severity) for a in out] == [("REVERSE_MARGIN:APPLE-1/SHOPA", "RED"), ("LOW_MARGIN:APPLE-1/SHOPA", "AMBER"),
                                                  ("SPOILAGE:APPLE-1/SHOPA", "AMBER")]
    assert "₩8186" in out[0].text and "Rec #7" in out[0].text and "4.20%" in out[1].text and "10000 → ₩9000" in out[2].text
    assert alerts_from_recommendations([(1, rec({}))]) == []


def snap(env, code, oos, hours_ago, item=1):
    SqlRepository(env.engine).insert_retail([{
        "scraped_at": T0.replace(tzinfo=None) - timedelta(hours=hours_ago), "platform": "SHOPA", "std_item_id": item,
        "match_confidence": Decimal("0.9"), "channel_item_code": code, "product_title": "t", "original_price": 1, "sale_price": 1,
        "normalized_unit_price": Decimal("1"), "is_out_of_stock": int(oos), "badge_info": None, "extraction_source": "SELECTOR",
        "region_code": "SEOUL"}])


def test_stockout_detection_requires_a_transition(env):
    snap(env, "NEW", True, 3)                                # first sight already OOS: not a transition
    snap(env, "FLIP", False, 5); snap(env, "FLIP", True, 1)  # in stock -> out of stock: event
    snap(env, "BACK", True, 5); snap(env, "BACK", False, 1)  # recovered: no event
    snap(env, "STAY", True, 5); snap(env, "STAY", True, 1)   # still out: no repeat event
    ev = env.repo.competitor_stockouts(T0.replace(tzinfo=None) - timedelta(hours=26))
    assert [e["channel_item_code"] for e in ev] == ["FLIP"]
    a = alerts_from_stockouts(ev)[0]
    assert a.key == "STOCKOUT:SHOPA:FLIP" and "Apple" in a.text


async def test_overflow_summary_is_deduplicated_across_runs(env):
    hook = Webhook()
    n = env.make(hook, max_per_run=2)
    alerts = [Alert(f"K{i}", "INFO", f"alert {i}") for i in range(5)]
    await n.dispatch(alerts)
    assert sum("more alert(s)" in p[1]["text"] for p in hook.posts) == 1
    await n.dispatch([Alert(f"N{i}", "INFO", f"new {i}") for i in range(5)])    # different overflow set, same window
    assert sum("more alert(s)" in p[1]["text"] for p in hook.posts) == 1


async def test_overflow_summary_is_deduplicated_across_runs(env):
    hook = Webhook()
    n = env.make(hook, max_per_run=2)
    batch = lambda tag: [Alert(f"{tag}{i}", "INFO", f"a{i}") for i in range(5)]
    await n.dispatch(batch("A"))
    first = len(hook.posts)                                                    # 2 alerts + 1 summary
    await n.dispatch(batch("B"))                                               # different keys, still overflowing
    assert sum("more alert(s)" in p[1]["text"] for p in hook.posts) == 1 and len(hook.posts) == first + 2
