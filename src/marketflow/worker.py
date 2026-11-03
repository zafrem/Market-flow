"""Entrypoint.

  python -m marketflow.worker --once     run every enabled collector now, then exit
  python -m marketflow.worker            run forever on the KST schedule (06:00, 09:00)
  python -m marketflow.worker --check    show which sources are configured (tokens redacted)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import time

from sqlalchemy.engine import Engine

from .collectors.base import Collector
from .collectors.garak import GarakCollector
from .collectors.kamis import KamisCollector
from .db import init_sqlite, make_engine
from .ingest import IngestResult, run_collector
from .partitions import ensure_partitions
from .repository import SqlRepository
from .schedule import next_run
from .settings import Settings, load_settings
from .time_provider import SystemTimeProvider, TimeProvider, today_kst
from .transport import HttpTransport, Transport

log = logging.getLogger("marketflow.worker")
BENCHMARK_TIMES = [time(6, 0), time(9, 0)]  # F-01-1, KST


def build_collectors(s: Settings, transport: Transport, tp: TimeProvider) -> list[Collector]:
    out: list[Collector] = []
    if s.kamis_enabled:
        out.append(KamisCollector(transport, tp, s.kamis_api_key, s.kamis_api_id, s.kamis_product_cls))
    else:
        log.warning("KAMIS skipped: set sources.kamis.api_key and api_id in config/settings.toml")
    if s.garak_enabled:
        out.append(GarakCollector(transport, tp, s.garak_url, s.garak_api_key))
    else:
        log.warning("Garak skipped: set sources.garak.url in config/settings.toml")
    return out


def prepare_db(engine: Engine, tp: TimeProvider) -> None:
    if engine.dialect.name == "sqlite":
        init_sqlite(engine)            # idempotent: creates tables + seed on first run
    else:
        ensure_partitions(engine, today_kst(tp))


async def run_jobs(engine: Engine, s: Settings, transport: Transport, tp: TimeProvider) -> dict[str, IngestResult | Exception]:
    prepare_db(engine, tp)
    repo = SqlRepository(engine)
    results: dict[str, IngestResult | Exception] = {}
    for c in build_collectors(s, transport, tp):
        try:  # a failing source must not block the others
            results[c.source_type] = await run_collector(c, repo)
        except Exception as e:
            log.exception("collector %s failed", c.source_type)
            results[c.source_type] = e
    return results


async def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if "--check" in argv:
        print(settings)
        print(f"kamis_enabled={settings.kamis_enabled} garak_enabled={settings.garak_enabled}")
        return 0
    tp, engine, transport = SystemTimeProvider(), make_engine(settings.database_url), HttpTransport()
    try:
        if "--once" in argv:
            results = await run_jobs(engine, settings, transport, tp)
            for name, r in results.items():
                print(f"{name}: " + (f"FAILED {r!r}" if isinstance(r, Exception) else
                      f"stored={r.stored} unmapped={len(r.unmapped)} unconvertible={len(r.unconvertible)}"))
            return 1 if any(isinstance(r, Exception) for r in results.values()) else 0
        while True:
            when = next_run(tp.now(), BENCHMARK_TIMES)
            delay = (when - tp.now()).total_seconds()
            log.info("next run at %s (in %.0fs)", when.isoformat(), delay)
            await asyncio.sleep(max(delay, 0))
            await run_jobs(engine, settings, transport, tp)
    finally:
        await transport.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
