"""Engine creation and SQLite bootstrap. (MySQL is initialised by db/init/*.sql instead.)"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from .seed import ALIASES, ITEMS, SOURCES
from .settings import ROOT

SQLITE_DDL = [
    """CREATE TABLE IF NOT EXISTS master_products (
        std_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_l TEXT NOT NULL, category_m TEXT NOT NULL, item_name TEXT NOT NULL,
        standard_unit TEXT NOT NULL, barcode TEXT UNIQUE, custom_attributes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (category_l, category_m, item_name))""",
    """CREATE TABLE IF NOT EXISTS master_product_aliases (
        source_type TEXT NOT NULL, alias TEXT NOT NULL,
        std_item_id INTEGER NOT NULL REFERENCES master_products (std_item_id),
        PRIMARY KEY (source_type, alias))""",
    """CREATE TABLE IF NOT EXISTS ext_public_benchmarks (
        benchmark_id INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_date TEXT NOT NULL, source_type TEXT NOT NULL, std_item_id INTEGER NOT NULL,
        raw_item_name TEXT NOT NULL, avg_price NUMERIC NOT NULL, min_price NUMERIC NOT NULL,
        max_price NUMERIC NOT NULL, normalized_price NUMERIC NOT NULL, extra_metrics TEXT,
        UNIQUE (recorded_date, source_type, raw_item_name))""",
    """CREATE TABLE IF NOT EXISTS ext_retail_prices (
        retail_id INTEGER PRIMARY KEY AUTOINCREMENT, scraped_at TEXT NOT NULL, platform TEXT NOT NULL,
        std_item_id INTEGER NOT NULL, match_confidence NUMERIC NOT NULL, channel_item_code TEXT NOT NULL,
        product_title TEXT NOT NULL, original_price INTEGER NOT NULL, sale_price INTEGER NOT NULL,
        normalized_unit_price NUMERIC NOT NULL, is_out_of_stock INTEGER DEFAULT 0, badge_info TEXT,
        extraction_source TEXT DEFAULT 'SELECTOR', region_code TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_retail_item_time ON ext_retail_prices (std_item_id, scraped_at)",
    "CREATE INDEX IF NOT EXISTS idx_bench_item_date ON ext_public_benchmarks (std_item_id, recorded_date)",
]


def resolve_database_url(url: str) -> str:
    """Relative SQLite paths are anchored at the project root so the DB doesn't depend on CWD."""
    u = make_url(url)
    if u.get_backend_name() == "sqlite" and u.database and u.database != ":memory:":
        db = Path(u.database)
        if not db.is_absolute():
            db = ROOT / db
        db.parent.mkdir(parents=True, exist_ok=True)
        return u.set(database=str(db)).render_as_string(hide_password=False)
    return url


def make_engine(url: str) -> Engine:
    url = resolve_database_url(url)
    kwargs = {} if url.startswith("sqlite") else {"pool_pre_ping": True}
    return create_engine(url, future=True, **kwargs)


def init_sqlite(engine: Engine) -> None:
    """Create tables and seed the starter catalog. Idempotent."""
    with engine.begin() as c:
        for stmt in SQLITE_DDL:
            c.execute(text(stmt))
        for l, m, name, unit in ITEMS:
            c.execute(text("INSERT OR IGNORE INTO master_products (category_l, category_m, item_name, standard_unit) "
                           "VALUES (:l,:m,:n,:u)"), {"l": l, "m": m, "n": name, "u": unit})
        for src in SOURCES:
            for alias, item in ALIASES.items():
                c.execute(text("INSERT OR IGNORE INTO master_product_aliases (source_type, alias, std_item_id) "
                               "SELECT :s, :a, std_item_id FROM master_products WHERE item_name = :n"),
                          {"s": src, "a": alias, "n": item})
