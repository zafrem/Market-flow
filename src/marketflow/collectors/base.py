from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from ..rules import Rules, current_rules, load_callable


@dataclass(frozen=True)
class BenchmarkRecord:
    recorded_date: date
    source_type: str
    raw_item_name: str          # unique per (date, source): item/kind[/grade]
    alias_candidates: tuple[str, ...]  # tried in order against master_product_aliases
    unit_text: str              # unit the price refers to, e.g. '10kg'
    avg_price: Decimal
    min_price: Decimal
    max_price: Decimal
    extra: dict[str, Any] = field(default_factory=dict)


class Collector(Protocol):
    source_type: str

    async def collect(self) -> list[BenchmarkRecord]: ...


def parse_price(value: object, rules: Rules | None = None) -> Decimal | None:
    """'46,500' -> 46500; '-', '', '0', None -> None. Cleanup regex comes from config."""
    if value is None:
        return None
    clean = (rules or current_rules())["common"]["price_clean_pattern"]
    s = re.sub(clean, "", str(value))
    if s in {"", "-"}:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d > 0 else None


def dig(obj: Any, path: list[str]) -> Any:
    for k in path:
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj


def parse_with_rules(source_type: str, body: str, today: date) -> list[BenchmarkRecord]:
    """Resolve the source's parser from *current* config on every call (dynamic loading)."""
    rules = current_rules()
    rule = rules["sources"][source_type]
    return load_callable(rule["parser"])(json.loads(body), rule, rules, today)
