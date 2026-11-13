import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from marketflow.cli import main
from marketflow.db import init_sqlite, insert_id, make_engine
from marketflow.migrate import migrate_sqlite_to_duckdb
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider

TP = FixedTimeProvider(datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc))


@pytest.fixture()
def old_db(tmp_path):
    p = tmp_path / "old.db"
    e = make_engine(f"sqlite:///{p}"); init_sqlite(e)
    with e.begin() as c:
        c.execute(text("INSERT INTO users (user_id, email, name, role, password_hash) VALUES (7,'a@x.io','A','ADMIN','h')"))
        c.execute(text("INSERT INTO ext_public_benchmarks (recorded_date, source_type, std_item_id, raw_item_name, avg_price, min_price, max_price, normalized_price, extra_metrics) "
                       "VALUES ('2026-11-02','KAMIS',1,'apple',1234.5,1000,1500,1234.5,'{}')"))
    e.dispose()
    return p


def test_copies_rows_keeps_ids_and_continues_the_sequence(old_db, tmp_path):
    dst = tmp_path / "new.duckdb"
    copied = migrate_sqlite_to_duckdb(old_db, dst)
    assert copied["users"] == 1 and copied["ext_public_benchmarks"] == 1 and copied["master_products"] >= 1
    e = make_engine(f"duckdb:///{dst}")
    with e.begin() as c:
        assert c.execute(text("SELECT user_id, email FROM users")).one() == (7, "a@x.io")
        assert float(c.execute(text("SELECT avg_price FROM ext_public_benchmarks")).scalar()) == 1234.5
        assert insert_id(c, "INSERT INTO users (email, name, role, password_hash) VALUES ('b@x.io','B','MD','h')", {}, "user_id") == 8


def test_refuses_to_overwrite_or_read_a_missing_file(old_db, tmp_path):
    dst = tmp_path / "new.duckdb"; migrate_sqlite_to_duckdb(old_db, dst)
    with pytest.raises(ValueError, match="already exists"):
        migrate_sqlite_to_duckdb(old_db, dst)
    with pytest.raises(ValueError, match="no such SQLite"):
        migrate_sqlite_to_duckdb(tmp_path / "nope.db", tmp_path / "x.duckdb")


def test_cli(old_db, tmp_path):
    out = io.StringIO()
    s = Settings(database_url=f"duckdb:///{tmp_path / 'default.duckdb'}")
    assert main(["migrate-sqlite", str(old_db), str(tmp_path / "t.duckdb")], settings=s, tp=TP, out=out) == 0
    assert "users: 1" in out.getvalue()
    assert not (tmp_path / "default.duckdb").exists()          # the configured database is untouched


def test_duckdb_survives_concurrent_first_connections(tmp_path):
    """The dashboard fires many requests at once; DuckDB used to fail with 'Unique file handle conflict' (HTTP 500)."""
    import concurrent.futures as cf
    from sqlalchemy import text
    from marketflow.db import init_sqlite, make_engine
    e = make_engine(f"duckdb:///{tmp_path / 'c.duckdb'}")
    init_sqlite(e)

    def one(_):
        with e.connect() as c:
            return c.execute(text("SELECT COUNT(*) FROM users")).scalar()
    for _round in range(20):
        with cf.ThreadPoolExecutor(16) as ex:
            assert set(ex.map(one, range(32))) == {0}
