"""One-way copy of an existing SQLite MarketFlow database into a new DuckDB file (ids, history and audit rows kept)."""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import text

from .db import SQLITE_DDL, init_sqlite, make_engine

_TABLES = [m.group(1) for s in SQLITE_DDL if (m := re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s))]   # parents before children
_PK = {m.group(1): m.group(2) for s in SQLITE_DDL
       if (m := re.search(r"CREATE TABLE IF NOT EXISTS (\w+) \(\s*(\w+) INTEGER PRIMARY KEY AUTOINCREMENT", s))}


def migrate_sqlite_to_duckdb(src_path: Path | str, dst_path: Path | str, batch: int = 5000) -> dict[str, int]:
    """Create dst (must not exist) with the current schema and copy every table. Returns rows copied per table."""
    src_path, dst_path = Path(src_path), Path(dst_path)
    if not src_path.is_file():
        raise ValueError(f"no such SQLite file: {src_path}")
    if dst_path.exists():
        raise ValueError(f"{dst_path} already exists; migrate into a new file so nothing is overwritten")
    src = make_engine(f"sqlite:///{src_path.resolve()}")
    dst = make_engine(f"duckdb:///{dst_path.resolve()}")
    init_sqlite(dst, seed=False)           # schema only: the source rows (including the catalog) are copied below
    copied: dict[str, int] = {}
    with src.connect() as s, dst.begin() as d:
        have = {r[0] for r in s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        for t in _TABLES:
            if t not in have:
                continue
            dst_cols = [r[0] for r in d.execute(text(f"SELECT column_name FROM information_schema.columns WHERE table_name='{t}' ORDER BY ordinal_position"))]
            src_cols = [r[1] for r in s.execute(text(f"PRAGMA table_info({t})"))]
            cols = [c for c in src_cols if c in dst_cols]
            sql = text(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})")
            n, res = 0, s.execute(text(f"SELECT {','.join(cols)} FROM {t}"))
            while rows := res.mappings().fetchmany(batch):
                d.execute(sql, [dict(r) for r in rows])
                n += len(rows)
            copied[t] = n
            if t in _PK and n:                            # the id sequence must continue after the copied ids
                top = d.execute(text(f"SELECT MAX({_PK[t]}) FROM {t}")).scalar()
                d.execute(text(f"SELECT count(nextval('seq_{t}')) FROM range({int(top)})"))
    src.dispose(); dst.dispose()
    return copied
