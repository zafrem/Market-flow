"""Human decisions on machine proposals: resolve review-queue items, approve/reject learned selectors.
MD/ADMIN only (SRS RBAC matrix). Every decision is audited."""
from __future__ import annotations

from .decision.service import DECIDERS, Actor, WorkflowError
from .ops_repo import Repo
from .scrape.selfheal import promote_candidate
from .time_provider import TimeProvider


def _need_decider(actor: Actor) -> None:
    if actor.role not in DECIDERS:
        raise PermissionError(f"role {actor.role} may not make curation decisions")


def _now(tp: TimeProvider):
    return tp.now().replace(tzinfo=None)


def resolve_review(repo: Repo, tp: TimeProvider, review_id: int, actor: Actor) -> None:
    _need_decider(actor)
    if not repo.resolve_review(review_id, actor.name, _now(tp)):
        raise WorkflowError(f"review item {review_id} not found or already resolved")
    repo.audit(actor.name, "REVIEW_RESOLVED", {"review_id": review_id}, {"status": "RESOLVED"}, _now(tp))


def _pending(repo: Repo, cand_id: int) -> dict:
    c = next((c for c in repo.list_candidates(None) if c["cand_id"] == cand_id), None)
    if c is None:
        raise WorkflowError(f"selector candidate {cand_id} not found")
    if c["status"] != "PENDING":
        raise WorkflowError(f"selector candidate {cand_id} is already {c['status']}")
    return c


def approve_selector(repo: Repo, tp: TimeProvider, cand_id: int, actor: Actor) -> None:
    _need_decider(actor)
    c = _pending(repo, cand_id)
    try:
        promote_candidate(c["platform"], c["selectors"], actor.name)    # re-validated against the live ruleset
    except (ValueError, TypeError, KeyError) as e:
        raise WorkflowError(f"candidate {cand_id} no longer valid: {e}") from e
    repo.decide_candidate(cand_id, "APPROVED", actor.name, _now(tp))
    repo.audit(actor.name, "SELECTOR_APPROVED", {"platform": c["platform"], "cand_id": cand_id}, c["selectors"], _now(tp))


def reject_selector(repo: Repo, tp: TimeProvider, cand_id: int, actor: Actor) -> None:
    _need_decider(actor)
    c = _pending(repo, cand_id)
    repo.decide_candidate(cand_id, "REJECTED", actor.name, _now(tp))
    repo.audit(actor.name, "SELECTOR_REJECTED", {"platform": c["platform"], "cand_id": cand_id}, None, _now(tp))
