"""Pure analysis for the two MI views. No I/O: feed it observation rows (see MiStore.observations) and supply figures.

Category Health  : is real demand rising or falling, where is it moving, is supply keeping up, and does search lead demand?
Trend Radar      : which keywords are gaining interest, at what lifecycle stage, and who else corroborates it?
Search is treated as *interest*; it only gets called a demand predictor where the data shows it leading demand."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Any, Iterable

from .config import MiConfig

Series = list[tuple[date, float]]
STAGES = ("SEED", "EMERGING", "GROWING", "MAINSTREAM", "FADING")
GROWTH_CAP = 5.0                                   # +500%: from a near-zero base growth is not meaningful beyond this


def _iso(series: Series) -> list[list[str]]:
    return [[d.isoformat(), f"{v:.4f}".rstrip("0").rstrip(".") or "0"] for d, v in series]


def momentum(values: list[float], recent: int) -> float | None:
    """Growth of the last `recent` periods' mean over the `recent` before it; with too little history, last vs previous."""
    n = len(values)
    if n >= 2 * recent:
        cur, prev = sum(values[-recent:]) / recent, sum(values[-2 * recent:-recent]) / recent
    elif n >= 2:
        cur, prev = values[-1], values[-2]
    else:
        return None
    if prev <= 0:
        return GROWTH_CAP if cur > 0 else 0.0
    return min((cur - prev) / prev, GROWTH_CAP)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx, sy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sx * sy)


# One-sided t critical values at p=0.01 by degrees of freedom (we test several lags, so a strict level guards against chance hits).
_T01 = {4: 3.747, 5: 3.365, 6: 3.143, 7: 2.998, 8: 2.896, 9: 2.821, 10: 2.764, 12: 2.681, 15: 2.602, 20: 2.528, 25: 2.485, 30: 2.457}


def critical_r(n: int) -> float:
    """Smallest correlation from n pairs that is unlikely (p<0.01, one-sided) to be chance. Uses the nearest tabulated df at or below."""
    df = n - 2
    t = _T01[max(k for k in _T01 if k <= max(df, 4))] if df < 1000 else 2.326
    return t / math.sqrt(max(df, 4) + t * t) if df >= 4 else 1.0


def _pct_changes(series: Series) -> list[float | None]:
    return [None if prev <= 0 else (cur - prev) / prev for (_, prev), (_, cur) in zip(series, series[1:])]


def leading_indicator(interest: Series, demand: Series, cfg: MiConfig) -> dict[str, Any]:
    """Does a change in searches precede a change in demand? Correlates period-over-period changes at lags 0..lag_max
    (changes, not levels: two trending series correlate by accident)."""
    di = dict(interest)
    shared = [d for d, _ in demand if d in di]
    dd = dict(demand)
    s_ch = _pct_changes([(d, di[d]) for d in shared])
    d_ch = _pct_changes([(d, dd[d]) for d in shared])
    best: tuple[int, float, int] | None = None
    r0 = None
    for lag in range(cfg.lag_max + 1):
        pairs = [(s, d_ch[i + lag]) for i, s in enumerate(s_ch) if i + lag < len(d_ch) and s is not None and d_ch[i + lag] is not None]
        if len(pairs) < cfg.lead_min_points:
            continue
        r = pearson([p[0] for p in pairs], [p[1] for p in pairs])
        if r is None:
            continue
        if lag == 0:
            r0 = r
        if best is None or r > best[1]:
            best = (lag, r, len(pairs))
    if best is None:
        return {"verdict": "INSUFFICIENT", "lag": None, "r": None, "n": 0}
    lag, r, n = best
    if r < max(cfg.lead_min_r, critical_r(n)):
        verdict = "NOT_VERIFIED"
    elif lag >= 1 and r - (r0 if r0 is not None else 0.0) >= 0.1:
        verdict = "LEADS"
    else:
        verdict = "COINCIDENT"
    return {"verdict": verdict, "lag": lag, "r": round(r, 2), "n": n}


