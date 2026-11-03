"""Persistence for inventory, competitor snapshots, recommendations and the audit log (SQLite or MySQL)."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .repository import _sqlite_value


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def _dt(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


class OpsRepository:
    def __init__(self, engine: Engine):
        self._e = engine
        self._sqlite = engine.dialect.name == "sqlite"

    def _b(self, params: dict[str, Any]) -> dict[str, Any]:
        return {k: _sqlite_value(v) for k, v in params.items()} if self._sqlite else params

    def _rows(self, sql: str, **p: Any) -> list[dict[str, Any]]:
        with self._e.connect() as c:
            return [dict(r) for r in c.execute(text(sql), self._b(p)).mappings().all()]

    # ---- catalog / inventory ------------------------------------------------------------
    def item_by_name(self, name: str) -> dict[str, Any] | None:
        r = self._rows("SELECT std_item_id, category_l, standard_unit FROM master_products WHERE item_name = :n", n=name)
        return r[0] if r else None

    def item_exists(self, std_item_id: int) -> dict[str, Any] | None:
        r = self._rows("SELECT std_item_id, category_l, standard_unit FROM master_products WHERE std_item_id = :i", i=std_item_id)
        return r[0] if r else None

    def replace_inventory(self, rows: list[dict[str, Any]]) -> int:
        """Idempotent load keyed by (recorded_date, internal_sku, channel_name): delete + insert, one transaction."""
        if not rows:
            return 0
        with self._e.begin() as c:
            for r in rows:
                p = self._b({**r, "lifecycle_metric": json.dumps(r["lifecycle_metric"])})
                c.execute(text("DELETE FROM int_operations_inventory WHERE recorded_date=:recorded_date AND "
                               "internal_sku=:internal_sku AND channel_name=:channel_name"), p)
                c.execute(text("INSERT INTO int_operations_inventory (recorded_date, std_item_id, internal_sku, channel_name, "
                               "purchase_cost, fixed_cost, logistics_cost, channel_fee_rate, promo_cost, vat_inclusive, "
                               "pack_std_units, current_selling_price, stock_qty, daily_sales_qty, min_price_floor, "
                               "lifecycle_metric) VALUES (:recorded_date, :std_item_id, :internal_sku, :channel_name, "
                               ":purchase_cost, :fixed_cost, :logistics_cost, :channel_fee_rate, :promo_cost, :vat_inclusive, "
                               ":pack_std_units, :current_selling_price, :stock_qty, :daily_sales_qty, :min_price_floor, "
                               ":lifecycle_metric)"), p)
        return len(rows)

    def inventory_on(self, d: date) -> list[dict[str, Any]]:
        out = []
        for r in self._rows("SELECT i.*, m.category_l FROM int_operations_inventory i JOIN master_products m USING (std_item_id) "
                            "WHERE i.recorded_date = :d ORDER BY i.internal_sku, i.channel_name", d=d):
            for k in ("purchase_cost", "fixed_cost", "logistics_cost", "channel_fee_rate", "promo_cost", "pack_std_units"):
                r[k] = _dec(r[k])
            r["lifecycle_metric"] = json.loads(r["lifecycle_metric"]) if isinstance(r["lifecycle_metric"], str) else r["lifecycle_metric"]
            out.append(r)
        return out

    def sales_history(self, sku: str, channel: str, upto: date, n: int = 3) -> list[int]:
        rows = self._rows("SELECT daily_sales_qty FROM int_operations_inventory WHERE internal_sku=:s AND channel_name=:c "
                          "AND recorded_date <= :d ORDER BY recorded_date DESC LIMIT :n", s=sku, c=channel, d=upto, n=n)
        return [int(r["daily_sales_qty"]) for r in rows]

    def set_price(self, sku: str, channel: str, price: int) -> bool:
        latest = self._rows("SELECT internal_id, recorded_date FROM int_operations_inventory WHERE internal_sku=:s AND "
                            "channel_name=:c ORDER BY recorded_date DESC LIMIT 1", s=sku, c=channel)
        if not latest:
            return False
        with self._e.begin() as c:
            c.execute(text("UPDATE int_operations_inventory SET current_selling_price=:p WHERE internal_id=:i AND recorded_date=:d"),
                      self._b({"p": price, "i": latest[0]["internal_id"], "d": latest[0]["recorded_date"]}))
        return True

    # ---- market context -------------------------------------------------------------------
    def competitor_snapshots(self, std_item_id: int, since: datetime, region: str | None = None) -> list[dict[str, Any]]:
        """Newest snapshot per (platform, channel_item_code) at or after `since`."""
        sql = ("SELECT platform, channel_item_code, normalized_unit_price, is_out_of_stock, match_confidence, "
               "region_code, scraped_at FROM ext_retail_prices WHERE std_item_id=:i AND scraped_at >= :since")
        params: dict[str, Any] = {"i": std_item_id, "since": since}
        if region:
            sql += " AND region_code = :r"
            params["r"] = region
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for r in sorted(self._rows(sql, **params), key=lambda r: str(r["scraped_at"])):
            r["scraped_at"], r["match_confidence"] = _dt(r["scraped_at"]), _dec(r["match_confidence"])
            r["normalized_unit_price"], r["is_out_of_stock"] = _dec(r["normalized_unit_price"]), bool(r["is_out_of_stock"])
            latest[(r["platform"], r["channel_item_code"])] = r
        return list(latest.values())

    def wholesale_change_pct(self, std_item_id: int) -> Decimal | None:
        """Mean normalized benchmark price: newest recorded_date vs the one before it."""
        rows = self._rows("SELECT recorded_date, AVG(normalized_price) AS p FROM ext_public_benchmarks WHERE std_item_id=:i "
                          "GROUP BY recorded_date ORDER BY recorded_date DESC LIMIT 2", i=std_item_id)
        if len(rows) < 2 or _dec(rows[1]["p"]) == 0:
            return None
        return (_dec(rows[0]["p"]) - _dec(rows[1]["p"])) / _dec(rows[1]["p"]) * 100

    # ---- recommendations / audit ------------------------------------------------------------
    def last_change_at(self, std_item_id: int, channel: str) -> datetime | None:
        r = self._rows("SELECT MAX(decided_at) AS t FROM price_recommendations WHERE std_item_id=:i AND channel_name=:c "
                       "AND status IN ('APPLIED','REVERTED')", i=std_item_id, c=channel)
        return _dt(r[0]["t"]) if r else None

    def has_open_duplicate(self, rec, since: datetime) -> bool:
        return bool(self._rows(
            "SELECT 1 AS x FROM price_recommendations WHERE std_item_id=:i AND channel_name=:c AND suggested_price=:s "
            "AND reason_code=:r AND status IN ('PENDING','SUPPRESSED') AND created_at >= :since",
            i=rec.std_item_id, c=rec.channel_name, s=rec.suggested_price, r=rec.reason_code, since=since))

    def insert_recommendation(self, rec, now: datetime) -> int:
        p = self._b({"t": now, "i": rec.std_item_id, "c": rec.channel_name, "cur": rec.current_price,
                     "s": rec.suggested_price, "r": rec.reason_code, "f": json.dumps(rec.flags, ensure_ascii=False),
                     "st": rec.status})
        with self._e.begin() as c:
            res = c.execute(text("INSERT INTO price_recommendations (created_at, std_item_id, channel_name, current_price, "
                                 "suggested_price, reason_code, flags, status) VALUES (:t,:i,:c,:cur,:s,:r,:f,:st)"), p)
            return int(res.lastrowid)

    def get_recommendation(self, rec_id: int) -> dict[str, Any] | None:
        r = self._rows("SELECT * FROM price_recommendations WHERE rec_id=:i", i=rec_id)
        if not r:
            return None
        r[0]["flags"] = json.loads(r[0]["flags"]) if isinstance(r[0]["flags"], str) else (r[0]["flags"] or {})
        return r[0]

    def list_recommendations(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM price_recommendations" + (" WHERE status=:s" if status else "") + " ORDER BY rec_id"
        return self._rows(sql, **({"s": status} if status else {}))

    def set_status(self, rec_id: int, status: str, actor: str, now: datetime) -> None:
        with self._e.begin() as c:
            c.execute(text("UPDATE price_recommendations SET status=:s, decided_by=:a, decided_at=:t WHERE rec_id=:i"),
                      self._b({"s": status, "a": actor, "t": now, "i": rec_id}))

    def audit(self, actor: str, action: str, before: Any, after: Any, now: datetime) -> None:
        with self._e.begin() as c:
            c.execute(text("INSERT INTO audit_log (ts, actor, action, before_state, after_state) VALUES (:t,:a,:ac,:b,:af)"),
                      self._b({"t": now, "a": actor, "ac": action, "b": json.dumps(before), "af": json.dumps(after)}))

    def audit_entries(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM audit_log ORDER BY audit_id")
