"""Point-in-time view of inventory, cost and margin (SRS F-08-1.4).

Two clocks: the *occurred* date (`recorded_date`, the day the figures apply to) and the *ingested* time (when the system
learned them). `known_at=None` answers "what do we know about that day now"; a timestamp answers "what did we believe
at that moment", so a retroactive cost correction does not change last month's report.
All timestamps are UTC, matching the DATETIME columns."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .decision.calc import disposal_cost, net_margin, q2
from .decision.config import load_decision_config
from .ops_repo import OpsRepository
from .settings import Settings


def snapshot(repo: OpsRepository, settings: Settings, day: date, known_at: datetime | None = None) -> list[dict[str, Any]]:
    out = []
    for r in repo.inventory_as_known(day, known_at):
        cfg = load_decision_config(settings.decision, r["category_l"])
        d = disposal_cost(r["purchase_cost"], r["disposal_cost"], cfg.disposal_rate_pct)
        amt, rate = net_margin(r["current_selling_price"], r["purchase_cost"], r["fixed_cost"], r["logistics_cost"], r["promo_cost"],
                               r["channel_fee_rate"], bool(r["vat_inclusive"]), cfg.vat_rate, disposal=d)
        out.append({"recorded_date": str(day), "known_since": r["ingested_at"].isoformat(sep=" "), "internal_sku": r["internal_sku"],
                    "channel_name": r["channel_name"], "item_name": r["item_name"], "category_l": r["category_l"], "category_m": r["category_m"],
                    "current_selling_price": int(r["current_selling_price"]), "purchase_cost": str(q2(r["purchase_cost"])),
                    "disposal_cost": str(q2(d)), "stock_qty": r["stock_qty"], "min_price_floor": int(r["min_price_floor"]),
                    "margin_amount": str(amt), "margin_rate": str(rate)})
    return out
