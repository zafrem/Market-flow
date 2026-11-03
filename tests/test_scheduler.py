from datetime import datetime, time, timedelta, timezone

import pytest

from marketflow.scheduler import BENCHMARK_TIMES, DECIDE_TIMES, RETAIL_TIMES, Job, Scheduler
from marketflow.time_provider import KST, FixedTimeProvider

# 2026-11-03 05:00 KST
START = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)


def harness(jobs_spec, on_failure=None):
    tp, log = FixedTimeProvider(START), []

    async def sleep(s):
        tp.set(tp.now() + timedelta(seconds=s))

    def job(name, times, boom=None):
        async def run():
            log.append((name, tp.now().astimezone(KST).strftime("%H:%M")))
            if boom:
                raise boom
        return Job(name, times, run)
    return tp, log, Scheduler([job(*j) for j in jobs_spec], tp, sleep=sleep, on_failure=on_failure)


async def test_a_full_day_follows_the_srs_schedule():
    tp, log, sch = harness([("benchmarks", BENCHMARK_TIMES), ("retail", RETAIL_TIMES), ("decide", DECIDE_TIMES)])
    for _ in range(8):
        await sch.step()
    assert log == [("benchmarks", "06:00"), ("retail", "08:00"), ("decide", "08:30"), ("benchmarks", "09:00"),
                   ("retail", "13:00"), ("decide", "13:30"), ("retail", "19:00"), ("decide", "19:30")]
    await sch.step()
    assert log[-1] == ("benchmarks", "06:00") and tp.now().astimezone(KST).day == 4         # rolls to the next day


async def test_simultaneous_jobs_run_in_declared_order():
    _, log, sch = harness([("a", [time(7, 0)]), ("b", [time(7, 0)])])
    assert await sch.step() == ["a", "b"] and [n for n, _ in log] == ["a", "b"]


async def test_failing_job_does_not_stop_siblings_or_the_loop():
    failures = []

    async def on_failure(job, exc):
        failures.append((job.name, type(exc).__name__))
    _, log, sch = harness([("bad", [time(7, 0)], ValueError("x")), ("good", [time(7, 0)]), ("later", [time(8, 0)])], on_failure)
    assert await sch.step() == ["bad", "good"] and failures == [("bad", "ValueError")]
    await sch.step()
    assert [n for n, _ in log] == ["bad", "good", "later"]


async def test_failure_handler_that_also_fails_is_contained():
    async def broken_handler(job, exc):
        raise RuntimeError("handler down")
    _, log, sch = harness([("bad", [time(7, 0)], ValueError("x")), ("good", [time(7, 0)])], broken_handler)
    await sch.step()
    assert [n for n, _ in log] == ["bad", "good"]


def test_next_due_never_in_the_past():
    tp, _, sch = harness([("a", [time(5, 0)])])
    when, _ = sch.next_due()                                      # exactly 05:00 now -> next is tomorrow, not "now"
    assert when > tp.now() and (when - tp.now()) == timedelta(hours=24)
