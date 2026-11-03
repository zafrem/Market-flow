"""Deterministic time source (SRS §6.2 #2). Never call datetime.now() elsewhere."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class TimeProvider(Protocol):
    def now(self) -> datetime:
        """Timezone-aware current time (UTC)."""


class SystemTimeProvider:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedTimeProvider:
    """Test double; time only moves when set()."""

    def __init__(self, at: datetime):
        if at.tzinfo is None:
            raise ValueError("FixedTimeProvider requires an aware datetime")
        self._at = at.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = at.astimezone(timezone.utc)


def today_kst(tp: TimeProvider) -> date:
    """KST calendar date, the meaning of `recorded_date` columns."""
    return tp.now().astimezone(KST).date()
