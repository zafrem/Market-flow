"""Glue: load observations, run both analyses, optionally restrict to a user's categories."""
from __future__ import annotations

from typing import Any

from ..settings import Settings
from .analysis import category_health, trend_radar
from .config import load_mi_config
from .store import MiStore


def mi_report(store: MiStore, settings: Settings, allowed: set[str] | None = None, include_stable: bool = False, lang: str = "en") -> dict[str, Any]:
    """allowed=None means every category (ADMIN). Rows with no category are visible to unrestricted users only.
    Shares are computed across all categories first, so a restricted user's numbers match what an admin sees."""
    cfg = load_mi_config(settings.mi)
    rows = store.observations(cfg.history_periods)
    health = category_health(rows, store.supply_by_category(), cfg, lang)
    radar = trend_radar(rows, health, cfg, include_stable, lang)
    if allowed is not None:
        health = [h for h in health if h["category"] in allowed]
        radar = [r for r in radar if r["category"] in allowed]
    periods = sorted({r["period_start"] for r in rows})
    return {"health": health, "radar": radar, "as_of": periods[-1].isoformat() if periods else None,
            "signals": sorted({r["signal_type"] for r in rows})}
