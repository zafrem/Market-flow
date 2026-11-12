"""Tunables for the MI module, from the optional [mi] table of settings.toml. Everything has a default."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

SIGNAL_TYPES = ("SEARCH", "BUYER", "SKU", "PRODUCT", "PROMOTION", "MEDIA")
DEFAULT_WEIGHTS = {"search": 0.5, "product": 0.2, "promotion": 0.15, "media": 0.15}


@dataclass(frozen=True)
class MiConfig:
    history_periods: int = 26          # how many periods back the dashboard looks
    min_periods: int = 4               # a keyword needs this many observations before it gets a lifecycle stage
    recent_periods: int = 2            # "recent" window; growth = recent mean vs the same number of periods before
    health_threshold: float = 0.03     # category demand moves less than this = STEADY
    rising_threshold: float = 0.15     # keyword search growth at or above this = rising (at or below -x = fading)
    growing_level: float = 0.4         # rising keywords at/above this volume percentile (within category) are GROWING
    mainstream_level: float = 0.6      # flat keywords at/above this percentile are MAINSTREAM
    mention_saturation: float = 3.0    # recent mentions at which a cross-signal counts as fully present
    lag_max: int = 2                   # search->demand lead check looks 0..lag_max periods ahead
    lead_min_r: float = 0.5
    lead_min_points: int = 6
    tight_days: float = 3.0            # stock cover below this = TIGHT, above surplus_days = SURPLUS
    surplus_days: float = 14.0
    interest_metric: str = "searches"
    gap_top_volume: float = 0.7        # search-sales gap: keywords at/above this volume percentile within their category ...
    gap_low_conversion: float = 0.3    # ... and at/below this conversion percentile (buyers per search) are examined
    gap_min_keywords: int = 5          # percentiles need at least this many keywords with both numbers
    gap_stockout_ratio: float = 0.3    # share of days in the period with zero stock that counts as "out of stock"
    gap_price_gap_pct: float = 5.0     # our unit price this far above the lowest confirmed competitor = price disadvantage
    demand_metrics: tuple[str, ...] = ("gmv", "unitsold")
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))


def load_mi_config(raw: Mapping[str, Any] | None) -> MiConfig:
    raw = dict(raw or {})
    base = MiConfig()
    kw: dict[str, Any] = {}
    for f in ("history_periods", "min_periods", "recent_periods", "lag_max", "lead_min_points", "gap_min_keywords"):
        if f in raw:
            kw[f] = int(raw[f])
    for f in ("health_threshold", "rising_threshold", "growing_level", "mainstream_level", "mention_saturation",
              "lead_min_r", "tight_days", "surplus_days",
              "gap_top_volume", "gap_low_conversion", "gap_stockout_ratio", "gap_price_gap_pct"):
        if f in raw:
            kw[f] = float(raw[f])
    if "interest_metric" in raw:
        kw["interest_metric"] = str(raw["interest_metric"]).lower()
    if "demand_metrics" in raw:
        kw["demand_metrics"] = tuple(str(m).lower() for m in raw["demand_metrics"])
    if "weights" in raw:
        w = {k: float(v) for k, v in dict(raw["weights"]).items()}
        unknown = set(w) - set(DEFAULT_WEIGHTS)
        if unknown or any(v < 0 for v in w.values()):
            raise ValueError(f"[mi.weights] allows {sorted(DEFAULT_WEIGHTS)} with values >= 0; got {raw['weights']}")
        merged = {**DEFAULT_WEIGHTS, **w}
        if sum(merged.values()) <= 0:
            raise ValueError("[mi.weights] must not all be zero")
        kw["weights"] = merged
    cfg = MiConfig(**{**base.__dict__, **kw})
    if cfg.recent_periods < 1 or cfg.min_periods < 2 or cfg.min_periods < cfg.recent_periods + 1:
        raise ValueError("[mi] needs recent_periods >= 1 and min_periods > recent_periods")
    return cfg
