"""Resolve -> normalize -> upsert pipeline for public benchmark records."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .collectors.base import BenchmarkRecord, Collector
from .repository import BenchmarkRepository
from .units import UnitError, normalize_price

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    stored: int = 0
    unmapped: list[str] = field(default_factory=list)       # no alias -> needs catalog entry
    unconvertible: list[str] = field(default_factory=list)  # unit can't map to standard_unit


def ingest(records: list[BenchmarkRecord], repo: BenchmarkRepository, source_type: str) -> IngestResult:
    aliases = repo.load_aliases(source_type)
    res, rows = IngestResult(), []
    for r in records:
        hit = next((aliases[a] for a in r.alias_candidates if a in aliases), None)
        if hit is None:
            res.unmapped.append(r.raw_item_name)
            continue
        std_item_id, std_unit = hit
        try:
            norm = normalize_price(r.avg_price, r.unit_text, std_unit)
        except UnitError as e:
            log.warning("skip %s: %s", r.raw_item_name, e)
            res.unconvertible.append(r.raw_item_name)
            continue
        rows.append({
            "recorded_date": r.recorded_date, "source_type": r.source_type, "std_item_id": std_item_id,
            "raw_item_name": r.raw_item_name, "avg_price": r.avg_price, "min_price": r.min_price,
            "max_price": r.max_price, "normalized_price": norm,
            "extra_metrics": {**r.extra, "unit": r.unit_text},
        })
    res.stored = repo.upsert_benchmarks(rows)
    return res


async def run_collector(collector: Collector, repo: BenchmarkRepository) -> IngestResult:
    result = ingest(await collector.collect(), repo, collector.source_type)
    log.info("%s: stored=%d unmapped=%d unconvertible=%d", collector.source_type, result.stored,
             len(result.unmapped), len(result.unconvertible))
    return result
