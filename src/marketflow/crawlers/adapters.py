"""The crawlers. Each feeds one kind of data; sources with no public API (Hive reports, Power BI, flyers) use a file inbox:
drop the export into the crawler's folder (or upload it on its page) and the next run imports it."""
from __future__ import annotations

import logging
import re
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as SafeET

from ..collectors.base import Collector
from ..ingest import run_collector
from ..llm import LLMClient
from ..mi.importer import import_signals
from ..mi.store import MiStore
from ..notify import Alert
from ..ops_repo import Repo
from ..repository import SqlRepository
from ..rules import current_rules
from ..scrape.retail import ComplianceError, scrape_platform
from ..scrape.selfheal import SelfHealingExtractor
from ..time_provider import TimeProvider
from ..transport import Transport
from ..vision import MAX_IMAGE_BYTES, OpenAIVision, VisionError
from .base import Crawler, Field, NotConfigured, RunOutcome

log = logging.getLogger(__name__)
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FEED_CHARS = 2_000_000


class InboxCrawler(Crawler):
    kind = "inbox"

    def __init__(self, cid: str, signal: str, times: tuple[str, ...], inbox_root: Path, store: MiStore, tp: TimeProvider):
        self.id, self.data_signal, self.default_times = cid, signal, times
        self.dir, self._store, self._tp = inbox_root / cid, store, tp

    def ensure_dirs(self) -> None:
        for sub in ("", "processed", "failed"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)

    def pending(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(f for f in self.dir.iterdir() if f.is_file() and not f.is_symlink() and f.suffix.lower() in {".csv", ".xlsx"})

    async def run(self, cfg: dict[str, Any]) -> RunOutcome:
        self.ensure_dirs()
        stamp = self._tp.now().strftime("%Y%m%d-%H%M%S")
        files = self.pending()
        rows = bad_cells = failed = 0
        notes: list[str] = []
        for f in files:
            dest = "processed"
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError("file larger than 20 MB")
                res = import_signals(f, self._store, self.data_signal, f"{self.id}:{f.name}")
                rows += res.loaded
                bad_cells += len(res.errors)
                if res.loaded == 0 and res.errors:
                    dest, failed = "failed", failed + 1
                    notes.append(f"{f.name}: {res.errors[0].message}")
            except Exception as e:                   # a broken file is parked in failed/, never retried forever
                dest, failed = "failed", failed + 1
                notes.append(f"{f.name}: {e}")
            shutil.move(str(f), self.dir / dest / f"{stamp}-{f.name}")
        if not files:
            return RunOutcome(0, "No new files in the inbox.")
        msg = f"{len(files)} file(s), {rows} value(s) loaded" + (f", {bad_cells} bad cell(s) skipped" if bad_cells else "")
        if notes:
            msg += "; failed: " + "; ".join(notes)
        return RunOutcome(rows, msg, failed=failed > 0)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class FlyerCrawler(InboxCrawler):
    """Flyers: CSV/XLSX counts as before, plus images read with the OpenAI API. What the reader finds waits on the Flyers tab
    (image beside the items, with the reader's confidence); only items a person approves become PROMOTION mentions. Images
    are sent to OpenAI only while this crawler is enabled and a key is set; without a key they stay in the inbox untouched."""
    fields = (Field("max_images", "int", 10, 50),)

    def __init__(self, inbox_root: Path, store: MiStore, tp: TimeProvider, vision: OpenAIVision | None):
        super().__init__("flyer", "PROMOTION", ("07:30",), inbox_root, store, tp)
        self._vision = vision

    def pending_images(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(f for f in self.dir.iterdir() if f.is_file() and not f.is_symlink() and f.suffix.lower() in IMAGE_SUFFIXES)

    async def run(self, cfg):
        base = await super().run(cfg)
        images = self.pending_images()
        if not images:
            return base
        if self._vision is None:
            return RunOutcome(base.stored, (base.message + "; " if base.stored or base.failed else "") +
                              f"{len(images)} image(s) waiting: set an OpenAI key (vision.openai_api_key) to read them.", failed=base.failed)
        from ..flyers import add_flyer
        now = self._tp.now().replace(tzinfo=None)
        week, stamp = _week_monday(now.date()), now.strftime("%Y%m%d-%H%M%S")
        kept = self.dir / "review"
        kept.mkdir(parents=True, exist_ok=True)
        done = failed = found = 0
        notes: list[str] = []
        for f in images[: int(cfg.get("max_images", 10))]:
            try:
                if f.stat().st_size > MAX_IMAGE_BYTES:
                    raise VisionError("image larger than 5 MB")
                items = await self._vision.read_flyer(f.read_bytes())
                dest = kept / f"{stamp}-{f.name}"
                shutil.move(str(f), dest)                                  # kept: the reviewer needs the image beside the items
                add_flyer(self._store, f.name, str(dest.relative_to(self.dir.parent)), week, items, now)
                done, found = done + 1, found + len(items)
            except Exception as e:                                         # park the file; do not resend a bad image every run
                failed += 1
                notes.append(f"{f.name}: {e}")
                if f.exists():
                    shutil.move(str(f), self.dir / "failed" / f"{stamp}-{f.name}")
        left = len(images) - done - failed
        msg = (base.message + "; " if base.stored or base.failed else "") + f"{done} image(s) read, {found} product(s) waiting for review (Flyers tab)" + \
              (f", {left} left for the next run" if left else "") + ("; failed: " + "; ".join(notes) if notes else "")
        return RunOutcome(base.stored, msg, failed=base.failed or failed > 0)


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _parse_when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s.strip())
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def parse_feed(xml: str) -> list[tuple[str, str, datetime | None]]:
    """(title, summary, published) for RSS 2.0 and Atom. XML is parsed with defusedxml (no entity expansion tricks)."""
    root = SafeET.fromstring(xml)
    items = []
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag not in {"item", "entry"}:
            continue
        get = lambda *names: next((("".join(c.itertext())).strip() for c in it if c.tag.rsplit("}", 1)[-1] in names), "")   # noqa: E731
        items.append((get("title"), re.sub(r"<[^>]+>", " ", get("description", "summary", "content")), _parse_when(get("pubDate", "published", "updated", "date"))))
    return items


class NewsCrawler(Crawler):
    """Counts how many articles per week mention each tracked keyword (every keyword in the latest search data, plus extras)
    and stores the counts as MEDIA signals for the Trend Radar. Reads only the RSS/Atom feeds an admin configured."""
    id, kind, data_signal = "news", "news", "MEDIA"
    default_times = ("07:00", "12:00", "18:00")
    fields = (Field("feeds", "lines", [], 20), Field("extra_keywords", "lines", [], 100), Field("lookback_days", "int", 14, 60))

    def __init__(self, store: MiStore, transport: Transport | None, tp: TimeProvider):
        self._store, self._transport, self._tp = store, transport, tp

    def not_configured(self, cfg):
        return None if cfg.get("feeds") else "Add at least one RSS/Atom feed URL in the settings."

    async def run(self, cfg):
        if (why := self.not_configured(cfg)):
            raise NotConfigured(why)
        assert self._transport is not None
        keywords = self._store.tracked_keywords(200) + [("", k) for k in cfg.get("extra_keywords", [])]
        keywords = [(c, k) for c, k in dict.fromkeys(keywords) if len(k.strip()) >= 2]
        since = self._tp.now().replace(tzinfo=None) - timedelta(days=int(cfg.get("lookback_days", 14)))
        seen: set[str] = set()
        counts: dict[tuple[date, str, str], int] = defaultdict(int)
        ok = bad = articles = 0
        for url in cfg["feeds"]:
            try:
                items = parse_feed((await self._transport.get_text(url))[:MAX_FEED_CHARS])
            except Exception as e:                    # one dead feed must not hide the others
                log.warning("news feed %s failed: %s", url, type(e).__name__)
                bad += 1
                continue
            ok += 1
            for title, summary, when in items:
                if when is None or when < since or title in seen:
                    continue
                seen.add(title)
                articles += 1
                hay = (title + " " + summary).lower()
                for cat, kw in keywords:
                    if kw.lower() in hay:
                        counts[(_week_monday(when.date()), cat, kw)] += 1
        if ok == 0:
            raise RuntimeError(f"all {bad} feed(s) failed")
        rows = [{"signal_type": "MEDIA", "period_start": w, "category_l": c, "keyword": k, "metric": "count", "value": n, "source": "news"}
                for (w, c, k), n in counts.items()]
        # a week's count only ever grows: the feed window rolls forward, so a later run may see fewer old articles
        self._store.upsert_observations(rows, keep_max=True)
        msg = f"{ok} feed(s)" + (f" ({bad} failed)" if bad else "") + f", {articles} article(s), {sum(counts.values())} keyword mention(s) across {len(keywords)} keyword(s)"
        return RunOutcome(len(rows), msg, failed=bad > 0)


class BenchmarkCrawler(Crawler):
    kind = "benchmark"

    def __init__(self, cid: str, collector: Collector | None, engine, tp: TimeProvider, missing: str, times=("06:00", "09:00")):
        self.id, self.default_times, self._collector, self._engine, self._tp, self._missing = cid, tuple(times), collector, engine, tp, missing

    def not_configured(self, cfg):
        return None if self._collector else self._missing

    async def run(self, cfg):
        if (why := self.not_configured(cfg)):
            raise NotConfigured(why)
        res = await run_collector(self._collector, SqlRepository(self._engine), Repo(self._engine).add_review, self._tp.now().replace(tzinfo=None))
        return RunOutcome(res.stored, f"{res.stored} price(s) stored, {len(res.unmapped)} unmapped, {len(res.unconvertible)} unconvertible")


class CompetitorCrawler(Crawler):
    id, kind, default_times = "competitor", "competitor", ("08:00", "13:00", "19:00")

    def __init__(self, engine, transport: Transport | None, tp: TimeProvider, llm_factory):
        self._engine, self._transport, self._tp, self._llm_factory = engine, transport, tp, llm_factory
        self._extractor: SelfHealingExtractor | None = None      # kept across runs: it counts repeated faults to decide when to escalate
        self._faults: list[str] = []

    @staticmethod
    def platforms() -> dict[str, dict[str, Any]]:
        return {p: r for p, r in current_rules().get("retail", {}).items() if r.get("urls")}

    def not_configured(self, cfg):
        return None if self.platforms() else "No retail platform with URLs in config/extraction.json (retail.<platform>.urls)."

    async def run(self, cfg):
        if (why := self.not_configured(cfg)):
            raise NotConfigured(why)
        assert self._transport is not None
        repo, queues = SqlRepository(self._engine), Repo(self._engine)
        if self._extractor is None:
            self._extractor = SelfHealingExtractor(self._llm_factory(), last_price=repo.last_retail_price, alert=self._faults.append,
                                                   on_candidate=lambda plat, cand: queues.add_candidate(plat, cand, self._tp.now().replace(tzinfo=None)))
        extractor = self._extractor
        self._faults.clear()
        stored, failed, alerts, parts = 0, 0, [], []
        for platform in self.platforms():
            try:
                r = await scrape_platform(platform, self._transport, self._tp, extractor, repo, queues.add_review)
            except ComplianceError as e:
                failed += 1
                parts.append(f"{platform}: {e}")
                alerts.append(Alert(f"COLLECTOR_FAILED:{platform}", "RED", f"Retail scrape {platform} refused ({type(e).__name__})."))
                continue
            except Exception as e:                    # one platform failing must not block the rest
                log.exception("retail %s failed", platform)
                failed += 1
                parts.append(f"{platform}: {type(e).__name__}")
                alerts.append(Alert(f"COLLECTOR_FAILED:{platform}", "RED", f"Retail scrape {platform} failed ({type(e).__name__})."))
                continue
            stored += r.stored
            parts.append(f"{platform}: {r.stored} stored")
            if r.candidate_pending:
                alerts.append(Alert(f"SELECTOR_CANDIDATE:{platform}", "AMBER", f"New selectors proposed for {platform}; review them on the dashboard."))
        alerts += [Alert(f"SELECTOR_FAULT:{m.split(':')[0]}", "RED", m) for m in self._faults if "ESCALATE" in m]
        return RunOutcome(stored, "; ".join(parts), alerts, failed=failed > 0)
