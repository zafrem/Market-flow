import shutil
from pathlib import Path

import pytest

from marketflow import rules as rules_mod

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_rules(tmp_path, monkeypatch):
    """Each test gets its own copy of the config + overlay so promotions never leak."""
    base = tmp_path / "extraction.json"
    shutil.copy(ROOT / "config" / "extraction.json", base)
    store = rules_mod.RuleStore(base, tmp_path / "overrides.json")
    monkeypatch.setattr(rules_mod, "_default", store)
    return store


@pytest.fixture(autouse=True)
def auto_confirm_matches(request, monkeypatch):
    """Most tests are about pricing, not matching: every listing they store counts as a reviewed SAME match, as if an MD had
    confirmed it. Tests of the confirmation rule itself opt out with @pytest.mark.unconfirmed."""
    if request.node.get_closest_marker("unconfirmed"):
        return
    from datetime import datetime
    from sqlalchemy import text
    from marketflow.repository import SqlRepository
    original = SqlRepository.insert_retail

    def insert_and_confirm(self, rows):
        n = original(self, rows)
        with self._engine.begin() as c:
            for r in {(x["platform"], x["channel_item_code"]): x for x in rows}.values():
                key = {"p": r["platform"], "c": r["channel_item_code"], "i": r["std_item_id"], "t": datetime(2026, 1, 1).isoformat(sep=" ")}
                if not c.execute(text("SELECT 1 FROM product_channel_map WHERE platform=:p AND channel_item_code=:c"), key).first():
                    c.execute(text("INSERT INTO product_channel_map (platform, channel_item_code, std_item_id, match_method, match_confidence, "
                                   "match_grade, reviewed_by, reviewed_at) VALUES (:p,:c,:i,'MANUAL',1,'SAME','test',:t)"), key)
        return n

    monkeypatch.setattr(SqlRepository, "insert_retail", insert_and_confirm)


@pytest.fixture(autouse=True)
def local_store_backend(monkeypatch):
    """MARKETFLOW_TEST_BACKEND=duckdb runs the suite's local-file databases on DuckDB instead of SQLite."""
    import os
    if os.environ.get("MARKETFLOW_TEST_BACKEND") != "duckdb":
        return
    from marketflow import db as db_mod
    real = db_mod.create_engine

    def create(url, *a, **k):
        if isinstance(url, str) and url.startswith("sqlite:///"):
            url = url.replace("sqlite:///", "duckdb:///", 1)
        return real(url, *a, **k)
    monkeypatch.setattr(db_mod, "create_engine", create)
