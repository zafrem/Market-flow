"""F-04-0: advisory-by-default workflow. Recommendations are PENDING until an MD/ADMIN decides; every
transition is audited and applying can be reverted. (No channel write-back yet: apply updates the internal ledger.)"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..ops_repo import OpsRepository
from ..settings import Settings
from ..time_provider import TimeProvider, today_kst
from .config import load_decision_config
from .engine import Context, Recommendation, evaluate

DECIDERS = {"ADMIN", "MD"}


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Actor:
    name: str
    role: str


def _naive_utc(tp: TimeProvider) -> datetime:
    return tp.now().replace(tzinfo=None)


def run_decisions(repo: OpsRepository, settings: Settings, tp: TimeProvider, on: date | None = None,
                  region: str | None = None) -> list[tuple[int, Recommendation]]:
    """Evaluate every inventory row of the day; persist new recommendations (de-duplicated within 24h)."""
    now, day = _naive_utc(tp), on or today_kst(tp)
    created = []
    for row in repo.inventory_on(day):
        cfg = load_decision_config(settings.decision, row["category_l"])
        ctx = Context(
            competitors=repo.competitor_snapshots(row["std_item_id"], now - timedelta(hours=float(cfg.competitor_max_age_hours)), region),
            recent_daily_sales=repo.sales_history(row["internal_sku"], row["channel_name"], day),
            wholesale_change_pct=repo.wholesale_change_pct(row["std_item_id"]),
            last_change_at=repo.last_change_at(row["std_item_id"], row["channel_name"]))
        rec = evaluate(row, ctx, cfg, now)
        if rec is None or repo.has_open_duplicate(rec, now - timedelta(hours=24)):
            continue
        created.append((repo.insert_recommendation(rec, now), rec))
    return created


def _load(repo: OpsRepository, rec_id: int, expect: set[str]) -> dict[str, Any]:
    r = repo.get_recommendation(rec_id)
    if r is None:
        raise WorkflowError(f"recommendation {rec_id} not found")
    if r["status"] not in expect:
        raise WorkflowError(f"recommendation {rec_id} is {r['status']}; expected {sorted(expect)}")
    return r


def _require_decider(actor: Actor) -> None:
    if actor.role not in DECIDERS:
        raise PermissionError(f"role {actor.role} may not decide on price recommendations")


def approve(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor) -> None:
    _require_decider(actor)
    r = _load(repo, rec_id, {"PENDING"})
    repo.set_status(rec_id, "APPROVED", actor.name, _naive_utc(tp))
    repo.audit(actor.name, "RECOMMENDATION_APPROVED", {"status": r["status"]}, {"rec_id": rec_id, "status": "APPROVED"}, _naive_utc(tp))


def reject(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor) -> None:
    _require_decider(actor)
    r = _load(repo, rec_id, {"PENDING"})
    repo.set_status(rec_id, "REJECTED", actor.name, _naive_utc(tp))
    repo.audit(actor.name, "RECOMMENDATION_REJECTED", {"status": r["status"]}, {"rec_id": rec_id, "status": "REJECTED"}, _naive_utc(tp))


def apply(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor) -> None:
    _require_decider(actor)
    r = _load(repo, rec_id, {"APPROVED"})
    sku = r["flags"]["metrics"]["internal_sku"]
    if not repo.set_price(sku, r["channel_name"], r["suggested_price"]):
        raise WorkflowError(f"no inventory row for {sku}/{r['channel_name']}")
    now = _naive_utc(tp)
    repo.set_status(rec_id, "APPLIED", actor.name, now)     # decided_at restarts the cool-down clock
    repo.audit(actor.name, "PRICE_APPLIED", {"sku": sku, "price": r["current_price"]}, {"sku": sku, "price": r["suggested_price"]}, now)


def revert(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor) -> None:
    _require_decider(actor)
    r = _load(repo, rec_id, {"APPLIED"})
    sku = r["flags"]["metrics"]["internal_sku"]
    repo.set_price(sku, r["channel_name"], r["current_price"])
    now = _naive_utc(tp)
    repo.set_status(rec_id, "REVERTED", actor.name, now)
    repo.audit(actor.name, "PRICE_REVERTED", {"sku": sku, "price": r["suggested_price"]}, {"sku": sku, "price": r["current_price"]}, now)