class Index:
    """Observations grouped for lookups. Category totals use an explicit total row (keyword '') when a period has one,
    otherwise the sum of that period's keyword rows."""

    def __init__(self, rows: Iterable[dict[str, Any]]):
        self.kw: dict[tuple[str, str, str, str], dict[date, float]] = defaultdict(dict)       # (type, metric, category, keyword)
        explicit: dict[tuple[str, str, str], dict[date, float]] = defaultdict(dict)
        summed: dict[tuple[str, str, str], dict[date, float]] = defaultdict(lambda: defaultdict(float))
        for r in rows:
            t, m, c, k, p, v = r["signal_type"], r["metric"], r["category_l"], r["keyword"], r["period_start"], float(r["value"])
            if k:
                self.kw[(t, m, c, k)][p] = v
                summed[(t, m, c)][p] += v
            else:
                explicit[(t, m, c)][p] = v
        self.total: dict[tuple[str, str, str], dict[date, float]] = {}
        for key in set(explicit) | set(summed):
            merged = dict(summed.get(key, {}))
            merged.update(explicit.get(key, {}))
            self.total[key] = merged

    def category_series(self, signal: str, metric: str, category: str) -> Series:
        return sorted(self.total.get((signal, metric, category), {}).items())

    def categories(self) -> list[str]:
        return sorted({c for (_, _, c) in self.total if c})


def _demand_series(ix: Index, category: str, cfg: MiConfig) -> tuple[Series, str]:
    for metric in cfg.demand_metrics:
        for signal in ("BUYER", "SKU"):
            s = ix.category_series(signal, metric, category)
            if s:
                return s, metric
    return [], ""


def _share(demand_by_cat: dict[str, Series]) -> dict[str, tuple[float, float | None]]:
    """(latest share %, change in percentage points vs the previous period) of total demand across all categories."""
    by_period: dict[date, dict[str, float]] = defaultdict(dict)
    for c, s in demand_by_cat.items():
        for d, v in s:
            by_period[d][c] = v
    periods = sorted(by_period)
    out: dict[str, tuple[float, float | None]] = {}
    if not periods:
        return out

    def shares(p: date) -> dict[str, float]:
        tot = sum(by_period[p].values())
        return {c: v / tot * 100 for c, v in by_period[p].items()} if tot > 0 else {}
    cur, prev = shares(periods[-1]), shares(periods[-2]) if len(periods) > 1 else {}
    for c, v in cur.items():
        out[c] = (v, (v - prev[c]) if c in prev else None)
    return out


def _supply_status(sup: dict[str, float] | None, cfg: MiConfig) -> dict[str, Any] | None:
    if not sup:
        return None
    if sup["daily"] <= 0:
        return {"cover_days": None, "status": "NO_SALES", "stock": sup["stock"]}
    days = sup["stock"] / sup["daily"]
    return {"cover_days": round(days, 1), "stock": sup["stock"],
            "status": "TIGHT" if days < cfg.tight_days else "SURPLUS" if days > cfg.surplus_days else "BALANCED"}


def category_health(rows: list[dict[str, Any]], supply: dict[str, dict[str, float]], cfg: MiConfig) -> list[dict[str, Any]]:
    ix = Index(rows)
    cats = ix.categories()
    demand = {c: _demand_series(ix, c, cfg) for c in cats}
    share = _share({c: s for c, (s, _) in demand.items() if s})
    out = []
    for c in cats:
        interest = ix.category_series("SEARCH", cfg.interest_metric, c)
        dser, dmetric = demand[c]
        dvals = [v for _, v in dser]
        g = momentum(dvals, cfg.recent_periods)
        state = "NO_DATA" if g is None else "GROWING" if g >= cfg.health_threshold else "WEAKENING" if g <= -cfg.health_threshold else "STEADY"
        lead = leading_indicator(interest, dser, cfg) if interest and dser else {"verdict": "INSUFFICIENT", "lag": None, "r": None, "n": 0}
        sup = _supply_status(supply.get(c), cfg)
        sh = share.get(c)
        entry = {"category": c, "demand_metric": dmetric, "demand_state": state, "demand_change": None if g is None else round(g, 4),
                 "demand_series": _iso(dser), "interest_series": _iso(interest),
                 "interest_change": None if not interest else (lambda m: None if m is None else round(m, 4))(momentum([v for _, v in interest], cfg.recent_periods)),
                 "share_pct": None if sh is None else round(sh[0], 1), "share_shift_pp": None if sh is None or sh[1] is None else round(sh[1], 1),
                 "supply": sup, "search_leads_demand": lead}
        entry.update(_narrate_category(entry, cfg))
        out.append(entry)
    order = {"WEAKENING": 0, "GROWING": 1, "STEADY": 2, "NO_DATA": 3}
    return sorted(out, key=lambda e: (order[e["demand_state"]], -abs(e["demand_change"] or 0), e["category"]))


