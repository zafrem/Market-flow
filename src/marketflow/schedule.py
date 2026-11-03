"""KST daily scheduling helpers (pure, TimeProvider-driven)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from .time_provider import KST


def next_run(now: datetime, times: list[time]) -> datetime:
    """Next KST wall-clock occurrence (strictly after `now`) of any of `times`, as UTC."""
    local = now.astimezone(KST)
    for day_offset in (0, 1):
        for t in sorted(times):
            cand = datetime.combine(local.date() + timedelta(days=day_offset), t, tzinfo=KST)
            if cand > local:
                return cand.astimezone(timezone.utc)
    raise ValueError("no times given")
