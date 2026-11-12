"""Glue: load observations, run both analyses, optionally restrict to a user's categories."""
from __future__ import annotations

from typing import Any

from ..settings import Settings
from datetime import datetime

from .analysis import category_health, trend_radar
from .gap import search_sales_gap
from .config import load_mi_config
from .store import MiStore


def mi_report(store: MiStore, settings: Settings, allowed: set[str] | None = None, include_stable: bool = False, lang: str = "en",
               now: datetime | None = None) -> dict[str, Any]:
    """allowed=None means every category (ADMIN). Rows with no category are visible to unrestricted users only.
    Shares are computed across all categories first, so a restricted user's numbers match what an admin sees."""
    cfg = load_mi_config(settings.mi)
    rows = store.observations(cfg.history_periods)
    health = category_health(rows, store.supply_by_category(), cfg, lang)
    radar = trend_radar(rows, health, cfg, include_stable, lang)
    cmp_ = store.period_comparison(cfg.interest_metric)
    for r in radar:                                   # period-over-period and year-over-year change of the keyword's searches
        slot = cmp_.get((r["category"], r["keyword"]), {})
        ratio = lambda a, b: None if a is None or not b else round(a / b - 1, 4)     # noqa: E731
        r["wow"], r["yoy"] = ratio(slot.get("latest"), slot.get("prev")), ratio(slot.get("latest"), slot.get("year"))
    gap = search_sales_gap(store, settings, cfg, datetime.utcnow() if now is None else now, lang)
    if allowed is not None:
        health = [h for h in health if h["category"] in allowed]
        radar = [r for r in radar if r["category"] in allowed]
        gap = [g for g in gap if g["category"] in allowed]
    periods = sorted({r["period_start"] for r in rows})
    return {"health": health, "radar": radar, "gap": gap, "synonyms_applied": len(store.synonyms()), "as_of": periods[-1].isoformat() if periods else None,
            "signals": sorted({r["signal_type"] for r in rows})}
