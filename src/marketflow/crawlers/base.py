from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import time
from typing import Any
from urllib.parse import urlparse

from ..notify import Alert


@dataclass
class RunOutcome:
    stored: int = 0
    message: str = ""
    alerts: list[Alert] = field(default_factory=list)
    failed: bool = False          # the run finished but must be shown as failed (e.g. a bad file)


@dataclass(frozen=True)
class Field:
    key: str
    kind: str                      # "text" | "lines" | "int"
    default: Any = ""
    max: int = 50                  # lines: max count; int: max value


class NotConfigured(Exception):
    """Raised by run() when a prerequisite is missing; recorded as SKIPPED with this message."""


class Crawler:
    id: str = ""
    kind: str = ""                 # "inbox" | "news" | "competitor" | "benchmark"
    default_times: tuple[str, ...] = ("07:00",)
    fields: tuple[Field, ...] = ()
    data_signal: str = ""          # MI signal type this crawler feeds (for "data as of")

    def not_configured(self, cfg: dict[str, Any]) -> str | None:
        return None

    async def run(self, cfg: dict[str, Any]) -> RunOutcome:           # pragma: no cover - interface
        raise NotImplementedError


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_times(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > 12 or not all(isinstance(v, str) and _HHMM.match(v.strip()) for v in values):
        raise ValueError("times must be a list of up to 12 HH:MM values (KST)")
    return sorted({v.strip() for v in values})


def to_time(v: str) -> time:
    return time(int(v[:2]), int(v[3:]))


def validate_feed_url(url: str) -> str:
    """Admin-configured URLs are fetched by the server: http(s) only, and never an obviously internal address."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if u.scheme not in {"http", "https"} or not host or host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError(f"not an allowed feed URL: {url!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url.strip()
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise ValueError(f"not an allowed feed URL (internal address): {url!r}")
    return url.strip()


def clean_config(schema: tuple[Field, ...], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    known = {f.key: f for f in schema}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"unknown setting(s): {sorted(unknown)}")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        f = known[k]
        if f.kind == "int":
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= f.max:
                raise ValueError(f"{k} must be a whole number from 1 to {f.max}")
            out[k] = v
        elif f.kind == "lines":
            lines = [str(x).strip() for x in (v if isinstance(v, list) else str(v).splitlines()) if str(x).strip()]
            if len(lines) > f.max or any(len(x) > 300 for x in lines):
                raise ValueError(f"{k}: at most {f.max} lines of 300 characters")
            out[k] = [validate_feed_url(x) for x in lines] if k == "feeds" else lines
        else:
            if not isinstance(v, str) or len(v) > 200:
                raise ValueError(f"{k} must be text up to 200 characters")
            out[k] = v.strip()
    return out
