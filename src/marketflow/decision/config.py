"""Decision parameters (SRS F-04). Defaults here; override in settings.toml [decision] (and per category_l)."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True)
class DecisionConfig:
    vat_rate: Decimal = Decimal("0.10")
    target_min_margin_pct: Decimal = Decimal("0")       # floor = lowest price meeting this net margin
    undercut_pct: Decimal = Decimal("0")                # 0 = match the lowest competitor
    max_step_pct: Decimal = Decimal("10")               # per-run price movement cap
    cooldown_hours: Decimal = Decimal("6")
    wholesale_rise_threshold_pct: Decimal = Decimal("5")
    round_to: int = 10                                  # KRW price tick
    spoilage_buffer_days: Decimal = Decimal("0")
    flash_discount_pct: Decimal = Decimal("10")
    vacuum_min_run_rate_days: Decimal = Decimal("7")
    vacuum_min_pct: Decimal = Decimal("5")
    vacuum_max_pct: Decimal = Decimal("15")
    competitor_max_age_hours: Decimal = Decimal("24")
    min_match_confidence: Decimal = Decimal("0.8")
    alert_margin_pct: Decimal = Decimal("10")
    disposal_rate_pct: Decimal = Decimal("0")           # category average write-off, % of purchase cost (SKU figure wins)
    require_confirmed_match: bool = True                # only reviewed, grade-SAME matches feed prices and alerts (F-08-2.3)


_TYPES = {f.name: f.type for f in fields(DecisionConfig)}


def load_decision_config(raw: Mapping[str, Any] | None, category_l: str | None = None) -> DecisionConfig:
    """`raw` = settings [decision] table; scalar keys are global, sub-table [decision.<category_l>] overrides."""
    raw = raw or {}
    merged = {k: v for k, v in raw.items() if not isinstance(v, Mapping)}
    if category_l and isinstance(raw.get(category_l), Mapping):
        merged.update(raw[category_l])
    cfg = DecisionConfig()
    updates = {}
    for k, v in merged.items():
        if k not in _TYPES:
            raise ValueError(f"unknown [decision] setting: {k!r}")
        if k == "require_confirmed_match":
            if not isinstance(v, bool):
                raise ValueError("invalid [decision]: require_confirmed_match must be true or false")
            updates[k] = v
        else:
            updates[k] = int(v) if k == "round_to" else Decimal(str(v))
    cfg = replace(cfg, **updates)
    if cfg.max_step_pct <= 0 or cfg.round_to <= 0 or not (0 <= cfg.undercut_pct <= 5):
        raise ValueError("invalid [decision]: max_step_pct>0, round_to>0 and 0<=undercut_pct<=5 required")
    if cfg.vacuum_min_pct > cfg.vacuum_max_pct:
        raise ValueError("invalid [decision]: vacuum_min_pct > vacuum_max_pct")
    return cfg
