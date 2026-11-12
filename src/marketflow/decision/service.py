"""F-04-0: advisory-by-default workflow. Recommendations are PENDING until an MD/ADMIN decides; every
transition is audited and applying can be reverted. (No channel write-back yet: apply updates the internal ledger.)"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from ..ops_repo import OpsRepository
from ..settings import Settings
from ..time_provider import TimeProvider, today_kst
from decimal import Decimal

from .calc import disposal_cost, net_margin, round_price, simulate_price_match
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
    _require_decider(actor)
    flags = _load(repo, rec_id, {"PENDING"})["flags"]
    if flags.get("author") and flags["author"] == actor.name:
        raise PermissionError("the author of a price change cannot approve it; ask another approver")
    if flags.get("REQUIRES_ADMIN") and actor.role != "ADMIN":
        raise PermissionError("this change is below the margin defense line: an administrator must approve it")
    _move(repo, tp, rec_id, actor, "PENDING", "APPROVED", "RECOMMENDATION_APPROVED")


def create_draft(repo: OpsRepository, settings: Settings, tp: TimeProvider, actor: Actor, sku: str, channel: str,
                 price: int, note: str = "") -> int:
    """A person's own price-change proposal (SRS F-08-4.6). It goes through the same PENDING -> APPROVED -> APPLIED path as
    a machine recommendation, with an evidence snapshot frozen at writing time. A price below the margin defense line
    (the price floor, or any negative net margin) needs an ADMIN to approve; the author never approves their own draft."""
    _require_decider(actor)
    row = repo.inventory_latest(sku, channel)
    if row is None:
        raise WorkflowError(f"no inventory row for {sku}/{channel}")
    if price <= 0:
        raise WorkflowError("price must be positive")
    now = _naive_utc(tp)
    cfg = load_decision_config(settings.decision, row["category_l"])
    current, floor = int(row["current_selling_price"]), int(row["min_price_floor"])
    if price == current:
        raise WorkflowError("the proposed price equals the current price")
    cost = {"purchase": row["purchase_cost"], "packaging": row["fixed_cost"], "logistics": row["logistics_cost"], "promo": row["promo_cost"],
            "fee_pct": row["channel_fee_rate"], "disposal": disposal_cost(row["purchase_cost"], row.get("disposal_cost"), cfg.disposal_rate_pct)}
    vat = bool(row["vat_inclusive"])
    sim = simulate_price_match(current, price, Decimal(row["daily_sales_qty"]) * 7, cost, vat, cfg.vat_rate)
    amt, rate = net_margin(current, cost["purchase"], cost["packaging"], cost["logistics"], cost["promo"], cost["fee_pct"], vat, cfg.vat_rate, disposal=cost["disposal"])
    comps = repo.competitor_snapshots(row["std_item_id"], now - timedelta(hours=float(cfg.competitor_max_age_hours)), None, confirmed_only=cfg.require_confirmed_match)
    live = [c for c in comps if not c["is_out_of_stock"]]
    low = min((c["normalized_unit_price"] for c in live), default=None)
    below = price < floor or Decimal(sim["expected_margin_amount"]) < 0
    flags = {"DRAFT": True, "author": actor.name, "BELOW_DEFENSE_LINE": below, "REQUIRES_ADMIN": below,
             "metrics": {"internal_sku": sku, "margin_amount": str(amt), "margin_rate": str(rate), "floor": floor, "match_simulation": sim},
             "evidence": {"note": note[:500], "author": actor.name, "created_at": now.isoformat(sep=" "),
                          "as_of": {"inventory_date": str(row["recorded_date"]), "competitor_scraped_at": max((c["scraped_at"].isoformat(sep=" ") for c in comps), default=None)},
                          "current_price": current, "proposed_price": price, "floor": floor,
                          "competitor": {"listings": len(live), "lowest_effective_pack_price": None if low is None else str((low * row["pack_std_units"]).quantize(Decimal("0.01")))}}}
    rec = Recommendation(int(row["std_item_id"]), channel, sku, current, price, "MANUAL_DRAFT", flags, "PENDING")
    rec_id = repo.insert_recommendation(rec, now)
    repo.audit(actor.name, "DRAFT_CREATED", {"sku": sku, "price": current}, {"rec_id": rec_id, "price": price, "below_defense_line": below}, now)
    return rec_id


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
