"""Persistence for benchmark ingestion. SQL repo for MySQL; in-memory twin for tests."""
from __future__ import annotations

import json
from typing import Any, Protocol

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine

# alias -> (std_item_id, standard_unit)
AliasMap = dict[str, tuple[int, str]]


class BenchmarkRepository(Protocol):
    def load_aliases(self, source_type: str) -> AliasMap: ...
    def upsert_benchmarks(self, rows: list[dict[str, Any]]) -> int: ...


class RetailRepository(Protocol):
    def load_aliases(self, source_type: str) -> AliasMap: ...
    def insert_retail(self, rows: list[dict[str, Any]]) -> int: ...


_INSERT_RETAIL = text(
    """
    INSERT INTO ext_retail_prices
      (scraped_at, platform, std_item_id, match_confidence, channel_item_code, product_title, original_price,
       sale_price, normalized_unit_price, is_out_of_stock, badge_info, extraction_source, region_code)
    VALUES (:scraped_at, :platform, :std_item_id, :match_confidence, :channel_item_code, :product_title,
            :original_price, :sale_price, :normalized_unit_price, :is_out_of_stock, :badge_info,
            :extraction_source, :region_code)
    """
)

_UPSERT = text(
    """
    INSERT INTO ext_public_benchmarks
      (recorded_date, source_type, std_item_id, raw_item_name, avg_price, min_price, max_price,
       normalized_price, extra_metrics)
    VALUES (:recorded_date, :source_type, :std_item_id, :raw_item_name, :avg_price, :min_price,
            :max_price, :normalized_price, :extra_metrics) AS new
    ON DUPLICATE KEY UPDATE
      std_item_id = new.std_item_id, avg_price = new.avg_price, min_price = new.min_price,
      max_price = new.max_price, normalized_price = new.normalized_price,
      extra_metrics = new.extra_metrics
    """
)


_UPSERT_SQLITE = text(
    """
    INSERT INTO ext_public_benchmarks
      (recorded_date, source_type, std_item_id, raw_item_name, avg_price, min_price, max_price,
       normalized_price, extra_metrics)
    VALUES (:recorded_date, :source_type, :std_item_id, :raw_item_name, :avg_price, :min_price,
            :max_price, :normalized_price, :extra_metrics)
    ON CONFLICT (recorded_date, source_type, raw_item_name) DO UPDATE SET
      std_item_id = excluded.std_item_id, avg_price = excluded.avg_price, min_price = excluded.min_price,
      max_price = excluded.max_price, normalized_price = excluded.normalized_price,
      extra_metrics = excluded.extra_metrics
    """
)


def _sqlite_value(v):
    # sqlite3 can't bind Decimal; store exact text (NUMERIC affinity keeps precision-safe values)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    return v


class SqlRepository:
    """MySQL (production) or SQLite (local); same behaviour, dialect-specific upsert."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def load_aliases(self, source_type: str) -> AliasMap:
        with self._engine.connect() as c:
            rows = c.execute(
                text(
                    "SELECT a.alias, a.std_item_id, p.standard_unit FROM master_product_aliases a "
                    "JOIN master_products p USING (std_item_id) WHERE a.source_type = :s"
                ),
                {"s": source_type},
            ).all()
        return {r[0]: (r[1], r[2]) for r in rows}

    def upsert_benchmarks(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        payload = [{**r, "extra_metrics": json.dumps(r.get("extra_metrics") or {}, ensure_ascii=False)} for r in rows]
        sqlite = self._engine.dialect.name == "sqlite"
        if sqlite:
            payload = [{k: _sqlite_value(v) for k, v in r.items()} for r in payload]
        with self._engine.begin() as c:
            c.execute(_UPSERT_SQLITE if sqlite else _UPSERT, payload)
        return len(rows)


    def last_retail_price(self, platform: str, code: str) -> int | None:
        with self._engine.connect() as c:
            return c.execute(text("SELECT sale_price FROM ext_retail_prices WHERE platform = :p AND "
                                  "channel_item_code = :c ORDER BY scraped_at DESC LIMIT 1"),
                             {"p": platform, "c": code}).scalar()

    def insert_retail(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        if self._engine.dialect.name == "sqlite":
            rows = [{k: _sqlite_value(v) for k, v in r.items()} for r in rows]
        with self._engine.begin() as c:   # snapshots are append-only time series: every run adds rows
            c.execute(_INSERT_RETAIL, rows)
        return len(rows)


class InMemoryRepository:
    def __init__(self, aliases: dict[str, AliasMap] | None = None):
        self.aliases = aliases or {}
        self.rows: dict[tuple, dict[str, Any]] = {}
        self.retail = []

    def load_aliases(self, source_type: str) -> AliasMap:
        return dict(self.aliases.get(source_type, {}))

    retail: list[dict[str, Any]]

    def last_retail_price(self, platform: str, code: str) -> int | None:
        prices = [r["sale_price"] for r in self.retail if r["platform"] == platform and r["channel_item_code"] == code]
        return prices[-1] if prices else None

    def insert_retail(self, rows: list[dict[str, Any]]) -> int:
        self.retail.extend(dict(r) for r in rows)
        return len(rows)

    def upsert_benchmarks(self, rows: list[dict[str, Any]]) -> int:
        for r in rows:
            self.rows[(r["recorded_date"], r["source_type"], r["raw_item_name"])] = dict(r)
        return len(rows)
