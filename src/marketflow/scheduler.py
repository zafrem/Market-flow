"""KST wall-clock job scheduler. Time and sleeping are injected so a whole day can be simulated in tests."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Awaitable, Callable

from .schedule import next_run
from .time_provider import TimeProvider

log = logging.getLogger(__name__)

# SRS F-01-1 / F-01-2. Decisions run 30 minutes after each retail scrape so they see fresh competitor data.
BENCHMARK_TIMES = [time(6, 0), time(9, 0)]
RETAIL_TIMES = [time(8, 0), time(13, 0), time(19, 0)]
DECIDE_TIMES = [time(8, 30), time(13, 30), time(19, 30)]


@dataclass(frozen=True)
class Job:
    name: str
    times: list[time]
    run: Callable[[], Awaitable[None]]


class Scheduler:
    def __init__(self, jobs: list[Job], tp: TimeProvider, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 on_failure: Callable[[Job, Exception], Awaitable[None]] | None = None):
        self._jobs, self._tp, self._sleep, self._on_failure = jobs, tp, sleep, on_failure

    def next_due(self) -> tuple[datetime, list[Job]]:
        now = self._tp.now()
        nxt = {j.name: next_run(now, j.times) for j in self._jobs}
        when = min(nxt.values())
        return when, [j for j in self._jobs if nxt[j.name] == when]

    async def step(self) -> list[str]:
        """Sleep until the next due time, then run every job due then (sequentially). Returns the job names run."""
        when, due = self.next_due()
        await self._sleep(max((when - self._tp.now()).total_seconds(), 0))
        if (left := (when - self._tp.now()).total_seconds()) > 0:     # woke early (monotonic vs wall clock): never run a slot early
            await self._sleep(left)
        for job in due:
            try:
                await job.run()
            except Exception as e:                   # one failing job must never stop the loop or the other jobs
                log.exception("job %s failed", job.name)
                if self._on_failure:
                    try:
                        await self._on_failure(job, e)
                    except Exception:
                        log.exception("failure handler for %s failed", job.name)
        return [j.name for j in due]

    async def run_forever(self) -> None:
        while True:
            log.info("next: %s at %s", [j.name for j in self.next_due()[1]], self.next_due()[0].isoformat())
            await self.step()
