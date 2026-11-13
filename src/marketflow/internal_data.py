"""F-01-3: internal data (inventory and cost) import from CSV/XLSX files (Excel downloaded from SharePoint) or a Hive query
(hive_source.py). Header aliases come from config (internal.columns).
Valid rows load; bad rows are reported with line numbers. A file missing a required column loads nothing."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .decision.calc import disposal_cost, price_floor
from .decision.config import load_decision_config
from .ops_repo import OpsRepository
from .rules import current_rules
from .settings import Settings
from .time_provider import TimeProvider, today_kst

REQUIRED = ("internal_sku", "channel_name", "purchase_cost", "fixed_cost", "logistics_cost",
            "channel_fee_rate", "current_selling_price", "stock_qty", "daily_sales_qty")
_TRUE, _FALSE = {"1", "true", "y", "yes", "예", "o"}, {"0", "false", "n", "no", "아니오", "x"}


@dataclass(frozen=True)
class RowError:
    line: int
    field: str
    message: str


@dataclass
class ImportResult:
    loaded: int = 0
    errors: list[RowError] = field(default_factory=list)


def _norm(h: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(h or "").strip().lower())


def read_table(path: Path) -> tuple[list[str], list[tuple[int, list[Any]]]]:
    """(header cells, [(line number, cells)]) from .csv (utf-8 or cp949) or .xlsx."""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
        wb.close()
    elif suffix == ".csv":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp949")
        rows = list(csv.reader(text.splitlines()))
    else:
        raise ValueError(f"unsupported file type {suffix!r} (use .csv or .xlsx)")
    if not rows:
        return [], []
    return [str(c) for c in rows[0]], [(i + 2, r) for i, r in enumerate(rows[1:]) if any(str(c or "").strip() for c in r)]


def _num(raw: Any, name: str, *, integer: bool = False, max_: Decimal | None = None) -> Decimal:
    s = re.sub(r"[,\s원₩%]", "", str(raw if raw is not None else ""))
    if s == "":
        raise ValueError(f"{name} is required")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{name}: {raw!r} is not a number") from None
    if d < 0:
        raise ValueError(f"{name} must be >= 0")
    if integer and d != d.to_integral_value():
        raise ValueError(f"{name} must be a whole number")
    if max_ is not None and d > max_:
        raise ValueError(f"{name} must be <= {max_}")
    return d


def _date(raw: Any, default: date) -> date:
    if raw is None or str(raw).strip() == "":
        return default
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(re.sub(r"[./]", "-", str(raw).strip()))
    except ValueError:
        raise ValueError(f"recorded_date: {raw!r} is not YYYY-MM-DD") from None


def import_internal(path: Path | str, repo: OpsRepository, settings: Settings, tp: TimeProvider,
                    recorded_date: date | None = None) -> ImportResult:
    """Load a CSV/XLSX file."""
    headers, lines = read_table(Path(path))
    return import_table(headers, lines, repo, settings, tp, recorded_date)


def import_table(headers: list[str], lines: list[tuple[int, list[Any]]], repo: OpsRepository, settings: Settings,
                 tp: TimeProvider, recorded_date: date | None = None) -> ImportResult:
    """Validate and load rows from any source (file or Hive); `lines` is [(line number, cells)]."""
    res = ImportResult()
    columns = current_rules()["internal"]["columns"]
    index: dict[str, int] = {}
    for canon, aliases in columns.items():
        wanted = {_norm(a) for a in aliases} | {_norm(canon)}
        for i, h in enumerate(headers):
            if _norm(h) in wanted:
                index.setdefault(canon, i)
    missing = [c for c in REQUIRED if c not in index]
    if "item_name" not in index and "std_item_id" not in index:
        missing.append("item_name|std_item_id")
    if missing:
        res.errors = [RowError(1, c, "missing required column") for c in missing]
        return res

    default_day, seen, rows = recorded_date or today_kst(tp), set(), []
    for line, cells in lines:
        def get(name: str) -> Any:
            i = index.get(name)
            return cells[i] if i is not None and i < len(cells) else None

        try:
            day = _date(get("recorded_date"), default_day)
            item = (repo.item_exists(int(_num(get("std_item_id"), "std_item_id", integer=True)))
                    if "std_item_id" in index and str(get("std_item_id") or "").strip()
                    else repo.item_by_name(str(get("item_name") or "").strip()))
            if item is None:
                raise ValueError(f"unknown item {get('item_name') or get('std_item_id')!r} (not in master_products)")
            sku, channel = str(get("internal_sku") or "").strip(), str(get("channel_name") or "").strip()
            if not sku or not channel:
                raise ValueError("internal_sku and channel_name are required")
            key = (day, sku, channel)
            if key in seen:
                raise ValueError(f"duplicate row for {sku}/{channel} on {day}")
            n = {k: _num(get(k), k) for k in ("purchase_cost", "fixed_cost", "logistics_cost")}
            fee = _num(get("channel_fee_rate"), "channel_fee_rate", max_=Decimal(100))
            promo = _num(get("promo_cost") or 0, "promo_cost")
            disposal = _num(get("disposal_cost"), "disposal_cost") if str(get("disposal_cost") or "").strip() else None
            price = int(_num(get("current_selling_price"), "current_selling_price", integer=True))
            stock = int(_num(get("stock_qty"), "stock_qty", integer=True))
            sales = int(_num(get("daily_sales_qty"), "daily_sales_qty", integer=True))
            pack = _num(get("pack_std_units") or 1, "pack_std_units")
            if pack == 0:
                raise ValueError("pack_std_units must be > 0")
            vat_raw = str(get("vat_inclusive") if get("vat_inclusive") is not None else "1").strip().lower()
            if vat_raw not in _TRUE | _FALSE:
                raise ValueError(f"vat_inclusive: {vat_raw!r} is not a boolean")
            vat = vat_raw in _TRUE
            if str(get("min_price_floor") or "").strip():
                floor = int(_num(get("min_price_floor"), "min_price_floor", integer=True))
            else:
                cfg = load_decision_config(settings.decision, item["category_l"])
                floor = price_floor(n["purchase_cost"], n["fixed_cost"], n["logistics_cost"], promo, fee, vat, cfg,
                                    disposal=disposal_cost(n["purchase_cost"], disposal, cfg.disposal_rate_pct))
            life = {k: (int(_num(get(k), k, integer=True)) if str(get(k) or "").strip() else None)
                    for k in ("remaining_days", "holding_age_days")}
        except ValueError as e:
            name = str(e).split(" ")[0].rstrip(":")
            res.errors.append(RowError(line, name, str(e)))
            continue
        seen.add(key)
        rows.append({"recorded_date": day, "std_item_id": item["std_item_id"], "internal_sku": sku, "channel_name": channel,
                     **n, "channel_fee_rate": fee, "promo_cost": promo, "disposal_cost": disposal, "vat_inclusive": int(vat), "pack_std_units": pack,
                     "current_selling_price": price, "stock_qty": stock, "daily_sales_qty": sales,
                     "min_price_floor": floor, "lifecycle_metric": life})
    res.loaded = repo.replace_inventory(rows, tp.now().replace(tzinfo=None))
    return res
