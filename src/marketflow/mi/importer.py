"""Import a signal file (CSV from Excel/Hive/Power BI exports, or XLSX) into mi_observations.

One row per period (and optionally category / keyword); every other numeric column becomes a metric. For example the
weekly search report:   week,keyword,category,searches,buyers,gmv,unitsold   with --type SEARCH.
Re-importing the same period overwrites it. Bad cells are reported with line numbers; good cells still load."""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..erp import ImportResult, RowError, _norm, read_table
from dataclasses import dataclass

from .classify import Classifier
from .config import SIGNAL_TYPES
from .privacy import contains_pii
from .store import MiStore

PERIOD_COLS = {"period", "periodstart", "week", "date", "month", "yyyymm", "yyyymmdd", "기간", "주차", "일자", "월", "날짜"}
CATEGORY_COLS = {"category", "categoryl", "카테고리", "대분류"}
KEYWORD_COLS = {"keyword", "theme", "searchkeyword", "키워드", "테마", "검색어"}
MAX_METRIC_LEN, MAX_TEXT_LEN = 40, 100


def parse_period(v: Any) -> date:
    """2026-10-26 | 2026/10/26 | 20261026 | 202610 | 2026-10 | 2026-W43 | 2026W43. Weeks map to their Monday, months to day 1."""
    if isinstance(v, date):
        return v if not hasattr(v, "date") else v.date()
    s = re.sub(r"\.0+$", "", str(v).strip())          # xlsx stores 202501 as 202501.0
    m = re.fullmatch(r"(\d{4})[-\s]?W(\d{1,2})", s, re.I)
    if m:
        return date.fromisocalendar(int(m[1]), int(m[2]), 1)
    m = re.fullmatch(r"(\d{4})[-/.]?(\d{2})", s)
    if m:
        return date(int(m[1]), int(m[2]), 1)
    m = re.fullmatch(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})(?:[ T].*)?", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    raise ValueError(f"unrecognised period {s!r} (use YYYY-MM-DD, YYYYMM or YYYY-Www)")


def parse_number(v: Any) -> Decimal | None:
    if v is None or str(v).strip() in {"", "-", "null", "NULL", "None"}:
        return None
    s = str(v).strip().replace(",", "").rstrip("%")
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"not a number: {v!r}") from e
    if not d.is_finite() or abs(d) >= Decimal(10) ** 16:
        raise ValueError(f"number out of range: {v!r}")
    return d


def _metric_name(h: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "_", str(h).strip().lower()).strip("_")[:MAX_METRIC_LEN]


@dataclass
class MiImportResult(ImportResult):
    pii_skipped: int = 0          # rows dropped because a keyword or category looked like personal data (never stored)
    classified: int = 0           # rows that arrived without a category and got one from the classifier
    unclassified: int = 0         # keyword rows left without a category (admin-only until someone labels them)


def import_signals(path: str | Path, store: MiStore, signal_type: str, source: str = "") -> MiImportResult:
    signal_type = signal_type.upper()
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(f"unknown signal type {signal_type!r}; use one of {', '.join(SIGNAL_TYPES)}")
    path = Path(path)
    header, rows = read_table(path)
    res = MiImportResult()
    cols = {i: _norm(h) for i, h in enumerate(header)}
    period_i = next((i for i, n in cols.items() if n in PERIOD_COLS), None)
    if period_i is None:
        res.errors.append(RowError(1, "period", f"no period column; name one of: week, date, month, period (got {header})"))
        return res
    cat_i = next((i for i, n in cols.items() if n in CATEGORY_COLS), None)
    kw_i = next((i for i, n in cols.items() if n in KEYWORD_COLS), None)
    metric_cols = {i: _metric_name(header[i]) for i in cols if i not in {period_i, cat_i, kw_i} and _metric_name(header[i])}
    if not metric_cols:
        res.errors.append(RowError(1, "metrics", "no metric columns (every column besides period/category/keyword is a metric)"))
        return res
    batch: dict[tuple, dict[str, Any]] = {}
    classifier = Classifier(*store.classifier_knowledge()) if kw_i is not None else None
    for line, cells in rows:
        cells = list(cells) + [None] * (len(header) - len(cells))
        try:
            period = parse_period(cells[period_i])
        except (ValueError, TypeError) as e:
            res.errors.append(RowError(line, "period", str(e)))
            continue
        cat = str(cells[cat_i] or "").strip()[:MAX_TEXT_LEN] if cat_i is not None else ""
        kw = str(cells[kw_i] or "").strip()[:MAX_TEXT_LEN] if kw_i is not None else ""
        if contains_pii(kw) or contains_pii(cat):
            res.pii_skipped += 1                              # privacy: drop the whole row, keep nothing of it
            continue
        if kw and not cat and classifier is not None:
            guessed = classifier.category_for(kw)
            if guessed:
                cat, res.classified = guessed, res.classified + 1
            else:
                res.unclassified += 1
        for i, metric in metric_cols.items():
            try:
                value = parse_number(cells[i])
            except ValueError as e:
                res.errors.append(RowError(line, metric, str(e)))
                continue
            if value is None:
                continue
            batch[(signal_type, period, cat, kw, metric)] = {
                "signal_type": signal_type, "period_start": period, "category_l": cat, "keyword": kw, "metric": metric,
                "value": value, "source": source[:60]}          # a repeated key inside one file: the last row wins
    res.loaded = store.upsert_observations(list(batch.values()))
    return res
