"""Hits the REAL APIs with the tokens in config/settings.toml. Opt in:  MARKETFLOW_LIVE=1 pytest -m live -s"""
import os

import pytest

from marketflow.collectors.garak import GarakCollector
from marketflow.collectors.kamis import KamisCollector
from marketflow.settings import load_settings
from marketflow.time_provider import SystemTimeProvider
from marketflow.transport import HttpTransport

S = load_settings()
pytestmark = [pytest.mark.live, pytest.mark.skipif(os.getenv("MARKETFLOW_LIVE") != "1", reason="set MARKETFLOW_LIVE=1")]


@pytest.mark.skipif(not S.kamis_enabled, reason="KAMIS tokens not configured")
async def test_live_kamis_returns_parseable_prices():
    t = HttpTransport()
    try:
        recs = await KamisCollector(t, SystemTimeProvider(), S.kamis_api_key, S.kamis_api_id, S.kamis_product_cls).collect()
    finally:
        await t.aclose()
    assert recs, "no records parsed - response shape may differ from config/extraction.json"
    assert all(r.avg_price > 0 and r.unit_text for r in recs)


@pytest.mark.skipif(not S.garak_enabled, reason="Garak URL not configured")
async def test_live_garak_returns_parseable_prices():
    t = HttpTransport()
    try:
        recs = await GarakCollector(t, SystemTimeProvider(), S.garak_url, S.garak_api_key).collect()
    finally:
        await t.aclose()
    assert recs, "no records parsed - adapt sources.GARAK in config/extraction.json"
