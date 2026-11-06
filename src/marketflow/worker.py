"""Entrypoint.

  python -m marketflow.worker --once     run every enabled collector now, then exit
  python -m marketflow.worker            run forever: every crawler on its own schedule (dashboard > Crawlers), decisions +30min, alerts
  python -m marketflow.worker --retail   scrape configured retail platforms once (needs legal_review_ref)
  python -m marketflow.worker --check    show which sources are configured (tokens redacted)
"""
from __future__ import annotations

import asyncio
import logging
import sys

from datetime import timedelta

from sqlalchemy.engine import Engine

from .collectors.base import Collector
from .collectors.garak import GarakCollector
from .collectors.kamis import KamisCollector
from .db import init_sqlite, make_engine
from .llm import LLMClient, get_llm
from .rules import current_rules
from .scrape.retail import ComplianceError, RetailResult, scrape_platform
from .scrape.selfheal import SelfHealingExtractor
from .ingest import IngestResult, run_collector
from .decision import service
from .notify import Alert, Notifier, alerts_from_recommendations, alerts_from_stockouts
from .ops_repo import Repo
from .crawlers.service import CrawlerService
from .scheduler import DECIDE_TIMES, Job, Scheduler
from .partitions import ensure_partitions
from .repository import SqlRepository
from .settings import Settings, load_settings
from .time_provider import SystemTimeProvider, TimeProvider, today_kst
from .transport import HttpTransport, Transport

log = logging.getLogger("marketflow.worker")


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
    repo, queues = SqlRepository(engine), Repo(engine)
    results: dict[str, IngestResult | Exception] = {}
    for c in build_collectors(s, transport, tp):
        try:  # a failing source must not block the others
            results[c.source_type] = await run_collector(c, repo, queues.add_review, tp.now().replace(tzinfo=None))
        except Exception as e:
            log.exception("collector %s failed", c.source_type)
            results[c.source_type] = e
    return results


async def run_retail(engine: Engine, transport: Transport, tp: TimeProvider, llm: LLMClient,
                     extractor: SelfHealingExtractor | None = None) -> dict[str, RetailResult | Exception]:
    prepare_db(engine, tp)
    repo, queues = SqlRepository(engine), Repo(engine)
    extractor = extractor or SelfHealingExtractor(
        llm, last_price=repo.last_retail_price,
        on_candidate=lambda plat, cand: queues.add_candidate(plat, cand, tp.now().replace(tzinfo=None)))
    results: dict[str, RetailResult | Exception] = {}
    for platform, rule in current_rules().get("retail", {}).items():
        if not rule.get("urls"):
            continue
        try:
            results[platform] = await scrape_platform(platform, transport, tp, extractor, repo, queues.add_review)
        except ComplianceError as e:
            log.error("%s", e)
            results[platform] = e
        except Exception as e:      # one platform failing must not block the rest
            log.exception("retail %s failed", platform)
            results[platform] = e
    return results


def build_scheduler(engine: Engine, s: Settings, tp: TimeProvider, notifier: Notifier, sleep=asyncio.sleep) -> Scheduler:
    """Fixed-time jobs. Crawlers are not here: they are scheduled per crawler (CrawlerService.loop), so their schedules
    can be edited on the dashboard without a restart."""
    repo = Repo(engine)

    async def decide() -> None:
        prepare_db(engine, tp)
        created = service.run_decisions(repo, s, tp)
        now = tp.now().replace(tzinfo=None)
        await notifier.dispatch(alerts_from_recommendations(created)
                                + alerts_from_stockouts(repo.competitor_stockouts(now - timedelta(hours=26))))

    async def on_failure(job: Job, exc: Exception) -> None:
        await notifier.dispatch([Alert(f"JOB_FAILED:{job.name}", "RED", f"Scheduled job '{job.name}' failed ({type(exc).__name__}).")])

    return Scheduler([Job("decide", DECIDE_TIMES, decide)], tp, sleep=sleep, on_failure=on_failure)


async def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if "--check" in argv:
        print(settings)
        print(f"kamis_enabled={settings.kamis_enabled} garak_enabled={settings.garak_enabled}")
        return 0
    tp, engine, transport = SystemTimeProvider(), make_engine(settings.database_url), HttpTransport()
    try:
        if "--retail" in argv:
            for name, r in (await run_retail(engine, transport, tp, get_llm(settings))).items():
                print(f"{name}: " + (f"FAILED {r}" if isinstance(r, Exception) else
                      f"stored={r.stored} source={r.source} unmatched={len(r.unmatched)} "
                      f"unparseable={len(r.unparseable)} quarantined={r.quarantined} candidate_pending={r.candidate_pending}"))
            return 0
        if "--once" in argv:
            results = await run_jobs(engine, settings, transport, tp)
            for name, r in results.items():
                print(f"{name}: " + (f"FAILED {r!r}" if isinstance(r, Exception) else
                      f"stored={r.stored} unmapped={len(r.unmapped)} unconvertible={len(r.unconvertible)}"))
            return 1 if any(isinstance(r, Exception) for r in results.values()) else 0
        prepare_db(engine, tp)
        notifier = Notifier(settings.alert_webhook_url, Repo(engine), tp)
        crawlers = CrawlerService(engine, settings, tp, transport, lambda: get_llm(settings), notifier)
        await asyncio.gather(build_scheduler(engine, settings, tp, notifier).run_forever(), crawlers.loop(asyncio.sleep))
    finally:
        await transport.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
