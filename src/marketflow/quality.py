"""Daily data-quality measures (SRS F-08-N5, N6): is the data coming in, is it understood, is it fresh?

Measured: crawl success over 7 days, hours since the last good run against twice the crawler's own cycle (stale),
titles that could not be parsed or matched, listings still waiting for a person to confirm, and confirmed listings that
have not been seen for a week (probably discontinued or moved: re-check the match). Breaching a threshold raises an
issue; the worker turns issues into alerts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from .crawlers.service import CrawlerService
from .notify import Alert
from .ops_repo import Repo

WINDOW_DAYS = 7


@dataclass(frozen=True)
class QualityThresholds:
    min_success_rate: float = 0.95      # share of finished crawler runs that succeeded
    max_unparseable: int = 20           # open titles whose quantity/unit could not be read
    max_unmapped: int = 50              # open titles / benchmark items that match nothing in the catalog
    max_unconfirmed: int = 100          # scraped listings still waiting for a person to grade them
    max_recheck: int = 10               # confirmed listings not seen for WINDOW_DAYS


def load_thresholds(raw: Mapping[str, Any] | None) -> QualityThresholds:
    raw = dict(raw or {})
    base = QualityThresholds()
    try:
        return QualityThresholds(**{k: type(getattr(base, k))(raw[k]) for k in base.__dict__ if k in raw})
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid [quality] setting: {e}") from e


def cycle_hours(times: list[str]) -> float:
    """Longest wait between two scheduled runs (a crawler that runs once a day has a 24 h cycle)."""
    mins = sorted({int(t[:2]) * 60 + int(t[3:5]) for t in times})
    if len(mins) < 2:
        return 24.0
    gaps = [b - a for a, b in zip(mins, mins[1:])] + [mins[0] + 1440 - mins[-1]]
    return max(gaps) / 60


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.rstrip("Z")) if ts else None


def quality_report(repo: Repo, crawlers: CrawlerService, th: QualityThresholds, now: datetime) -> dict[str, Any]:
    since = now - timedelta(days=WINDOW_DAYS)
    out_crawlers, issues = [], []
    for c in crawlers.registry.values():
        eff = crawlers.effective(c)
        runs = crawlers.store._rows("SELECT status, COUNT(*) AS n FROM crawler_runs WHERE crawler_id=:c AND finished_at >= :s "
                                    "AND status IN ('OK','FAILED') GROUP BY status", c=c.id, s=since)
        ok = next((int(r["n"]) for r in runs if r["status"] == "OK"), 0)
        failed = next((int(r["n"]) for r in runs if r["status"] == "FAILED"), 0)
        rate = ok / (ok + failed) if ok + failed else None
        last = _parse(crawlers.store.last_success(c.id))
        cyc = cycle_hours(eff["times"])
        configured = eff["enabled"] and not c.not_configured(eff["config"])
        age = None if last is None else (now - last).total_seconds() / 3600
        stale = bool(configured and (last is None or age > 2 * cyc))
        out_crawlers.append({"id": c.id, "enabled": eff["enabled"], "configured": not c.not_configured(eff["config"]), "ok": ok, "failed": failed,
                             "success_rate": None if rate is None else round(rate, 3), "last_success_hours": None if age is None else round(age, 1),
                             "cycle_hours": round(cyc, 1), "stale": stale})
        if configured and rate is not None and rate < th.min_success_rate:
            issues.append({"code": "LOW_SUCCESS_RATE", "subject": c.id})
        if stale and last is not None:
            issues.append({"code": "STALE_DATA", "subject": c.id})
    review = {r["kind"]: int(r["n"]) for r in repo._rows("SELECT kind, COUNT(*) AS n FROM review_queue WHERE status='OPEN' GROUP BY kind")}
    unconfirmed = int(repo._rows("SELECT COUNT(*) AS n FROM product_channel_map WHERE reviewed_at IS NULL")[0]["n"])
    recheck = 0
    for r in repo._rows("SELECT c.platform, c.channel_item_code, (SELECT MAX(p.scraped_at) FROM ext_retail_prices p WHERE p.platform = c.platform "
                        "AND p.channel_item_code = c.channel_item_code) AS seen FROM product_channel_map c WHERE c.reviewed_at IS NOT NULL AND c.match_grade = 'SAME'"):
        seen = r["seen"] if r["seen"] is None or isinstance(r["seen"], datetime) else datetime.fromisoformat(str(r["seen"]))
        if seen is None or seen < since:
            recheck += 1
    unparse = review.get("UNPARSEABLE_TITLE", 0) + review.get("UNCONVERTIBLE_BENCHMARK", 0)
    unmapped = review.get("UNMATCHED_TITLE", 0) + review.get("UNMAPPED_BENCHMARK", 0)
    newest = repo._rows("SELECT MAX(scraped_at) AS t FROM ext_retail_prices")[0]["t"]
    newest = newest if newest is None or isinstance(newest, datetime) else datetime.fromisoformat(str(newest))
    for code, value, limit in (("UNPARSEABLE_TITLES", unparse, th.max_unparseable), ("UNMAPPED_ITEMS", unmapped, th.max_unmapped),
                               ("UNCONFIRMED_MATCHES", unconfirmed, th.max_unconfirmed), ("MATCHES_TO_RECHECK", recheck, th.max_recheck)):
        if value > limit:
            issues.append({"code": code, "subject": str(value)})
    return {"as_of": now.isoformat(sep=" "), "window_days": WINDOW_DAYS, "crawlers": out_crawlers, "unparseable_titles": unparse, "unmapped_items": unmapped,
            "unconfirmed_matches": unconfirmed, "matches_to_recheck": recheck,
            "competitor_data_age_hours": None if newest is None else round((now - newest).total_seconds() / 3600, 1), "issues": issues}


def alerts_from_quality(report: dict[str, Any], link: str = "") -> list[Alert]:
    tail = f" {link}" if link else ""
    text = {"LOW_SUCCESS_RATE": "Data quality: crawler '{s}' succeeded in too few runs this week.",
            "STALE_DATA": "Data quality: crawler '{s}' has not delivered fresh data for more than twice its cycle.",
            "UNPARSEABLE_TITLES": "Data quality: {s} titles could not be parsed; see the review queue.",
            "UNMAPPED_ITEMS": "Data quality: {s} items match nothing in the catalog; see the review queue.",
            "UNCONFIRMED_MATCHES": "Data quality: {s} competitor listings wait to be confirmed (Product matches).",
            "MATCHES_TO_RECHECK": "Data quality: {s} confirmed listings have not been seen for a week; re-check their matches."}
    return [Alert(f"DATA_QUALITY:{i['code']}:{i['subject'] if i['code'] in {'LOW_SUCCESS_RATE', 'STALE_DATA'} else ''}", "AMBER", text[i["code"]].format(s=i["subject"]) + tail)
            for i in report["issues"]]
