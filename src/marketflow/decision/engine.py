"""F-04: price recommendation for one inventory row. Pure: all inputs are passed in.
`now` and snapshot timestamps are naive UTC (matches the DATETIME columns). Region filtering happens at query time."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .calc import INF, net_margin, q2, round_price, run_rate, spoilage_risk
from .config import DecisionConfig


@dataclass(frozen=True)
class Context:
    competitors: list[dict[str, Any]] = field(default_factory=list)   # latest snapshot per competitor listing
    recent_daily_sales: list[int] = field(default_factory=list)        # newest first
    wholesale_change_pct: Decimal | None = None                        # latest vs previous benchmark
    last_change_at: datetime | None = None                             # last APPLIED/REVERTED change


@dataclass(frozen=True)
class Recommendation:
    std_item_id: int
    channel_name: str
    internal_sku: str
    current_price: int
    suggested_price: int
    reason_code: str
    flags: dict[str, Any]
    status: str                    # PENDING (needs a decision) | SUPPRESSED (warning only, price unchanged)


def usable_competitors(ctx: Context, cfg: DecisionConfig, now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(hours=float(cfg.competitor_max_age_hours))
    return [c for c in ctx.competitors
            if c["match_confidence"] >= cfg.min_match_confidence and c["scraped_at"] >= cutoff]


def evaluate(row: dict[str, Any], ctx: Context, cfg: DecisionConfig, now: datetime) -> Recommendation | None:
    current, floor = int(row["current_selling_price"]), int(row["min_price_floor"])
    cooling = bool(ctx.last_change_at and now - ctx.last_change_at < timedelta(hours=float(cfg.cooldown_hours)))
    stock = int(row["stock_qty"])
    rr = run_rate(stock, ctx.recent_daily_sales)
    life = row.get("lifecycle_metric") or {}
    margin_amt, margin_rate = net_margin(current, row["purchase_cost"], row["fixed_cost"], row["logistics_cost"],
                                         row["promo_cost"], row["channel_fee_rate"], bool(row["vat_inclusive"]), cfg.vat_rate)
    flags: dict[str, Any] = {"metrics": {
        "margin_amount": str(margin_amt), "margin_rate": str(margin_rate),
        "run_rate_days": "INFINITE" if rr == INF else str(q2(rr)), "remaining_days": life.get("remaining_days"),
        "internal_sku": row["internal_sku"], "floor": floor}}
    if margin_rate <= cfg.alert_margin_pct:
        flags["LOW_MARGIN"] = True

    comps = usable_competitors(ctx, cfg, now)
    in_stock = [c for c in comps if not c["is_out_of_stock"]]
    spoil = spoilage_risk(stock, life.get("remaining_days"), rr, cfg.spoilage_buffer_days)
    pack = Decimal(str(row.get("pack_std_units") or 1))
    suggested, reason = current, "NO_CHANGE"

    competitive: Decimal | None = None
    if in_stock:                                                       # steps 1-4 of the F-04-1 algorithm
        ref = min(Decimal(str(c["normalized_unit_price"])) for c in in_stock) * pack
        competitive = ref * (1 - cfg.undercut_pct / 100)
        flags["metrics"]["competitor_reference"] = str(q2(ref))

    if spoil:
        flags["SPOILAGE_RISK_FLAG"] = True
        flags["actions"] = ["FLASH_COUPON", "REROUTE_OFFLINE_MARKDOWN"]
        flash = round_price(Decimal(current) * (1 - cfg.flash_discount_pct / 100), cfg.round_to)
        suggested, reason = max(floor, min(current, flash)), "SPOILAGE_RISK"
        if competitive is not None and competitive < suggested:
            suggested = max(floor, round_price(competitive, cfg.round_to))
    elif competitive is not None:
        step = Decimal(current) * cfg.max_step_pct / 100
        capped = min(max(competitive, Decimal(current) - step), Decimal(current) + step)
        if competitive < floor:                                        # uses the UNclamped target (F-04-1)
            flags["REVERSE_MARGIN_WARNING"] = "RED"
            suggested, reason = max(current, floor), "REVERSE_MARGIN"
        else:
            suggested, reason = max(floor, round_price(capped, cfg.round_to)), "COMPETITIVE_MATCH"
        if suggested < current and ctx.wholesale_change_pct is not None \
                and ctx.wholesale_change_pct > cfg.wholesale_rise_threshold_pct:
            suggested, reason = current, "WHOLESALE_GUARD"
            flags["WHOLESALE_GUARD"] = str(ctx.wholesale_change_pct)

    # F-04-3 supply vacuum: only when spoilage is NOT flagged and we are not about to cut the price
    oos = [c for c in comps if c["is_out_of_stock"]]
    if not spoil and oos and rr > cfg.vacuum_min_run_rate_days and suggested >= current and floor <= current:
        share = Decimal(len(oos)) / Decimal(len(comps))
        pct = cfg.vacuum_min_pct + (cfg.vacuum_max_pct - cfg.vacuum_min_pct) * share
        up = max(floor, round_price(Decimal(current) * (1 + pct / 100), cfg.round_to))
        if up > suggested:
            suggested, reason = up, "SUPPLY_VACUUM"
            flags["vacuum_pct"] = str(q2(pct))

    alerts = any(k in flags for k in ("REVERSE_MARGIN_WARNING", "SPOILAGE_RISK_FLAG", "LOW_MARGIN", "WHOLESALE_GUARD"))
    if cooling:                       # cool-down holds the PRICE after a change, never the hazard alerts. LOW_MARGIN /
        if not any(k in flags for k in ("REVERSE_MARGIN_WARNING", "SPOILAGE_RISK_FLAG")):   # WHOLESALE_GUARD stay quiet: the
            return None                                                                      # MD just chose this price
        if suggested != current:
            flags["COOLDOWN_HOLD"] = str(suggested)
            suggested, reason = current, "COOLDOWN_HOLD"
    if suggested == current and not alerts:
        return None
    if reason == "NO_CHANGE" and "LOW_MARGIN" in flags:
        reason = "LOW_MARGIN"
    return Recommendation(int(row["std_item_id"]), row["channel_name"], row["internal_sku"], current, suggested,
                          reason, flags, "SUPPRESSED" if suggested == current else "PENDING")
