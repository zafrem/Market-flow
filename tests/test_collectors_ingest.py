import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from conftest import FIX
from marketflow.collectors.garak import GarakCollector
from marketflow.collectors.kamis import KamisCollector, KamisError
from marketflow.ingest import run_collector
from marketflow.repository import InMemoryRepository
from marketflow.time_provider import FixedTimeProvider
from marketflow.transport import FixtureTransport

# 2026-11-03 07:00 KST == 2026-11-02 22:00 UTC -> recorded_date must be the KST date
TP = FixedTimeProvider(datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc))
ALIASES = {
    "KAMIS": {"사과": (1, "100g"), "양파": (3, "100g"), "배추": (5, "100g"), "무": (6, "100g")},
    "GARAK": {"사과": (1, "100g"), "대파": (7, "100g")},
}


async def test_kamis_end_to_end_with_fixture_transport():
    t = FixtureTransport({"kamis.or.kr": FIX / "kamis_daily.json"})
    repo = InMemoryRepository(ALIASES)
    res = await run_collector(KamisCollector(t, TP, "k", "i"), repo)
    assert t.calls[0][1]["action"] == "dailySalesList"
    assert res.stored == 2                      # 사과, 양파
    assert res.unmapped == ["두리안/상품"]        # no catalog alias
    assert res.unconvertible == ["무/상품"]       # '박스' has no unit conversion; '-' price row dropped
    apple = repo.rows[(datetime(2026, 11, 3).date(), "KAMIS", "사과/후지/상품")]
    assert apple["normalized_price"] == Decimal("523.00")
    assert apple["std_item_id"] == 1


async def test_kamis_is_idempotent():
    t = FixtureTransport({"kamis.or.kr": FIX / "kamis_daily.json"})
    repo = InMemoryRepository(ALIASES)
    c = KamisCollector(t, TP, "k", "i")
    await run_collector(c, repo)
    await run_collector(c, repo)
    assert len(repo.rows) == 2


def test_kamis_error_codes():
    c = KamisCollector(FixtureTransport({}), TP, "k", "i")
    assert c.parse(json.dumps({"data": ["001"]})) == []
    with pytest.raises(KamisError):
        c.parse(json.dumps({"data": ["200"]}))


async def test_garak_min_max_fallback_and_volume():
    t = FixtureTransport({"garak": FIX / "garak_daily.json"})
    repo = InMemoryRepository(ALIASES)
    res = await run_collector(GarakCollector(t, TP, "https://garak.example/api"), repo)
    assert res.stored == 2
    apple = next(r for r in repo.rows.values() if r["raw_item_name"].startswith("사과"))
    assert (apple["min_price"], apple["max_price"]) == (Decimal(45000), Decimal(80000))
    assert apple["extra_metrics"]["inflow_kg"] == "12000"
    assert apple["normalized_price"] == Decimal("400.00")
    green = next(r for r in repo.rows.values() if r["raw_item_name"].startswith("대파"))
    assert green["min_price"] == green["max_price"] == Decimal(2100)
