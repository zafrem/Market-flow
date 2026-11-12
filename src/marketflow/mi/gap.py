"""Search-sales gap diagnosis (SRS F-08-3.7): keywords people search a lot but rarely buy from, and why.

Candidates: within each category, keywords in the top volume band and the bottom conversion band (buyers per search).
The primary cause is the FIRST that applies, in this order; every other cause that applies is shown as contributing:
  1. NO_PRODUCT          the catalog has no item or alias matching the keyword
  2. OUT_OF_STOCK        the matching item had zero stock on a large share of the days in the period
  3. PRICE_DISADVANTAGE  our unit price is clearly above the lowest confirmed competitor's effective price
  4. OTHER               none of the above: needs a manual look (ranking, product page)"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from ..decision.config import load_decision_config
from .config import MiConfig
from .messages import msg
from .store import MiStore

CAUSES = ("NO_PRODUCT", "OUT_OF_STOCK", "PRICE_DISADVANTAGE", "OTHER")


def _percentile_rank(values: dict[str, float]) -> dict[str, float]:
    ks = sorted(values, key=lambda k: values[k])
    n = len(ks)
    return {k: (sum(1 for o in ks if values[o] < values[k]) / (n - 1)) if n > 1 else 0.0 for k in ks}


def _items_for(keyword: str, names: list[tuple[str, int]]) -> set[int]:
    k = keyword.strip().lower()
    return {i for n, i in names if len(n) >= 2 and (n in k or k in n)}


def search_sales_gap(store: MiStore, settings, cfg: MiConfig, now: datetime, lang: str = "en") -> list[dict[str, Any]]:
    period = store.latest_period("SEARCH")
    if period is None:
        return []
    p = date.fromisoformat(period)
    by_kw: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for (cat, kw), slot in store.period_comparison(cfg.interest_metric).items():
        if slot["latest"] is not None:
            by_kw[(cat, kw)][cfg.interest_metric] = slot["latest"]
    buyers = {k: v for k, v in store.period_metric("buyers", p).items()}
    for k, v in buyers.items():
        by_kw[k]["buyers"] = v
    rows = {k: m for k, m in by_kw.items() if m.get(cfg.interest_metric, 0) > 0 and "buyers" in m}
    names = store.catalog_names()
    out = []
    by_cat: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for (cat, kw), m in rows.items():
        by_cat[cat][kw] = m
    for cat, kws in by_cat.items():
        if len(kws) < cfg.gap_min_keywords:
            continue
        vol = _percentile_rank({k: m[cfg.interest_metric] for k, m in kws.items()})
        conv = {k: m["buyers"] / m[cfg.interest_metric] for k, m in kws.items()}
        conv_rank = _percentile_rank(conv)
        med = median(conv.values())
        for kw, m in kws.items():
            if vol[kw] < cfg.gap_top_volume or conv_rank[kw] > cfg.gap_low_conversion:
                continue
            items = _items_for(kw, names)
            applies: dict[str, Any] = {}
            evidence: dict[str, Any] = {"items": sorted(items)}
            if not items:
                applies["NO_PRODUCT"] = True
            else:
                ratio = store.stockout_ratio(sorted(items), p, p + timedelta(days=7))
                evidence["stockout_ratio"] = None if ratio is None else round(ratio, 2)
                if ratio is not None and ratio >= cfg.gap_stockout_ratio:
                    applies["OUT_OF_STOCK"] = ratio
                gap = store.price_gap_pct(sorted(items), now, settings)
                evidence["price_gap_pct"] = None if gap is None else round(gap, 1)
                if gap is not None and gap >= cfg.gap_price_gap_pct:
                    applies["PRICE_DISADVANTAGE"] = gap
            if not applies:
                applies["OTHER"] = True
            order = [c for c in CAUSES if c in applies]
            primary, secondary = order[0], order[1:]
            T = lambda key, **kw_: msg(lang, key, **kw_)                       # noqa: E731
            why = T("gap.why." + primary, pct=f"{(applies.get(primary) or 0) * (100 if primary == 'OUT_OF_STOCK' else 1):.0f}%")
            if secondary:
                why += " " + T("gap.also", causes=", ".join(T("gap.cause." + c) for c in secondary))
            out.append({
                "keyword": kw, "category": cat, "searches": m[cfg.interest_metric], "buyers": m["buyers"], "conversion": round(conv[kw], 4),
                "category_median_conversion": round(med, 4), "primary_cause": primary, "contributing_causes": secondary, "evidence": evidence,
                "changed": T("gap.changed", searches=f"{m[cfg.interest_metric]:,.0f}", conv=f"{conv[kw] * 100:.1f}%", median=f"{med * 100:.1f}%"),
                "why": why, "action": T("gap.action." + primary)})
    return sorted(out, key=lambda e: (-e["searches"], e["keyword"]))
