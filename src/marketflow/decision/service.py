"""F-04-0: advisory-by-default workflow. Recommendations are PENDING until an MD/ADMIN decides; every
transition is audited and applying can be reverted. (No channel write-back yet: apply updates the internal ledger.)"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
        user_threshold = getattr(repo, "alert_threshold", lambda _c: None)(row["category_l"])
        if user_threshold is not None:                                 # F-05-3: per-user alert_margin_bep
            cfg = replace(cfg, alert_margin_pct=user_threshold)
        ctx = Context(
            competitors=repo.competitor_snapshots(row["std_item_id"], now - timedelta(hours=float(cfg.competitor_max_age_hours)), region,
                                                   confirmed_only=cfg.require_confirmed_match),
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


def _move(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor, expect: str, new: str, action: str) -> dict[str, Any]:
    """Validate, then claim the transition atomically so concurrent deciders cannot both win."""
    _require_decider(actor)
    r = _load(repo, rec_id, {expect})
    now = _naive_utc(tp)
    if not repo.transition(rec_id, expect, new, actor.name, now):
        raise WorkflowError(f"recommendation {rec_id} was changed by someone else; reload and retry")
    repo.audit(actor.name, action, {"status": expect}, {"rec_id": rec_id, "status": new}, now)
    return r


def approve(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor) -> None:
    _move(repo, tp, rec_id, actor, "PENDING", "APPROVED", "RECOMMENDATION_APPROVED")


def reject(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor) -> None:
    _move(repo, tp, rec_id, actor, "PENDING", "REJECTED", "RECOMMENDATION_REJECTED")


def _guarded_price_change(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor, expect: str, new: str,
                          expect_live: str, target: str, action: str) -> None:
    """Move the price only if the live price still equals what this recommendation was based on."""
    _require_decider(actor)
    r = _load(repo, rec_id, {expect})
    sku = r["flags"]["metrics"]["internal_sku"]
    live = repo.live_price(sku, r["channel_name"])
    if live is None:
        raise WorkflowError(f"no inventory row for {sku}/{r['channel_name']}")
    if live != r[expect_live]:
        raise WorkflowError(f"price of {sku}/{r['channel_name']} is now {live}, not {r[expect_live]}; "
                            f"this recommendation is stale")
    now = _naive_utc(tp)
    if not repo.transition(rec_id, expect, new, actor.name, now):       # decided_at restarts the cool-down clock
        raise WorkflowError(f"recommendation {rec_id} was changed by someone else; reload and retry")
    if not repo.set_price(sku, r["channel_name"], r[target]):
        repo.set_status(rec_id, expect, r["decided_by"] or actor.name, r["decided_at"] or now)   # undo the claim
        raise WorkflowError(f"no inventory row for {sku}/{r['channel_name']}")
    repo.audit(actor.name, action, {"sku": sku, "price": live}, {"sku": sku, "price": r[target]}, now)


def apply(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor) -> None:
    _guarded_price_change(repo, tp, rec_id, actor, "APPROVED", "APPLIED", "current_price", "suggested_price", "PRICE_APPLIED")


def revert(repo: OpsRepository, tp: TimeProvider, rec_id: int, actor: Actor) -> None:
    _guarded_price_change(repo, tp, rec_id, actor, "APPLIED", "REVERTED", "suggested_price", "current_price", "PRICE_REVERTED")


def price_changes(repo: OpsRepository, since: datetime) -> list[dict[str, Any]]:
    """APPLIED recommendations decided at/after `since`, shaped for a channel bulk-price upload file.
    The system never pushes to a channel itself (advisory only); an operator uploads this file."""
    return [{"internal_sku": r["flags"]["metrics"]["internal_sku"], "channel_name": r["channel_name"],
             "new_price": r["suggested_price"], "previous_price": r["current_price"], "rec_id": r["rec_id"],
             "applied_at": _as_dt(r["decided_at"]).isoformat(sep=" "), "applied_by": r["decided_by"]}
            for r in repo.applied_since(since)]


def _as_dt(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
