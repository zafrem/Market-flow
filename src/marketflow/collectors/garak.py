"""Garak Market (서울 가락시장) auction collector.

ASSUMPTION: no stable public schema was verified. The default mapping in config/extraction.json
(sources.GARAK) expects `{"items":[{"item","kind","grade","unit","avg_price","min_price","max_price",
"volume_kg"}]}` from GARAK_API_URL. When the real endpoint differs, edit the config (or promote an
override) - no code change needed unless the structure itself differs, in which case point
`sources.GARAK.parser` at a new function.
"""
from __future__ import annotations

from datetime import date

from ..rules import Rules
from ..time_provider import TimeProvider, today_kst
from ..transport import Transport
from .base import BenchmarkRecord, dig, parse_price, parse_with_rules


def parse_garak(payload: dict, rule: dict, rules: Rules, today: date) -> list[BenchmarkRecord]:
    f = rule["fields"]
    out: dict[str, BenchmarkRecord] = {}
    for it in dig(payload, rule["items_path"]) or []:
        avg = parse_price(it.get(f["avg"]), rules)
        name, unit = (it.get(f["name"]) or "").strip(), (it.get(f["unit"]) or "").strip()
        if avg is None or not name or not unit:
            continue
        lo = parse_price(it.get(f["min"]), rules) or avg
        hi = parse_price(it.get(f["max"]), rules) or avg
        raw = "/".join(p for p in (name, (it.get(f["kind"]) or "").strip(), (it.get(f["grade"]) or "").strip()) if p)
        extra = {}
        if (vol := parse_price(it.get(f["volume"]), rules)) is not None:
            extra["inflow_kg"] = str(vol)
        out[raw] = BenchmarkRecord(
            recorded_date=today, source_type="GARAK", raw_item_name=raw[:150], alias_candidates=(name,),
            unit_text=unit, avg_price=avg, min_price=min(lo, avg), max_price=max(hi, avg), extra=extra,
        )
    return list(out.values())


class GarakCollector:
    source_type = "GARAK"

    def __init__(self, transport: Transport, time: TimeProvider, url: str, api_key: str = ""):
        self._t, self._time, self._url, self._key = transport, time, url, api_key

    async def collect(self) -> list[BenchmarkRecord]:
        params = {"serviceKey": self._key} if self._key else None
        return self.parse(await self._t.get_text(self._url, params))

    def parse(self, body: str) -> list[BenchmarkRecord]:
        return parse_with_rules(self.source_type, body, today_kst(self._time))
