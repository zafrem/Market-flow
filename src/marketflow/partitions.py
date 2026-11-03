"""Monthly RANGE partition management (SRS §5.1). Pure SQL builders + an `ensure_partitions` job."""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine

# table -> partition style. 'date' = RANGE COLUMNS(date col); 'days' = RANGE (TO_DAYS(datetime col))
PARTITIONED_TABLES: dict[str, str] = {
    "ext_public_benchmarks": "date",
    "ext_retail_prices": "days",
    "int_operations_inventory": "date",
}


def add_months(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) + n
    return date(idx // 12, idx % 12 + 1, 1)


def partition_name(month: date) -> str:
    return f"p{month:%Y%m}"


def partition_def(kind: str, month: date) -> str:
    """Partition holding `month`; upper bound is the first day of the next month."""
    bound = add_months(month, 1).isoformat()
    if kind == "date":
        return f"PARTITION {partition_name(month)} VALUES LESS THAN ('{bound}')"
    if kind == "days":
        return f"PARTITION {partition_name(month)} VALUES LESS THAN (TO_DAYS('{bound}'))"
    raise ValueError(kind)


def maxvalue_def(kind: str) -> str:
    return "PARTITION pmax VALUES LESS THAN (MAXVALUE)" if kind == "date" else "PARTITION pmax VALUES LESS THAN MAXVALUE"


def initial_clause(kind: str, first_month: date, count: int) -> str:
    defs = [partition_def(kind, add_months(first_month, i)) for i in range(count)] + [maxvalue_def(kind)]
    head = "RANGE COLUMNS (%s)" if kind == "date" else "RANGE (TO_DAYS(%s))"
    return head + " (\n  " + ",\n  ".join(defs) + "\n)"


def missing_months(existing: list[str], today: date, ahead: int = 3) -> list[date]:
    """Months to add so partitions cover through `today`'s month + `ahead`."""
    months = sorted(
        date(int(n[1:5]), int(n[5:7]), 1) for n in existing if n != "pmax" and len(n) == 7 and n[1:].isdigit()
    )
    target = add_months(date(today.year, today.month, 1), ahead)
    if not months:
        start = date(today.year, today.month, 1)
    else:
        start = add_months(months[-1], 1)
    out, m = [], start
    while m <= target:
        out.append(m)
        m = add_months(m, 1)
    return out


def reorganize_sql(table: str, kind: str, months: list[date]) -> str:
    defs = [partition_def(kind, m) for m in months] + [maxvalue_def(kind)]
    return f"ALTER TABLE {table} REORGANIZE PARTITION pmax INTO (\n  " + ",\n  ".join(defs) + "\n)"


def ensure_partitions(engine: Engine, today: date, ahead: int = 3) -> dict[str, list[str]]:
    """Idempotently pre-create upcoming monthly partitions. Returns {table: [added names]}."""
    added: dict[str, list[str]] = {}
    with engine.begin() as conn:
        for table, kind in PARTITIONED_TABLES.items():
            rows = conn.execute(
                text(
                    "SELECT partition_name FROM information_schema.partitions "
                    "WHERE table_schema = DATABASE() AND table_name = :t AND partition_name IS NOT NULL"
                ),
                {"t": table},
            ).scalars().all()
            todo = missing_months(list(rows), today, ahead)
            if todo:
                conn.execute(text(reorganize_sql(table, kind, todo)))
            added[table] = [partition_name(m) for m in todo]
    return added
