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
        extraction_source TEXT DEFAULT 'SELECTOR', region_code TEXT NOT NULL,
        coupon_discount INTEGER NOT NULL DEFAULT 0, shipping_fee INTEGER NOT NULL DEFAULT 0,
        bonus_qty NUMERIC NOT NULL DEFAULT 0, effective_unit_price NUMERIC)""",
    """CREATE TABLE IF NOT EXISTS int_operations_inventory (
        internal_id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_date TEXT NOT NULL, std_item_id INTEGER NOT NULL,
        internal_sku TEXT NOT NULL, channel_name TEXT NOT NULL, purchase_cost NUMERIC NOT NULL,
        fixed_cost NUMERIC NOT NULL, logistics_cost NUMERIC NOT NULL, channel_fee_rate NUMERIC NOT NULL,
        promo_cost NUMERIC NOT NULL DEFAULT 0, disposal_cost NUMERIC, vat_inclusive INTEGER NOT NULL DEFAULT 1,
        pack_std_units NUMERIC NOT NULL DEFAULT 1, current_selling_price INTEGER NOT NULL,
        stock_qty INTEGER NOT NULL, daily_sales_qty INTEGER NOT NULL, min_price_floor INTEGER NOT NULL,
        lifecycle_metric TEXT NOT NULL, UNIQUE (recorded_date, internal_sku, channel_name))""",
    "CREATE INDEX IF NOT EXISTS idx_inv_item_date ON int_operations_inventory (std_item_id, recorded_date)",
    """CREATE TABLE IF NOT EXISTS product_channel_map (
        platform TEXT NOT NULL, channel_item_code TEXT NOT NULL,
        std_item_id INTEGER NOT NULL REFERENCES master_products (std_item_id),
        pack_size_factor NUMERIC NOT NULL DEFAULT 1, match_method TEXT NOT NULL, match_confidence NUMERIC NOT NULL,
        match_grade TEXT NOT NULL DEFAULT 'SAME', reviewed_by TEXT, reviewed_at TEXT,
        PRIMARY KEY (platform, channel_item_code))""",
    """CREATE TABLE IF NOT EXISTS price_recommendations (
        rec_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, std_item_id INTEGER NOT NULL,
        channel_name TEXT NOT NULL, current_price INTEGER NOT NULL, suggested_price INTEGER NOT NULL,
        reason_code TEXT NOT NULL, flags TEXT, status TEXT NOT NULL DEFAULT 'PENDING'
          CHECK (status IN ('PENDING','APPROVED','REJECTED','APPLIED','SUPPRESSED','REVERTED')),
        decided_by TEXT, decided_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
        before_state TEXT, after_state TEXT)""",
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('ADMIN','MD','BUYER')), password_hash TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS user_category_assignments (
        user_id INTEGER NOT NULL REFERENCES users (user_id), category_l TEXT NOT NULL, category_m TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, category_l, category_m))""",
    """CREATE TABLE IF NOT EXISTS user_watchlists (
        watch_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users (user_id),
        std_item_id INTEGER NOT NULL REFERENCES master_products (std_item_id), sort_order INTEGER NOT NULL DEFAULT 0,
        UNIQUE (user_id, std_item_id))""",
    """CREATE TABLE IF NOT EXISTS user_preferences (
        pref_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE REFERENCES users (user_id),
        alert_margin_bep NUMERIC NOT NULL DEFAULT 10, ui_layout TEXT)""",
    """CREATE TABLE IF NOT EXISTS review_queue (
        review_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
        item_key TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT, status TEXT NOT NULL DEFAULT 'OPEN'
          CHECK (status IN ('OPEN','RESOLVED')), resolved_by TEXT, resolved_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_review ON review_queue (status, kind, source, item_key)",
    """CREATE TABLE IF NOT EXISTS alert_log (
        alert_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, alert_key TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('SENT','DEAD')), attempts INTEGER NOT NULL, last_error TEXT, message TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_alert ON alert_log (alert_key, status, created_at)",
    """CREATE TABLE IF NOT EXISTS selector_candidates (
        cand_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, platform TEXT NOT NULL, selectors TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','REJECTED')), decided_by TEXT, decided_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS mi_observations (
        obs_id INTEGER PRIMARY KEY AUTOINCREMENT, signal_type TEXT NOT NULL
          CHECK (signal_type IN ('SEARCH','BUYER','SKU','PRODUCT','PROMOTION','MEDIA')),
        period_start TEXT NOT NULL, category_l TEXT NOT NULL DEFAULT '', keyword TEXT NOT NULL DEFAULT '',
        metric TEXT NOT NULL, value NUMERIC NOT NULL, source TEXT NOT NULL DEFAULT '',
        UNIQUE (signal_type, period_start, category_l, keyword, metric))""",
    "CREATE INDEX IF NOT EXISTS idx_mi_period ON mi_observations (signal_type, period_start)",
    """CREATE TABLE IF NOT EXISTS crawler_settings (
        crawler_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, times TEXT, config TEXT,
        last_slot TEXT, updated_by TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS crawler_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT, crawler_id TEXT NOT NULL, trigger_kind TEXT NOT NULL
          CHECK (trigger_kind IN ('SCHEDULED','MANUAL')), requested_by TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('QUEUED','RUNNING','OK','FAILED','SKIPPED')),
        queued_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, rows_loaded INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS idx_crawler_runs ON crawler_runs (crawler_id, run_id)",
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


# Columns added after the first release: an existing development database gets them on start (MySQL: db/migrations).
SQLITE_ADDED_COLUMNS = [
    ("ext_retail_prices", "coupon_discount", "INTEGER NOT NULL DEFAULT 0"),
    ("ext_retail_prices", "shipping_fee", "INTEGER NOT NULL DEFAULT 0"),
    ("ext_retail_prices", "bonus_qty", "NUMERIC NOT NULL DEFAULT 0"),
    ("ext_retail_prices", "effective_unit_price", "NUMERIC"),
    ("int_operations_inventory", "disposal_cost", "NUMERIC"),
]


def init_sqlite(engine: Engine) -> None:
    """Create tables and seed the starter catalog. Idempotent."""
    with engine.begin() as c:
        for stmt in SQLITE_DDL:
            c.execute(text(stmt))
        for table, column, decl in SQLITE_ADDED_COLUMNS:
            have = {r[1] for r in c.execute(text(f"PRAGMA table_info({table})"))}
            if column not in have:
                c.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
        for l, m, name, unit in ITEMS:
            c.execute(text("INSERT OR IGNORE INTO master_products (category_l, category_m, item_name, standard_unit) "
                           "VALUES (:l,:m,:n,:u)"), {"l": l, "m": m, "n": name, "u": unit})
        for src in SOURCES:
            for alias, item in ALIASES.items():
                c.execute(text("INSERT OR IGNORE INTO master_product_aliases (source_type, alias, std_item_id) "
                               "SELECT :s, :a, std_item_id FROM master_products WHERE item_name = :n"),
                          {"s": src, "a": alias, "n": item})
