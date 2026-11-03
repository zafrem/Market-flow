"""F-05-3 alerting: Slack-compatible webhook with de-duplication, rate limiting, retry/backoff and dead-lettering."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from .decision.engine import Recommendation
from .ops_repo import QueueMixin
from .time_provider import TimeProvider

log = logging.getLogger(__name__)
SEVERITY_ICON = {"RED": ":red_circle:", "AMBER": ":large_orange_circle:", "INFO": ":information_source:"}


@dataclass(frozen=True)
class Alert:
    key: str          # de-duplication identity, e.g. "LOW_MARGIN:APPLE-1:SHOPA"
    severity: str     # RED | AMBER | INFO
    text: str


@dataclass
class DispatchResult:
    sent: int = 0
    suppressed: int = 0       # same key already sent inside the window
    dead: int = 0             # gave up after max attempts
    overflow: int = 0         # beyond the per-run cap (summarised, not sent individually)
    skipped_no_webhook: int = 0
    keys_sent: list[str] = field(default_factory=list)


def slack_escape(text: str) -> str:
    """Slack treats &, <, > specially: <!channel>, <@U123>, <http://x|label> would otherwise be live markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def validate_webhook(url: str) -> str:
    u = urlparse(url)
    if u.scheme not in {"https", "http"} or not u.netloc:
        raise ValueError("alert webhook must be an http(s) URL")
    if u.scheme == "http" and u.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("alert webhook must use https (plain http only allowed for localhost)")
    return url


Post = Callable[[str, dict[str, Any]], Awaitable[int]]


async def _httpx_post(url: str, payload: dict[str, Any]) -> int:
    async with httpx.AsyncClient(timeout=10) as c:
        return (await c.post(url, json=payload)).status_code


class Notifier:
    def __init__(self, webhook_url: str, queues: QueueMixin, tp: TimeProvider, *, post: Post = _httpx_post,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, window_hours: float = 6,
                 max_per_run: int = 20, max_attempts: int = 5, base_delay: float = 1.0):
        self._url = validate_webhook(webhook_url) if webhook_url else ""
        self._q, self._tp, self._post, self._sleep = queues, tp, post, sleep
        self._window, self._cap, self._attempts, self._base = timedelta(hours=window_hours), max_per_run, max_attempts, base_delay

    def _now(self):
        return self._tp.now().replace(tzinfo=None)

    async def dispatch(self, alerts: list[Alert]) -> DispatchResult:
        res = DispatchResult()
        seen: set[str] = set()
        fresh: list[Alert] = []
        for a in alerts:                                       # de-duplicate inside the batch and against the log
            if a.key in seen or self._q.recently_sent(a.key, self._now() - self._window):
                res.suppressed += 1
                continue
            seen.add(a.key)
            fresh.append(a)
        if not self._url:
            res.skipped_no_webhook = len(fresh)
            for a in fresh:
                log.warning("ALERT (no webhook configured) [%s] %s", a.severity, a.text)
            return res
        to_send, rest = fresh[: self._cap], fresh[self._cap:]
        res.overflow = len(rest)
        for a in to_send:
            await self._send_one(a, f"{SEVERITY_ICON.get(a.severity, '')} {slack_escape(a.text)}".strip(), res)
        if rest:                                               # one summary instead of a flood, itself de-duplicated
            summary = Alert("OVERFLOW", "INFO",
                            f"{len(rest)} more alert(s) suppressed by the per-run cap of {self._cap}; see the dashboard.")
            if self._q.recently_sent(summary.key, self._now() - self._window):
                return res
            await self._send_one(summary, SEVERITY_ICON["INFO"] + " " + slack_escape(summary.text), res)
        return res

    async def _send_one(self, alert: Alert, message: str, res: DispatchResult) -> None:
        error = None
        for attempt in range(1, self._attempts + 1):
            try:
                status = await self._post(self._url, {"text": message})
                if 200 <= status < 300:
                    self._q.log_alert(alert.key, "SENT", attempt, None, message, self._now())
                    res.sent += 1
                    res.keys_sent.append(alert.key)
                    return
                error = f"HTTP {status}"
            except Exception as e:                             # never log the URL or exception text: may contain the secret
                error = type(e).__name__
            if attempt < self._attempts:
                await self._sleep(self._base * 2 ** (attempt - 1))
        self._q.log_alert(alert.key, "DEAD", self._attempts, error, message, self._now())
        log.error("alert %s dead-lettered after %d attempts (%s)", alert.key, self._attempts, error)
        res.dead += 1


def alerts_from_recommendations(recs: list[tuple[int, Recommendation]]) -> list[Alert]:
    out = []
    for rec_id, r in recs:
        who, m = f"{r.internal_sku}/{r.channel_name}", r.flags.get("metrics", {})
        if "REVERSE_MARGIN_WARNING" in r.flags:
            out.append(Alert(f"REVERSE_MARGIN:{who}", "RED", f"Reverse margin risk {who}: competitor price is below the "
                             f"floor (₩{m.get('floor')}); price held at ₩{r.suggested_price}. Rec #{rec_id}."))
        if "SPOILAGE_RISK_FLAG" in r.flags:
            out.append(Alert(f"SPOILAGE:{who}", "AMBER", f"Spoilage risk {who}: shelf life {m.get('remaining_days')}d vs "
                             f"run-rate {m.get('run_rate_days')}d. Suggested ₩{r.current_price} → ₩{r.suggested_price}. Rec #{rec_id}."))
        if r.flags.get("LOW_MARGIN"):
            out.append(Alert(f"LOW_MARGIN:{who}", "AMBER", f"Low margin {who}: {m.get('margin_rate')}% at ₩{r.current_price}. Rec #{rec_id}."))
    return out


def alerts_from_stockouts(events: list[dict[str, Any]]) -> list[Alert]:
    return [Alert(f"STOCKOUT:{e['platform']}:{e['channel_item_code']}", "INFO",
                  f"Competitor stock-out on {e['platform']}: {e['item_name']} ({e['channel_item_code']}).") for e in events]
