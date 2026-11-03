"""Unit parsing / price normalization. Tokens and regex come from config (rules.py), not code."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from .rules import Rules, current_rules, unit_regex

# SRS standard_unit -> (dimension, base quantity). Domain constants, not crawl-dependent.
STANDARD_UNITS: dict[str, tuple[str, Decimal]] = {
    "100g": ("g", Decimal(100)),
    "1kg": ("g", Decimal(1000)),
    "100ml": ("ml", Decimal(100)),
    "1unit": ("unit", Decimal(1)),
}


class UnitError(ValueError):
    pass


@lru_cache(maxsize=8)
def _compiled(pattern: str, tokens: tuple[str, ...]):
    return unit_regex({"pattern": pattern, "measure_tokens": dict.fromkeys(tokens), "count_tokens": []})


def parse_unit(text: str, rules: Rules | None = None) -> tuple[str, Decimal]:
    """'10kg' -> ('g', 10000). Raises UnitError when unresolvable (never guesses)."""
    u = (rules or current_rules())["units"]
    rx = _compiled(u["pattern"], tuple(sorted([*u["measure_tokens"], *u["count_tokens"]])))
    m = rx.match(text or "")
    if not m:
        raise UnitError(f"unparseable unit: {text!r}")
    qty = Decimal((m.group(1) or "1").replace(",", ""))
    token = m.group(2).lower()
    if token in {t.lower() for t in u["count_tokens"]}:
        return "unit", qty
    for k, (dim, factor) in u["measure_tokens"].items():
        if k.lower() == token:
            return dim, qty * Decimal(str(factor))
    raise UnitError(f"unparseable unit: {text!r}")


def normalize_price(price: Decimal, unit_text: str, standard_unit: str, rules: Rules | None = None) -> Decimal:
    """Price per `standard_unit`, rounded half-up to 2 decimals."""
    if standard_unit not in STANDARD_UNITS:
        raise UnitError(f"unknown standard unit: {standard_unit!r}")
    dim, base_qty = parse_unit(unit_text, rules)
    std_dim, std_qty = STANDARD_UNITS[standard_unit]
    if dim != std_dim:
        raise UnitError(f"cannot convert {unit_text!r} ({dim}) to {standard_unit} ({std_dim})")
    return (Decimal(price) * std_qty / base_qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
