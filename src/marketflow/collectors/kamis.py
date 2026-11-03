"""KAMIS (aT 농산물유통정보) daily price collector.

Field names, item path and regexes live in config/extraction.json (sources.KAMIS); this module only
holds the parser *function* the config points at. Shape follows the documented `dailySalesList` JSON
(item_name, kind_name, unit, dpr1 = latest-day price). KAMIS gives one price per row, so min = max = avg
(`price_basis: single`). Verify against a live response before production (needs API key/id).
"""
from __future__ import annotations

import re
from datetime import date

from ..rules import Rules
from ..time_provider import TimeProvider, today_kst
from ..transport import Transport
from .base import BenchmarkRecord, dig, parse_price, parse_with_rules

KAMIS_URL = "http://www.kamis.or.kr/service/price/xml.do"


class KamisError(RuntimeError):
    pass


def parse_kamis(payload: dict, rule: dict, rules: Rules, today: date) -> list[BenchmarkRecord]:
    status = dig(payload, rule["status_path"])
    if isinstance(status, list):  # KAMIS returns ["001"] (no data) / ["200"] (auth error)
        code = status[0] if status else "?"
        if code in rule.get("no_data_codes", []):
            return []
        raise KamisError(f"KAMIS error code {code}")
    if not isinstance(status, dict) or status.get("error_code") not in (None, "000"):
        raise KamisError(f"KAMIS error: {status!r}")
    f, strip = rule["fields"], rule.get("alias_strip_pattern")
    out: dict[str, BenchmarkRecord] = {}
    for it in dig(payload, rule["items_path"]) or []:
        price = parse_price(it.get(f["price"]), rules)
        name, unit = (it.get(f["name"]) or "").strip(), (it.get(f["unit"]) or "").strip()
        if price is None or not name or not unit:
            continue
        raw = "/".join(p for p in (name, (it.get(f["kind"]) or "").strip(), (it.get(f["rank"]) or "").strip()) if p)
        aliases = (name, re.sub(strip, "", name)) if strip else (name,)
        out[raw] = BenchmarkRecord(
            recorded_date=today, source_type="KAMIS", raw_item_name=raw[:150], alias_candidates=aliases,
            unit_text=unit, avg_price=price, min_price=price, max_price=price,
            extra={"price_basis": "single"},
        )
    return list(out.values())


class KamisCollector:
    source_type = "KAMIS"

    def __init__(self, transport: Transport, time: TimeProvider, api_key: str, api_id: str,
                 product_cls: str = "02", url: str = KAMIS_URL):
        self._t, self._time = transport, time
        self._key, self._id, self._cls, self._url = api_key, api_id, product_cls, url

    async def collect(self) -> list[BenchmarkRecord]:
        body = await self._t.get_text(self._url, {
            "action": "dailySalesList", "p_cert_key": self._key, "p_cert_id": self._id,
            "p_returntype": "json", "p_product_cls_code": self._cls,
        })
        return self.parse(body)

    def parse(self, body: str) -> list[BenchmarkRecord]:
        return parse_with_rules(self.source_type, body, today_kst(self._time))
