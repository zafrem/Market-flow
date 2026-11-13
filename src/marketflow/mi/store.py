"""Persistence for MI observations (long format: one row per signal/period/category/keyword/metric)."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from ..db import changed
from ..ops_repo import OpsRepository, _dec

_UPSERT_SQLITE = ("INSERT INTO mi_observations (signal_type, period_start, category_l, keyword, metric, value, source) "
                  "VALUES (:signal_type,:period_start,:category_l,:keyword,:metric,:value,:source) "
                  "ON CONFLICT (signal_type, period_start, category_l, keyword, metric) DO UPDATE SET value=excluded.value, source=excluded.source")
_KEEP_MAX_SQLITE = _UPSERT_SQLITE.replace("value=excluded.value", "value=MAX(value, excluded.value)")
_UPSERT_MYSQL = ("INSERT INTO mi_observations (signal_type, period_start, category_l, keyword, metric, value, source) "
                 "VALUES (:signal_type,:period_start,:category_l,:keyword,:metric,:value,:source) "
                 "ON DUPLICATE KEY UPDATE value=VALUES(value), source=VALUES(source)")
_KEEP_MAX_MYSQL = _UPSERT_MYSQL.replace("value=VALUES(value)", "value=GREATEST(value, VALUES(value))")


class MiStore(OpsRepository):
    def upsert_observations(self, rows: list[dict[str, Any]], keep_max: bool = False) -> int:
        """Insert or overwrite; with keep_max an existing larger value wins (for counts that are re-derived from a rolling window)."""
        if not rows:
            return 0
        keep = _KEEP_MAX_SQLITE.replace("MAX(value,", "GREATEST(value,") if self._duck else _KEEP_MAX_SQLITE
        sql = text((keep if keep_max else _UPSERT_SQLITE) if self._sqlite else (_KEEP_MAX_MYSQL if keep_max else _UPSERT_MYSQL))
        with self._e.begin() as c:
            c.execute(sql, [self._b(r) for r in rows])
        return len(rows)

    # ---- synonym dictionary (SRS F-08-2.6) --------------------------------------------------------------------
    def synonyms(self) -> dict[str, str]:
        """alias -> canonical keyword, lower-cased for matching."""
        return {r["alias"].strip().lower(): r["canonical"].strip() for r in self._rows("SELECT alias, canonical FROM mi_synonyms")}

    def list_synonyms(self) -> list[dict[str, Any]]:
        return self._rows("SELECT alias, canonical, updated_by, updated_at FROM mi_synonyms ORDER BY canonical, alias")

    def synonym_impact(self, alias: str) -> int:
        """How many stored (category, keyword) series carry this alias: what the next report will fold into the canonical keyword."""
        r = self._rows("SELECT COUNT(*) AS n FROM (SELECT DISTINCT category_l, keyword FROM mi_observations WHERE LOWER(keyword) = :a) x", a=alias.strip().lower())
        return int(r[0]["n"])

    def set_synonym(self, alias: str, canonical: str, actor: str, now: datetime) -> None:
        a, c = alias.strip(), canonical.strip()
        if not a or not c or a.lower() == c.lower():
            raise ValueError("alias and canonical keyword must be different, non-empty words")
        known = self.synonyms()
        if c.lower() in known:
            raise ValueError(f"{c!r} is itself an alias of {known[c.lower()]!r}; point at that instead")
        if any(v.lower() == a.lower() for v in known.values()):
            raise ValueError(f"{a!r} is already the canonical keyword of other aliases; remove those first")
        with self._e.begin() as conn:
            p = self._b({"a": a, "c": c, "u": actor, "t": now})
            if not changed(conn, "UPDATE mi_synonyms SET canonical=:c, updated_by=:u, updated_at=:t WHERE alias=:a", p):
                conn.execute(text("INSERT INTO mi_synonyms (alias, canonical, updated_by, updated_at) VALUES (:a,:c,:u,:t)"), p)

    def remove_synonym(self, alias: str) -> bool:
        with self._e.begin() as conn:
            return changed(conn, "DELETE FROM mi_synonyms WHERE alias=:a", {"a": alias.strip()}) == 1

    def fold_synonyms(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace aliases by their canonical keyword and add up rows that now share a key (counts, GMV, searches)."""
        syn = self.synonyms()
        if not syn:
            return rows
        merged: dict[tuple, dict[str, Any]] = {}
        for r in rows:
            kw = syn.get((r["keyword"] or "").strip().lower(), r["keyword"])
            key = (r["signal_type"], r["period_start"], r["category_l"], kw, r["metric"])
            if key in merged:
                merged[key]["value"] += r["value"]
            else:
                merged[key] = {**r, "keyword": kw}
        return list(merged.values())

    # ---- classifier inputs --------------------------------------------------------------------------------------
    def classifier_knowledge(self) -> tuple[dict[str, Counter], dict[str, str]]:
        labelled: dict[str, Counter] = defaultdict(Counter)
        for r in self._rows("SELECT DISTINCT category_l, keyword FROM mi_observations WHERE keyword <> '' AND category_l <> ''"):
            labelled[r["keyword"]][r["category_l"]] += 1
        catalog: dict[str, str] = {}
        for r in self._rows("SELECT item_name AS n, category_l FROM master_products UNION SELECT a.alias AS n, m.category_l FROM master_product_aliases a "
                            "JOIN master_products m ON m.std_item_id = a.std_item_id"):
            catalog.setdefault(r["n"], r["category_l"])
        return dict(labelled), catalog

    def observations(self, history_periods: int) -> list[dict[str, Any]]:
        """Rows from the newest `history_periods` distinct periods of each signal type, synonyms folded."""
        return self.fold_synonyms(self._observations(history_periods))

    def period_comparison(self, metric: str) -> dict[tuple[str, str], dict[str, float | None]]:
        """Search `metric` per (category, keyword): latest period, the period before it, and the same period a year earlier
        (nearest period within 7 days of 364 days back). Synonyms folded. Basis for week-over-week / year-over-year."""
        periods = [r["period_start"] for r in self._rows(
            "SELECT DISTINCT period_start FROM mi_observations WHERE signal_type='SEARCH' AND metric=:m ORDER BY period_start DESC", m=metric)]
        periods = [p if isinstance(p, date) else date.fromisoformat(str(p)[:10]) for p in periods]
        if not periods:
            return {}
        latest = periods[0]
        prev = periods[1] if len(periods) > 1 else None
        target = latest - timedelta(days=364)
        near = [p for p in periods if abs((p - target).days) <= 7]
        year = min(near, key=lambda p: abs((p - target).days)) if near else None
        wanted = {"latest": latest, "prev": prev, "year": year}
        rows = []
        for name, p in wanted.items():
            if p is None:
                continue
            for r in self._rows("SELECT signal_type, period_start, category_l, keyword, metric, value FROM mi_observations "
                                "WHERE signal_type='SEARCH' AND metric=:m AND period_start=:p AND keyword <> ''", m=metric, p=p):
                r["period_start"], r["value"] = p, float(_dec(r["value"]))
                r["_which"] = name
                rows.append(r)
        out: dict[tuple[str, str], dict[str, float | None]] = {}
        syn = self.synonyms()
        for r in rows:
            kw = syn.get(r["keyword"].strip().lower(), r["keyword"])
            slot = out.setdefault((r["category_l"], kw), {"latest": None, "prev": None, "year": None})
            slot[r["_which"]] = (slot[r["_which"]] or 0.0) + r["value"]
        return out

    # ---- inputs of the search-sales gap diagnosis ---------------------------------------------------------------
    def period_metric(self, metric: str, period: date) -> dict[tuple[str, str], float]:
        syn = self.synonyms()
        out: dict[tuple[str, str], float] = {}
        for r in self._rows("SELECT category_l, keyword, value FROM mi_observations WHERE signal_type='SEARCH' AND metric=:m AND period_start=:p AND keyword <> ''", m=metric, p=period):
            key = (r["category_l"], syn.get(r["keyword"].strip().lower(), r["keyword"]))
            out[key] = out.get(key, 0.0) + float(_dec(r["value"]))
        return out

    def catalog_names(self) -> list[tuple[str, int]]:
        rows = self._rows("SELECT item_name AS n, std_item_id AS i FROM master_products UNION SELECT alias AS n, std_item_id AS i FROM master_product_aliases")
        return [(r["n"].strip().lower(), int(r["i"])) for r in rows]

    def stockout_ratio(self, item_ids: list[int], start: date, end: date) -> float | None:
        """Share of inventory rows (days x SKUs/channels) of these items that had zero stock, in [start, end)."""
        ids = ",".join(str(int(i)) for i in item_ids)
        r = self._rows(f"SELECT COUNT(*) AS n, SUM(CASE WHEN stock_qty <= 0 THEN 1 ELSE 0 END) AS z FROM int_operations_inventory "
                       f"WHERE std_item_id IN ({ids}) AND recorded_date >= :s AND recorded_date < :e", s=start, e=end)
        n = int(r[0]["n"] or 0)
        return None if n == 0 else float(r[0]["z"] or 0) / n

    def price_gap_pct(self, item_ids: list[int], now: datetime, settings) -> float | None:
        """Worst (largest) percentage by which our latest pack price exceeds the lowest confirmed, in-stock competitor's
        effective price for the same pack. None when nothing is comparable."""
        from ..decision.config import load_decision_config
        worst: float | None = None
        for item in item_ids:
            inv = self._rows("SELECT i.current_selling_price AS price, i.pack_std_units AS pack, m.category_l FROM int_operations_inventory i "
                             "JOIN master_products m ON m.std_item_id = i.std_item_id WHERE i.std_item_id = :i AND i.recorded_date = "
                             "(SELECT MAX(x.recorded_date) FROM int_operations_inventory x WHERE x.internal_sku = i.internal_sku AND x.channel_name = i.channel_name)", i=item)
            if not inv:
                continue
            cfg = load_decision_config(settings.decision, inv[0]["category_l"])
            comps = [c for c in self.competitor_snapshots(item, now - timedelta(hours=float(cfg.competitor_max_age_hours)), None, confirmed_only=cfg.require_confirmed_match)
                     if not c["is_out_of_stock"] and c["match_confidence"] >= cfg.min_match_confidence]
            if not comps:
                continue
            low = min(c["normalized_unit_price"] for c in comps)
            for r in inv:
                theirs = low * _dec(r["pack"])
                if theirs > 0:
                    pct = float((_dec(r["price"]) - theirs) / theirs * 100)
                    worst = pct if worst is None else max(worst, pct)
        return worst

    def _observations(self, history_periods: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for st in [r["signal_type"] for r in self._rows("SELECT DISTINCT signal_type FROM mi_observations")]:
            periods = [r["period_start"] for r in self._rows(
                "SELECT DISTINCT period_start FROM mi_observations WHERE signal_type = :s ORDER BY period_start DESC", s=st)][:history_periods]
            if not periods:
                continue
            first = str(periods[-1])[:10]
            for r in self._rows("SELECT signal_type, period_start, category_l, keyword, metric, value FROM mi_observations "
                                "WHERE signal_type = :s AND period_start >= :f", s=st, f=date.fromisoformat(first)):
                r["period_start"] = r["period_start"] if isinstance(r["period_start"], date) else date.fromisoformat(str(r["period_start"])[:10])
                r["value"] = float(_dec(r["value"]))
                out.append(r)
        return out

    def supply_by_category(self) -> dict[str, dict[str, float]]:
        """Latest inventory per SKU/channel summed by large category: stock and daily sales (for stock-cover days)."""
        rows = self._rows(
            "SELECT m.category_l, SUM(i.stock_qty) AS stock, SUM(i.daily_sales_qty) AS daily FROM int_operations_inventory i "
            "JOIN master_products m ON m.std_item_id = i.std_item_id WHERE i.recorded_date = "
            "(SELECT MAX(x.recorded_date) FROM int_operations_inventory x WHERE x.internal_sku = i.internal_sku AND x.channel_name = i.channel_name) "
            "GROUP BY m.category_l")
        return {r["category_l"]: {"stock": float(r["stock"] or 0), "daily": float(r["daily"] or 0)} for r in rows}

    def tracked_keywords(self, limit: int) -> list[tuple[str, str]]:
        """(category, keyword) pairs from the latest search period, highest volume first."""
        rows = self._rows("SELECT category_l, keyword, value FROM mi_observations WHERE signal_type='SEARCH' AND keyword <> '' AND metric='searches' "
                          "AND period_start = (SELECT MAX(period_start) FROM mi_observations WHERE signal_type='SEARCH') ORDER BY value DESC LIMIT :l", l=limit)
        return [(r["category_l"], r["keyword"]) for r in rows]

    def latest_period(self, signal: str) -> str | None:
        r = self._rows("SELECT MAX(period_start) AS p FROM mi_observations WHERE signal_type = :s", s=signal)
        return str(r[0]["p"])[:10] if r and r[0]["p"] else None