def _pct(x: float) -> str:
    return f"{abs(x) * 100:.0f}%"


def _narrate_category(e: dict[str, Any], cfg: MiConfig) -> dict[str, str]:
    metric = {"gmv": "GMV", "unitsold": "Units sold"}.get(e["demand_metric"], e["demand_metric"] or "Demand")
    if e["demand_state"] == "NO_DATA":
        return {"changed": "Not enough demand data to tell yet.", "why": "Import buyer or SKU data (GMV or units sold) for this category.",
                "action": "Import demand data."}
    changed = (f"{metric} is steady (within {_pct(cfg.health_threshold)})" if e["demand_state"] == "STEADY"
               else f"{metric} {'rose' if e['demand_state'] == 'GROWING' else 'fell'} {_pct(e['demand_change'])}")
    if e["share_shift_pp"] is not None and abs(e["share_shift_pp"]) >= 0.5:
        changed += f"; its share of total demand {'gained' if e['share_shift_pp'] > 0 else 'lost'} {abs(e['share_shift_pp']):.1f} pt"
    lead, sup = e["search_leads_demand"], e["supply"]
    if lead["verdict"] == "LEADS":
        why = f"Searches have led demand by {lead['lag']} period(s) here (r={lead['r']}, n={lead['n']}), so search is a usable early signal."
    elif lead["verdict"] == "COINCIDENT":
        why = f"Searches move together with demand (r={lead['r']}, n={lead['n']}) but do not lead it."
    elif lead["verdict"] == "NOT_VERIFIED":
        why = f"Search has not been verified as a demand predictor here (best r={lead['r']}); read search as interest, not demand."
    else:
        why = "Too little overlapping search and demand history to test whether search leads demand."
    if sup and sup["status"] == "TIGHT":
        why += f" Stock covers only {sup['cover_days']} days of sales."
    elif sup and sup["status"] == "SURPLUS":
        why += f" Stock covers {sup['cover_days']} days of sales."
    tight, surplus = bool(sup and sup["status"] == "TIGHT"), bool(sup and sup["status"] == "SURPLUS")
    if e["demand_state"] == "GROWING":
        action = "Demand is rising while stock is tight: secure supply first." if tight else "Demand is rising: check purchasing plans and promotion room."
    elif e["demand_state"] == "WEAKENING":
        action = "Demand is falling and stock is high: hold purchases and consider clearing stock." if surplus else "Demand is falling: review purchasing and promotion plans."
    else:
        action = "No action needed on demand." + (" Stock is tight; check supply." if tight else "")
    return {"changed": changed, "why": why, "action": action}


MIN_PEERS = 3                                       # a volume percentile among fewer keywords says nothing


def _level_percentiles(levels: dict[str, float]) -> dict[str, float | None]:
    ks = sorted(levels, key=lambda k: levels[k])
    if len(ks) < MIN_PEERS:
        return {k: None for k in ks}
    return {k: sum(1 for o in ks if levels[o] < levels[k]) / (len(ks) - 1) for k in ks}


