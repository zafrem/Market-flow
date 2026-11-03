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
