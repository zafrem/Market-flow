"""Crawler registry + runner + dispatcher. The worker calls dispatch_once() every few seconds; the API only reads and edits
settings and queues manual runs (the worker picks them up)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..collectors.garak import GarakCollector
from ..collectors.kamis import KamisCollector
from ..mi.store import MiStore
from ..notify import Alert
from ..schedule import latest_slot
from ..settings import ROOT, Settings
from ..time_provider import TimeProvider
from ..transport import Transport
from ..vision import OpenAIVision
from .adapters import BenchmarkCrawler, CompetitorCrawler, FlyerCrawler, InboxCrawler, NewsCrawler
from .base import Crawler, NotConfigured, RunOutcome, clean_config, parse_times, to_time
from .store import CrawlerStore

log = logging.getLogger(__name__)
CATCH_UP = timedelta(hours=6)             # a slot missed by more than this (e.g. a long outage) is skipped, not run late

# id, signal, default KST times (docs/MI_FRAMEWORK.md section 7: each existing data source gets its own crawler)
INBOX = (("report", "SEARCH", ("07:30",)), ("buyer", "BUYER", ("07:30",)), ("flyer", "PROMOTION", ("07:30",)), ("newproduct", "PRODUCT", ("07:30",)))
ORDER = ("report", "buyer", "news", "competitor", "flyer", "newproduct", "benchmark_kamis", "benchmark_garak")


def resolve_inbox(root: str) -> Path:
    p = Path(root)
    return p if p.is_absolute() else ROOT / p


class CrawlerService:
    def __init__(self, engine, settings: Settings, tp: TimeProvider, transport: Transport | None = None, llm_factory=None, notifier=None):
        self.store, self.tp, self.settings, self.notifier = CrawlerStore(engine), tp, settings, notifier
        mi = MiStore(engine)
        self._mi = mi
        root = resolve_inbox(settings.inbox_dir)
        crawlers: list[Crawler] = [InboxCrawler(cid, sig, times, root, mi, tp) for cid, sig, times in INBOX if cid != "flyer"]
        crawlers.append(FlyerCrawler(root, mi, tp, OpenAIVision(settings.openai_api_key, settings.openai_model) if settings.vision_enabled else None))
        crawlers.append(NewsCrawler(mi, transport, tp))
        crawlers.append(CompetitorCrawler(engine, transport, tp, llm_factory or (lambda: None)))
        crawlers.append(BenchmarkCrawler("benchmark_kamis", KamisCollector(transport, tp, settings.kamis_api_key, settings.kamis_api_id, settings.kamis_product_cls)
                                         if settings.kamis_enabled else None, engine, tp, "Set sources.kamis.api_key and api_id in config/settings.toml."))
        crawlers.append(BenchmarkCrawler("benchmark_garak", GarakCollector(transport, tp, settings.garak_url, settings.garak_api_key)
                                         if settings.garak_enabled else None, engine, tp, "Set sources.garak.url in config/settings.toml."))
        self.registry: dict[str, Crawler] = {c.id: c for c in sorted(crawlers, key=lambda c: ORDER.index(c.id))}

    def now(self) -> datetime:
        return self.tp.now().replace(tzinfo=None)

    # ---- reading ----------------------------------------------------------------------------------
    def effective(self, c: Crawler) -> dict[str, Any]:
        s = self.store.get_settings(c.id)
        defaults = {f.key: f.default for f in c.fields}
        return {"enabled": s["enabled"], "times": s["times"] if s["times"] is not None else list(c.default_times),
                "config": {**defaults, **s["config"]}, "last_slot": s["last_slot"]}

    def data_as_of(self, c: Crawler) -> str | None:
        if c.data_signal:
            return self._mi.latest_period(c.data_signal)
        if c.id == "competitor":
            r = self._mi._rows("SELECT MAX(scraped_at) AS t FROM ext_retail_prices")
        else:
            r = self._mi._rows("SELECT MAX(recorded_date) AS t FROM ext_public_benchmarks WHERE source_type = :s", s=c.id.split("_")[1].upper())
        return str(r[0]["t"])[:10] if r and r[0]["t"] else None

    def summary(self, c: Crawler, last: dict[str, Any] | None) -> dict[str, Any]:
        eff = self.effective(c)
        why = c.not_configured(eff["config"])
        if why:
            state = "NOT_CONFIGURED"
        elif not eff["enabled"]:
            state = "DISABLED"
        elif last is None:
            state = "NEVER_RUN"
        else:
            state = {"OK": "HEALTHY", "FAILED": "FAILING", "SKIPPED": "NOT_CONFIGURED", "RUNNING": "RUNNING", "QUEUED": "QUEUED"}[last["status"]]
        return {"id": c.id, "kind": c.kind, "signal": c.data_signal, "enabled": eff["enabled"], "times": eff["times"], "state": state,
                "not_configured": why, "last_run": last, "data_as_of": self.data_as_of(c)}

    def list(self) -> list[dict[str, Any]]:
        last = self.store.last_run_per_crawler()
        return [self.summary(c, last.get(c.id)) for c in self.registry.values()]

    def detail(self, cid: str) -> dict[str, Any]:
        c = self.registry[cid]
        runs = self.store.runs(cid, 20)
        out = self.summary(c, runs[0] if runs else None)
        eff = self.effective(c)
        out.update({"config": eff["config"], "fields": [{"key": f.key, "kind": f.kind, "max": f.max} for f in c.fields],
                    "default_times": list(c.default_times), "runs": runs, "last_success": self.store.last_success(cid)})
        if isinstance(c, InboxCrawler):
            out["inbox"] = {"path": str(c.dir), "pending": [f.name for f in c.pending() + (c.pending_images() if isinstance(c, FlyerCrawler) else [])]}
        if isinstance(c, CompetitorCrawler):
            out["platforms"] = {p: {"access_method": r.get("access_method"), "legal_review": bool((r.get("legal_review_ref") or "").strip()), "urls": len(r.get("urls", []))}
                                for p, r in c.platforms().items()}
        return out

    # ---- editing ----------------------------------------------------------------------------------
    def update(self, cid: str, by: str, enabled: bool | None, times: Any, config: Any) -> None:
        c = self.registry[cid]
        cur = self.store.get_settings(cid)
        new_times = parse_times(times) if times is not None else cur["times"]
        new_cfg = {**cur["config"], **clean_config(c.fields, config)} if config is not None else cur["config"]
        self.store.save_settings(cid, cur["enabled"] if enabled is None else enabled, new_times, new_cfg, by, self.now())

    # ---- running ----------------------------------------------------------------------------------
    async def run(self, cid: str, trigger: str, by: str, run_id: int | None = None) -> dict[str, Any] | None:
        """Execute one crawler and record the outcome. Returns None if it is already running."""
        c = self.registry[cid]
        rid = self.store.start_run(cid, trigger, by, self.now(), run_id)
        if rid is None:
            return None
        outcome, status = RunOutcome(), "OK"
        try:
            outcome = await c.run(self.effective(c)["config"])
            status = "FAILED" if outcome.failed else "OK"
        except NotConfigured as e:
            outcome, status = RunOutcome(0, str(e)), "SKIPPED"
        except Exception as e:                    # recorded and alerted; never propagates into the worker loop
            log.exception("crawler %s failed", cid)
            outcome, status = RunOutcome(0, f"{type(e).__name__}: {e}"[:400]), "FAILED"
        self.store.finish_run(rid, status, outcome.stored, outcome.message, self.now())
        alerts = list(outcome.alerts)
        if status == "FAILED":
            alerts.append(Alert(f"CRAWLER_FAILED:{cid}", "RED", f"Crawler '{cid}' failed: {outcome.message}"[:300]))
        if alerts and self.notifier is not None:
            try:
                await self.notifier.dispatch(alerts)
            except Exception:
                log.exception("alert dispatch for crawler %s failed", cid)
        return {"run_id": rid, "status": status, "stored": outcome.stored, "message": outcome.message}

    async def dispatch_once(self) -> list[str]:
        """Run what is due: queued manual runs first, then scheduled slots. Returns the crawler ids that ran."""
        now, ran = self.now(), []
        self.store.reap_stale(now)
        for q in self.store.queued_runs():
            if q["crawler_id"] in self.registry and await self.run(q["crawler_id"], "MANUAL", q["requested_by"], run_id=q["run_id"]):
                ran.append(q["crawler_id"])
        for c in self.registry.values():
            eff = self.effective(c)
            if not eff["enabled"] or not eff["times"]:
                continue
            slot = latest_slot(self.tp.now(), [to_time(t) for t in eff["times"]])
            slot = slot.replace(tzinfo=None) if slot else None
            if slot is None or now - slot > CATCH_UP or (eff["last_slot"] is not None and eff["last_slot"] >= slot):
                continue
            if await self.run(c.id, "SCHEDULED", "scheduler") is not None:
                self.store.set_last_slot(c.id, slot)           # recorded after the run starts: a crash does not loop forever
                ran.append(c.id)
        return ran

    async def loop(self, sleep, interval: float = 30.0) -> None:
        while True:
            try:
                await self.dispatch_once()
            except Exception:
                log.exception("crawler dispatch failed")
            await sleep(interval)
