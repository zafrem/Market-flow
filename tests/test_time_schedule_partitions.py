from datetime import date, datetime, time, timezone

import pytest

from marketflow.partitions import (initial_clause, maxvalue_def, missing_months, partition_def,
                                    reorganize_sql)
from marketflow.schedule import next_run
from marketflow.time_provider import KST, FixedTimeProvider, today_kst


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_today_kst_crosses_utc_midnight():
    assert today_kst(FixedTimeProvider(utc(2026, 11, 2, 16, 0))) == date(2026, 11, 3)
    assert today_kst(FixedTimeProvider(utc(2026, 11, 2, 14, 59))) == date(2026, 11, 2)


def test_fixed_provider_requires_aware():
    with pytest.raises(ValueError):
        FixedTimeProvider(datetime(2026, 1, 1))


def test_next_run_picks_following_slot():
    t = [time(6), time(9)]
    assert next_run(datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc), t) == datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)  # 06:00 KST
    assert next_run(datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc), t).astimezone(KST).hour == 9  # strictly after
    assert next_run(datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc), t).astimezone(KST).day == 4   # rolls to tomorrow


def test_partition_sql_forms():
    assert partition_def("date", date(2026, 12, 1)) == "PARTITION p202612 VALUES LESS THAN ('2027-01-01')"
    assert partition_def("days", date(2026, 1, 1)) == "PARTITION p202601 VALUES LESS THAN (TO_DAYS('2026-02-01'))"
    assert "MAXVALUE)" in maxvalue_def("date") and not maxvalue_def("days").endswith(")")
    assert initial_clause("date", date(2026, 1, 1), 2).startswith("RANGE COLUMNS (%s)")


def test_missing_months_extends_contiguously():
    existing = [f"p2026{m:02d}" for m in range(1, 13)] + ["p202701", "pmax"]
    assert missing_months(existing, date(2026, 11, 3), ahead=3) == [date(2027, 2, 1)]
    assert missing_months(["p202611", "pmax"], date(2026, 11, 3), ahead=1) == [date(2026, 12, 1)]
    assert missing_months(["pmax"], date(2026, 11, 3), ahead=1) == [date(2026, 11, 1), date(2026, 12, 1)]
    assert missing_months(existing, date(2026, 1, 1), ahead=3) == []


def test_reorganize_sql_keeps_pmax():
    sql = reorganize_sql("ext_retail_prices", "days", [date(2027, 2, 1)])
    assert sql.startswith("ALTER TABLE ext_retail_prices REORGANIZE PARTITION pmax INTO")
    assert "p202702" in sql and sql.rstrip().endswith(")") and "pmax VALUES LESS THAN MAXVALUE" in sql
