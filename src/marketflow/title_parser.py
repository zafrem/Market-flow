"""F-03-1: derive total quantity from a listing title and price per standard unit. Regexes live in config."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from .rules import Rules, current_rules
from .units import STANDARD_UNITS, UnitError, parse_unit


@lru_cache(maxsize=16)
def _rx(template: str, tokens: tuple[str, ...]) -> re.Pattern[str]:
    alt = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    return re.compile(template.replace("{tokens}", alt), re.I)


def total_quantity(title: str, rules: Rules | None = None) -> tuple[str, Decimal]:
    """Title -> (dimension, base quantity). 'a x N' packs win over a bare quantity. Never guesses."""
    r = rules or current_rules()
    tokens = tuple([*r["units"]["measure_tokens"], *r["units"]["count_tokens"]])
    t = r["titles"]
    m = _rx(t["multiplied_pattern"], tokens).search(title)
    if m:
        dim, base = parse_unit(f"{m.group(1)}{m.group(2)}", r)
        return dim, base * Decimal(m.group(3))
    m = _rx(t["quantity_pattern"], tokens).search(title)
    if not m:
        raise UnitError(f"UNPARSEABLE: {title!r}")
    return parse_unit(f"{m.group(1)}{m.group(2)}", r)


def unit_price(title: str, price: Decimal | int, standard_unit: str, rules: Rules | None = None) -> Decimal:
    dim, qty = total_quantity(title, rules)
    std_dim, std_qty = STANDARD_UNITS[standard_unit]
    if dim != std_dim:
        raise UnitError(f"UNPARSEABLE: {title!r} is {dim}, expected {std_dim}")
    return (Decimal(price) * std_qty / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
