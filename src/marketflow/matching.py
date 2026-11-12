"""Human decisions on competitor-listing matches (SRS F-08-2.1 / 2.3). A listing feeds prices and alerts only after a
person has graded it SAME; SIMILAR and SUBSTITUTE are recorded but never used for margin decisions.
MD/ADMIN only; every change is audited."""
from __future__ import annotations

from .decision.service import DECIDERS, Actor, WorkflowError
from .ops_repo import OpsRepository
from .time_provider import TimeProvider

GRADES = ("SAME", "SIMILAR", "SUBSTITUTE")


def _now(tp: TimeProvider):
    return tp.now().replace(tzinfo=None)


def set_match(repo: OpsRepository, tp: TimeProvider, actor: Actor, platform: str, code: str, std_item_id: int, grade: str) -> None:
    if actor.role not in DECIDERS:
        raise PermissionError(f"role {actor.role} may not change product matches")
    if grade not in GRADES:
        raise WorkflowError(f"grade must be one of {', '.join(GRADES)}")
    if repo.item_exists(std_item_id) is None:
        raise WorkflowError(f"item {std_item_id} is not in the catalog")
    if not repo.listing_seen(platform, code):
        raise WorkflowError(f"no scraped listing {platform}/{code}")
    before = repo.get_match(platform, code)
    repo.save_match(platform, code, std_item_id, grade, actor.name, _now(tp))
    repo.audit(actor.name, "MATCH_SET",
               {"item": before["std_item_id"], "grade": before["match_grade"], "confirmed": before["reviewed_at"] is not None} if before else None,
               {"platform": platform, "code": code, "item": std_item_id, "grade": grade}, _now(tp))


def remove_match(repo: OpsRepository, tp: TimeProvider, actor: Actor, platform: str, code: str) -> None:
    if actor.role not in DECIDERS:
        raise PermissionError(f"role {actor.role} may not change product matches")
    before = repo.get_match(platform, code)
    if before is None or not repo.remove_match(platform, code):
        raise WorkflowError(f"no match {platform}/{code}")
    repo.audit(actor.name, "MATCH_REMOVED", {"item": before["std_item_id"], "grade": before["match_grade"]},
               {"platform": platform, "code": code}, _now(tp))
