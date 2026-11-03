"""Pure business formulas (SRS F-03-2, F-03-3, F-04). All money math in Decimal."""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from .config import DecisionConfig

INF = Decimal("Infinity")
_Q2 = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return x.quantize(_Q2, rounding=ROUND_HALF_UP)


def net_price(price: int | Decimal, vat_inclusive: bool, vat_rate: Decimal) -> Decimal:
    p = Decimal(price)
    return p / (1 + vat_rate) if vat_inclusive else p


def net_margin(price: int | Decimal, cogs: Decimal, packaging: Decimal, logistics: Decimal, promo: Decimal,
               fee_pct: Decimal, vat_inclusive: bool, vat_rate: Decimal) -> tuple[Decimal, Decimal]:
    """(margin amount, margin rate %). Commission is charged on the shown (VAT-inclusive) price."""
    p = Decimal(price)
    pn = net_price(p, vat_inclusive, vat_rate)
    amount = pn - (cogs + packaging + logistics + promo + p * fee_pct / 100)
    return q2(amount), (q2(amount / pn * 100) if pn else Decimal(0))


def price_floor(cogs: Decimal, packaging: Decimal, logistics: Decimal, promo: Decimal, fee_pct: Decimal,
                vat_inclusive: bool, cfg: DecisionConfig) -> int:
    """Lowest integer price whose net margin >= target_min_margin_pct (default: break-even)."""
    t, f = cfg.target_min_margin_pct / 100, fee_pct / 100
    k = (1 - t) / (1 + cfg.vat_rate) if vat_inclusive else (1 - t)
    if k <= f:
        raise ValueError("channel fee + target margin leave no feasible price")
    costs = cogs + packaging + logistics + promo

    def ok(price: int) -> bool:
        pn = net_price(price, vat_inclusive, cfg.vat_rate)
        return pn - costs - Decimal(price) * f >= t * pn

    p = max(1, math.ceil(costs / (k - f)))
    while not ok(p):          # exact verification: never trust the closed form near rounding edges
        p += 1
    while p > 1 and ok(p - 1):
        p -= 1
    return p


def run_rate(stock: int, recent_daily_sales: Sequence[int]) -> Decimal:
    """Days to exhaust using the mean of up to the last 3 daily rows; INFINITE when nothing sells."""
    hist = list(recent_daily_sales)[:3]
    if not hist or sum(hist) == 0:
        return INF
    return Decimal(stock) / (Decimal(sum(hist)) / len(hist))


def spoilage_risk(stock: int, remaining_days: int | None, rr: Decimal, buffer_days: Decimal) -> bool:
    if stock <= 0 or remaining_days is None:
        return False
    return rr == INF or Decimal(remaining_days) <= rr + buffer_days


def round_price(x: Decimal, tick: int) -> int:
    return int((x / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * tick