def trend_radar(rows: list[dict[str, Any]], health: list[dict[str, Any]], cfg: MiConfig, include_stable: bool = False) -> list[dict[str, Any]]:
    ix = Index(rows)
    by_cat = {h["category"]: h for h in health}
    # per keyword: search series and, for the corroborating signals, mentions per period (all metrics summed)
    kws: dict[tuple[str, str], Series] = {}
    mentions: dict[tuple[str, str], dict[str, dict[date, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for (t, m, c, k), by_p in ix.kw.items():
        if t == "SEARCH" and m == cfg.interest_metric:
            kws[(c, k)] = sorted(by_p.items())
        elif t in {"PRODUCT", "PROMOTION", "MEDIA"}:
            for p, v in by_p.items():
                mentions[(c, k)][t.lower()][p] += v
    levels_by_cat: dict[str, dict[str, float]] = defaultdict(dict)
    stats: dict[tuple[str, str], tuple[float, float]] = {}
    for key, s in kws.items():
        vals = [v for _, v in s]
        if len(vals) < cfg.min_periods:
            continue
        g = momentum(vals, cfg.recent_periods)
        level = sum(vals[-cfg.recent_periods:]) / cfg.recent_periods
        stats[key] = (g if g is not None else 0.0, level)
        levels_by_cat[key[0]][key[1]] = level
    pct = {c: _level_percentiles(l) for c, l in levels_by_cat.items()}
    out = []
    for (c, k), (g, level) in stats.items():
        s = kws[(c, k)]
        last_periods = [d for d, _ in s][-cfg.recent_periods:]
        recent_mentions = {sig: sum(v for d, v in by_p.items() if d >= last_periods[0]) for sig, by_p in mentions[(c, k)].items()}
        corroborating = sorted(sig for sig, v in recent_mentions.items() if v > 0)
        lp = pct[c][k]
        if g <= -cfg.rising_threshold:
            stage = "FADING"
        elif g >= cfg.rising_threshold:
            stage = "GROWING" if lp is not None and lp >= cfg.growing_level else "EMERGING" if corroborating else "SEED"
        else:
            stage = "MAINSTREAM" if lp is not None and lp >= cfg.mainstream_level else "STABLE"
        w = cfg.weights
        total_w = sum(w.values())
        parts = {"search": min(max(g, 0.0), 1.0)}
        for sig in ("product", "promotion", "media"):
            parts[sig] = min(recent_mentions.get(sig, 0.0) / cfg.mention_saturation, 1.0)
        score = 100 * sum(w[sig] * parts[sig] for sig in parts) / total_w
        if stage in {"FADING", "STABLE"}:
            score = 0.0                                  # the radar ranks what is rising, not what is falling
        entry = {"keyword": k, "category": c, "stage": stage, "growth": round(g, 4), "volume_percentile": None if lp is None else round(lp, 2),
                 "score": round(score, 1), "opportunity": round(score * (1 - (lp if lp is not None else 0.0)), 1),
                 "search_series": _iso(s), "signals": {sig: round(recent_mentions.get(sig, 0.0), 2) for sig in ("product", "promotion", "media")},
                 "corroborating": corroborating}
        entry.update(_narrate_keyword(entry, by_cat.get(c), cfg))
        if stage != "STABLE" or include_stable:
            out.append(entry)
    return sorted(out, key=lambda e: (-e["opportunity"] if e["stage"] in {"SEED", "EMERGING", "GROWING"} else 1, STAGES.index(e["stage"]) if e["stage"] in STAGES else 9, -e["score"], e["keyword"]))


def _narrate_keyword(e: dict[str, Any], cat: dict[str, Any] | None, cfg: MiConfig) -> dict[str, str]:
    g, stage, r = e["growth"], e["stage"], cfg.recent_periods
    changed = (f"Searches {'rose' if g > 0 else 'fell' if g < 0 else 'held'} {_pct(g)}" if abs(g) >= 0.005 else "Searches held flat") + \
              f" (last {r} periods vs the {r} before)."
    lead = (cat or {}).get("search_leads_demand", {}).get("verdict")
    if stage == "SEED":
        why = "Rising from a small base; no product, promotion or media signal confirms it yet."
    elif stage == "EMERGING":
        why = f"Rising from a small base and also showing up in: {', '.join(e['corroborating'])}."
    elif stage == "GROWING":
        why = f"Rising on already-high volume (top {max(1, 100 - round(e['volume_percentile'] * 100))}% in {e['category'] or 'its category'})."
        if e["corroborating"]:
            why += f" Also seen in: {', '.join(e['corroborating'])}."
    elif stage == "FADING":
        why = "Interest is falling."
    elif stage == "MAINSTREAM":
        why = "High, steady volume: established demand rather than a trend."
    else:
        why = "No notable change."
    if stage in {"SEED", "EMERGING", "GROWING"}:
        why += " Search has led demand in this category, so this is a credible early signal." if lead == "LEADS" else \
               " This measures interest, not demand: search is not verified as a demand predictor in this category."
    tight = bool(cat and cat.get("supply") and cat["supply"]["status"] == "TIGHT")
    action = {"SEED": "Watch: keep it on the radar and re-check next period.",
              "EMERGING": "Review assortment: check whether we carry it or can source it.",
              "GROWING": "Check supply and pricing before demand arrives." + (" Category stock is already tight." if tight else ""),
              "FADING": "Avoid new purchase commitments; review promotion plans.",
              "MAINSTREAM": "Keep availability; this is core demand.", "STABLE": "No action."}[stage]
    return {"changed": changed, "why": why, "action": action}
