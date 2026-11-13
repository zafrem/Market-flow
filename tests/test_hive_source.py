"""Internal data from Hive: read-only queries, secrets stay out of errors, rows go through the same validator as files."""
import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from marketflow.cli import main
from marketflow.db import init_sqlite, make_engine
from marketflow.hive_source import HiveConfig, HiveError, fetch_hive, load_hive_config, validate_query
from marketflow.internal_data import import_table
from marketflow.ops_repo import OpsRepository
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider

TP = FixedTimeProvider(datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc))
COLS = ["t.internal_sku", "t.item_name", "t.channel_name", "t.purchase_cost", "t.fixed_cost", "t.logistics_cost",
        "t.channel_fee_rate", "t.current_selling_price", "t.stock_qty", "t.daily_sales_qty"]
ROWS = [("APPLE-1", "Apple", "SHOPA", 5000, 500, 1000, 11.5, 10000, 100, 10),
        ("NOPE-1", "Unknown item", "SHOPA", 1, 1, 1, 1, 100, 1, 1)]
TABLE = {"host": "hive.internal", "username": "analyst", "queries": {"inventory": "SELECT * FROM mart.inventory"}}


class FakeConn:
    def __init__(self, rows=ROWS, boom=None):
        self.rows, self.boom, self.sql, self.closed = rows, boom, None, False

    def cursor(self):
        return self

    def execute(self, sql):
        if self.boom:
            raise self.boom
        self.sql = sql

    @property
    def description(self):
        return [(c,) for c in COLS]

    def fetchmany(self, n):
        return self.rows[:n]

    def close(self):
        self.closed = True


def cfg(**kw):
    return HiveConfig(**{**load_hive_config(TABLE, {}).__dict__, **kw})


def test_only_a_single_read_only_select_is_allowed():
    validate_query("SELECT a FROM t WHERE x = 'drop table'")          # a word inside a string literal is fine
    validate_query("WITH x AS (SELECT 1) SELECT * FROM x;")
    for bad in ("DROP TABLE t", "INSERT INTO t SELECT 1", "SELECT 1; DROP TABLE t", "UPDATE t SET a=1",
                "SELECT * FROM t; SELECT 2", "SELECT 1 FROM t WHERE 1=1 OR (DELETE FROM t)"):
        with pytest.raises(HiveError):
            validate_query(bad)
    with pytest.raises(HiveError):
        load_hive_config({"host": "h", "queries": {"bad": "DROP TABLE t"}}, {})


def test_env_overrides_file_and_password_is_not_in_repr():
    c = load_hive_config({**TABLE, "password": "from-file"}, {"HIVE_PASSWORD": "from-env", "HIVE_HOST": "other"})
    assert (c.host, c.password) == ("other", "from-env")
    assert "from-env" not in repr(c) and "from-file" not in repr(c)


def test_fetch_returns_headers_without_table_prefix_and_closes():
    conn = FakeConn()
    headers, lines = fetch_hive(cfg(), "inventory", lambda _c: conn)
    assert headers[0] == "internal_sku" and [n for n, _ in lines] == [2, 3] and conn.closed
    assert conn.sql == "SELECT * FROM mart.inventory"


def test_driver_errors_do_not_leak_the_password_or_the_query():
    secret = "S3cret-pw"
    conn = FakeConn(boom=RuntimeError(f"auth failed for analyst:{secret}@hive.internal"))
    with pytest.raises(HiveError) as e:
        fetch_hive(cfg(password=secret), "inventory", lambda _c: conn)
    assert secret not in str(e.value) and "mart.inventory" not in str(e.value) and "RuntimeError" in str(e.value)
    assert conn.closed


def test_unconfigured_unknown_and_too_many_rows():
    with pytest.raises(HiveError, match="not configured"):
        fetch_hive(HiveConfig(), "inventory", lambda _c: FakeConn())
    with pytest.raises(HiveError, match="unknown Hive query"):
        fetch_hive(cfg(), "nope", lambda _c: FakeConn())
    with pytest.raises(HiveError, match="more than 1 rows"):
        fetch_hive(cfg(max_rows=1), "inventory", lambda _c: FakeConn())


def test_rows_are_validated_like_a_file_and_bad_rows_report_their_number(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'h.db'}"); init_sqlite(e)
    repo = OpsRepository(e)
    headers, lines = fetch_hive(cfg(), "inventory", lambda _c: FakeConn())
    res = import_table(headers, lines, repo, Settings(), TP)
    assert res.loaded == 1 and [(x.line, x.field) for x in res.errors] == [(3, "unknown")]
    with e.connect() as c:
        assert c.execute(text("SELECT internal_sku FROM int_operations_inventory")).scalar() == "APPLE-1"


def test_cli_import_hive_refuses_when_unconfigured_and_never_prints_a_password(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path / 'c.db'}")
    out = io.StringIO()
    assert main(["import-hive"], settings=s, tp=TP, out=out) == 2
    assert "not configured" in out.getvalue()
