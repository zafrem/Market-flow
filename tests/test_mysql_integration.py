"""Needs MySQL initialised from db/init/*.sql. Set MARKETFLOW_TEST_DB_URL to run."""
import os
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from marketflow.ingest import ingest
from marketflow.collectors.base import BenchmarkRecord
from marketflow.partitions import PARTITIONED_TABLES, ensure_partitions
from marketflow.db import make_engine
from marketflow.repository import SqlRepository

URL = os.getenv("MARKETFLOW_TEST_DB_URL")
pytestmark = [pytest.mark.mysql, pytest.mark.skipif(not URL, reason="MARKETFLOW_TEST_DB_URL not set")]


@pytest.fixture()
def engine():
    e = make_engine(URL)
    with e.begin() as c:
        c.execute(text("DELETE FROM ext_public_benchmarks"))
    return e


def rec(price="52300", d=date(2026, 11, 3)):
    p = Decimal(price)
    return BenchmarkRecord(d, "KAMIS", "사과/후지/상품", ("사과",), "10kg", p, p, p)


def test_seed_and_aliases(engine):
    al = SqlRepository(engine).load_aliases("KAMIS")
    assert al["사과"][1] == "100g" and "대파" in al


def test_upsert_is_idempotent_and_updates(engine):
    repo = SqlRepository(engine)
    assert ingest([rec()], repo, "KAMIS").stored == 1
    ingest([rec("60000")], repo, "KAMIS")
    with engine.connect() as c:
        rows = c.execute(text("SELECT avg_price, normalized_price FROM ext_public_benchmarks")).all()
    assert rows == [(Decimal("60000.00"), Decimal("600.00"))]


def test_no_foreign_keys_on_partitioned_tables(engine):
    with engine.connect() as c:
        n = c.execute(text(
            "SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_schema=DATABASE() "
            "AND constraint_type='FOREIGN KEY' AND table_name IN :t").bindparams(
            __import__("sqlalchemy").bindparam("t", expanding=True)), {"t": list(PARTITIONED_TABLES)}).scalar()
    assert n == 0


@pytest.mark.parametrize("table,col,lo,hi", [
    ("ext_public_benchmarks", "recorded_date", "2026-11-01", "2026-11-30"),
    ("ext_retail_prices", "scraped_at", "2026-11-01 00:00:00", "2026-11-30 23:59:59"),
    ("int_operations_inventory", "recorded_date", "2026-11-01", "2026-11-30"),
])
def test_partition_pruning(engine, table, col, lo, hi):
    with engine.connect() as c:
        row = c.execute(text(f"EXPLAIN SELECT * FROM {table} WHERE {col} BETWEEN :lo AND :hi"),
                        {"lo": lo, "hi": hi}).mappings().one()
    assert row["partitions"] == "p202611"


def test_ensure_partitions_extends_and_is_idempotent(engine):
    first = ensure_partitions(engine, date(2027, 6, 15), ahead=3)
    assert first["ext_retail_prices"] == ["p202707", "p202708", "p202709"]
    assert all(v == [] for v in ensure_partitions(engine, date(2027, 6, 15), ahead=3).values())
    with engine.begin() as c:   # pmax still catches rows, and new month prunes correctly
        c.execute(text("INSERT INTO ext_public_benchmarks (recorded_date,source_type,std_item_id,raw_item_name,"
                       "avg_price,min_price,max_price,normalized_price) VALUES ('2027-08-02','T',1,'x',1,1,1,1)"))
        part = c.execute(text("EXPLAIN SELECT * FROM ext_public_benchmarks WHERE recorded_date='2027-08-02'")).mappings().one()["partitions"]
        c.execute(text("DELETE FROM ext_public_benchmarks WHERE source_type='T'"))
    assert part == "p202708"
