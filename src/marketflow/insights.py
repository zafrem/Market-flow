"""P2 analyses (SRS F-08-3.5, 3.8, 3.9): failed promotions, prolonged discounts, new products and sales-response scenarios.

All of it reads data the system already holds (daily sales in the inventory loads, scraped competitor prices, the
promotion calendar) and every finding shows the figures it rests on, so a person can check it. Estimates need enough data:
with too little, the answer is "not enough data", never a number."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from .ops_repo import OpsRepository


@dataclass(frozen=True)
class InsightConfig:
    min_uplift_pct: float = 5.0          # a promotion that lifts sales by less than this is called failed
    min_promo_days: int = 3              # judge a promotion only after this many days with sales data
    baseline_weeks: int = 4              # same weekday in the previous N weeks
    min_baseline_samples: int = 2        # per day
    prolonged_window_days: int = 90
    prolonged_share_pct: float = 60.0    # share of observed days on which a listing was discounted
    prolonged_min_days: int = 14         # observed days needed before judging
    new_window_days: int = 14
    min_promotions_for_scenarios: int = 3


def load_insight_config(raw: Mapping[str, Any] | None) -> InsightConfig:
    raw, base = dict(raw or {}), InsightConfig()
    try:
        return InsightConfig(**{k: type(getattr(base, k))(raw[k]) for k in base.__dict__ if k in raw})
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid [insights] setting: {e}") from e


def _d(v: Any) -> date:
    return v.date() if isinstance(v, datetime) else v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _daily_sales(repo: OpsRepository, category: str, channel: str, start: date, end: date) -> dict[date, float]:
    rows = repo._rows("SELECT i.recorded_date AS d, SUM(i.daily_sales_qty) AS q FROM int_operations_inventory i JOIN master_products m USING (std_item_id) "
                      "WHERE (:cat = '' OR m.category_l = :cat) AND (:ch = '' OR i.channel_name = :ch) AND i.recorded_date BETWEEN :a AND :b GROUP BY i.recorded_date",
                      cat=category, ch=channel, a=start.isoformat(), b=end.isoformat())
    return {_d(r["d"]): float(r["q"]) for r in rows}


def evaluate_promotion(repo: OpsRepository, promo: Mapping[str, Any], today: date, cfg: InsightConfig,
                       other_promos: list[tuple[date, date]] | None = None) -> dict[str, Any]:
    """Actual units during the promotion against the average of the same weekday in the previous weeks (no seasonal adjustment:
    the baseline says so, because a holiday week would need one)."""
    s, e = _d(promo["start_date"]), min(_d(promo["end_date"]), today)
    out = {"promo_id": promo["promo_id"], "name": promo["name"], "category_l": promo["category_l"], "channel_name": promo["channel_name"],
           "start_date": str(s), "end_date": str(_d(promo["end_date"])), "discount_pct": None if promo.get("discount_pct") in (None, "") else float(promo["discount_pct"]),
           "baseline_rule": f"mean of the same weekday in the previous {cfg.baseline_weeks} weeks (days of any promotion excluded); no seasonal adjustment"}
    busy = [(s, _d(promo["end_date"])), *(other_promos or [])]            # a baseline day inside any promotion is not baseline
    lookback = s - timedelta(weeks=cfg.baseline_weeks)
    sales = _daily_sales(repo, promo["category_l"], promo["channel_name"], lookback, e)
    actual = baseline = 0.0
    days = 0
    for i in range((e - s).days + 1):
        day = s + timedelta(days=i)
        if day not in sales:
            continue
        samples = [sales[p] for k in range(1, cfg.baseline_weeks + 1) if (p := day - timedelta(weeks=k)) in sales and not any(a <= p <= b for a, b in busy)]
        if len(samples) < cfg.min_baseline_samples:
            continue
        actual, baseline, days = actual + sales[day], baseline + statistics.mean(samples), days + 1
    out.update(days_measured=days, actual_units=round(actual, 1), baseline_units=round(baseline, 1))
    if days < cfg.min_promo_days or baseline <= 0:
        return {**out, "uplift_pct": None, "status": "INSUFFICIENT_DATA"}
    uplift = (actual / baseline - 1) * 100
    return {**out, "uplift_pct": round(uplift, 1), "status": "FAILED" if uplift < cfg.min_uplift_pct else "OK"}


def promotion_results(repo: OpsRepository, today: date, cfg: InsightConfig, window_days: int = 120) -> list[dict[str, Any]]:
    from .promotions import list_promotions
    promos = list_promotions(repo, today, window_days + 40)
    spans = [(_d(p["start_date"]), _d(p["end_date"])) for p in promos]
    return [evaluate_promotion(repo, p, today, cfg, spans) for p in list_promotions(repo, today, window_days) if _d(p["start_date"]) <= today]


def prolonged_discounts(repo: OpsRepository, today: date, cfg: InsightConfig) -> list[dict[str, Any]]:
    """Competitor listings that were on discount for most of the observed days in the window."""
    since = (today - timedelta(days=cfg.prolonged_window_days)).isoformat()
    rows = repo._rows("SELECT platform, channel_item_code, MAX(product_title) AS title, MAX(std_item_id) AS std_item_id, COUNT(DISTINCT DATE(scraped_at)) AS seen, "
                      "COUNT(DISTINCT CASE WHEN sale_price < original_price THEN DATE(scraped_at) END) AS discounted FROM ext_retail_prices "
                      "WHERE scraped_at >= :s AND original_price > 0 GROUP BY platform, channel_item_code", s=since)
    out = []
    for r in rows:
        seen, disc = int(r["seen"]), int(r["discounted"] or 0)
        if seen >= cfg.prolonged_min_days and disc * 100 / seen >= cfg.prolonged_share_pct:
            out.append({"platform": r["platform"], "channel_item_code": r["channel_item_code"], "title": r["title"], "std_item_id": int(r["std_item_id"]),
                        "days_observed": seen, "days_discounted": disc, "share_pct": round(disc * 100 / seen, 1), "window_days": cfg.prolonged_window_days})
    return sorted(out, key=lambda x: -x["share_pct"])


_NORM = re.compile(r"[\W\d_]+")


def _title_key(title: str) -> str:
    """Letters only: a changed pack size or a decoration like '[특가]' does not make a product new."""
    return _NORM.sub("", re.sub(r"\[[^\]]*\]|\([^)]*\)", "", title).lower())


def new_products(repo: OpsRepository, today: date, cfg: InsightConfig) -> list[dict[str, Any]]:
    """Listings first seen inside the window on a platform we have watched for longer. A listing counts as renamed, not new,
    when its letters-only title matches an earlier one, or when it replaces an earlier code for the same catalog item that
    has since disappeared."""
    cut = datetime.combine(today - timedelta(days=cfg.new_window_days), datetime.min.time())
    first = repo._rows("SELECT platform, channel_item_code, MIN(scraped_at) AS first_seen, MAX(scraped_at) AS last_seen, MAX(product_title) AS title, MAX(std_item_id) AS std_item_id "
                       "FROM ext_retail_prices GROUP BY platform, channel_item_code")
    for r in first:
        for k in ("first_seen", "last_seen"):
            r[k] = r[k] if isinstance(r[k], datetime) else datetime.fromisoformat(str(r[k]))
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for r in first:
        by_platform.setdefault(r["platform"], []).append(r)
    out = []
    for platform, items in by_platform.items():
        if min(i["first_seen"] for i in items) >= cut:                    # we have not watched this platform for long: everything would look new
            continue
        old = [i for i in items if i["first_seen"] < cut]
        old_keys = {_title_key(i["title"]) for i in old}
        gone_items = {i["std_item_id"] for i in old if i["last_seen"] < cut}
        for i in items:
            if i["first_seen"] < cut:
                continue
            renamed = _title_key(i["title"]) in old_keys or i["std_item_id"] in gone_items
            if not renamed:
                out.append({"platform": platform, "channel_item_code": i["channel_item_code"], "title": i["title"], "std_item_id": int(i["std_item_id"]), "first_seen": i["first_seen"].isoformat(sep=" ")})
    return sorted(out, key=lambda x: x["first_seen"], reverse=True)


def elasticity_basis(results: list[dict[str, Any]], category_l: str, cfg: InsightConfig) -> dict[str, Any]:
    """Sales response to a price cut, estimated from this category's own measured promotions that had a discount:
    elasticity = (units uplift) / (price cut). Three scenarios from the lower quartile, median and upper quartile."""
    used = [r for r in results if r["category_l"] in (category_l, "") and r["status"] in ("OK", "FAILED") and r["discount_pct"]]
    if len(used) < cfg.min_promotions_for_scenarios:
        return {"status": "INSUFFICIENT_DATA", "promotions_used": len(used), "needed": cfg.min_promotions_for_scenarios}
    es = sorted(max(0.0, (r["uplift_pct"] / 100) / (r["discount_pct"] / 100)) for r in used)
    q = statistics.quantiles(es, n=4, method="inclusive")
    return {"status": "OK", "promotions_used": len(used), "span": [min(r["start_date"] for r in used), max(r["end_date"] for r in used)],
            "elasticity": {"pessimistic": round(q[0], 2), "base": round(q[1], 2), "optimistic": round(q[2], 2)}}


def project_scenarios(basis: Mapping[str, Any], weekly_units: float, current_price: float, new_price: float, unit_margin_now: float, unit_margin_new: float) -> dict[str, Any]:
    """Expected weekly units and margin amount at the new price under each scenario."""
    if basis.get("status") != "OK":
        return {"status": "INSUFFICIENT_DATA", **{k: v for k, v in basis.items() if k != "status"}}
    cut = max(0.0, (current_price - new_price) / current_price)
    out = {}
    for name, e in basis["elasticity"].items():
        units = weekly_units * (1 + e * cut)
        out[name] = {"elasticity": e, "weekly_units": round(units, 1), "weekly_margin": round(units * unit_margin_new), "vs_now": round(units * unit_margin_new - weekly_units * unit_margin_now)}
    return {"status": "OK", "price_cut_pct": round(cut * 100, 1), "promotions_used": basis["promotions_used"], "span": basis["span"], "scenarios": out}
