"""Persistence for MI observations (long format: one row per signal/period/category/keyword/metric)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from ..ops_repo import OpsRepository, _dec

_UPSERT_SQLITE = ("INSERT INTO mi_observations (signal_type, period_start, category_l, keyword, metric, value, source) "
                  "VALUES (:signal_type,:period_start,:category_l,:keyword,:metric,:value,:source) "
                  "ON CONFLICT (signal_type, period_start, category_l, keyword, metric) DO UPDATE SET value=excluded.value, source=excluded.source")
_UPSERT_MYSQL = ("INSERT INTO mi_observations (signal_type, period_start, category_l, keyword, metric, value, source) "
                 "VALUES (:signal_type,:period_start,:category_l,:keyword,:metric,:value,:source) "
                 "ON DUPLICATE KEY UPDATE value=VALUES(value), source=VALUES(source)")


class MiStore(OpsRepository):
    def upsert_observations(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        sql = text(_UPSERT_SQLITE if self._sqlite else _UPSERT_MYSQL)
        with self._e.begin() as c:
            c.execute(sql, [self._b(r) for r in rows])
        return len(rows)

    def observations(self, history_periods: int) -> list[dict[str, Any]]:
        """Rows from the newest `history_periods` distinct periods of each signal type."""
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
