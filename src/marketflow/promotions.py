"""Promotion calendar (SRS F-08 P1): when did we or a channel run a campaign, so a jump in search or sales is read against it.

Informational: promotions do not change prices by themselves. MD/ADMIN record them; each change is audited."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from .decision.service import Actor


def _d(v: Any) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def add_promotion(repo, actor: Actor, now: datetime, name: str, start: date, end: date, category_l: str = "", channel: str = "",
                  discount_pct: float | None = None, note: str = "") -> int:
    if actor.role not in ("MD", "ADMIN"):
        raise PermissionError("only MD or ADMIN can record promotions")
    name = name.strip()
    if not name:
        raise ValueError("a promotion needs a name")
    if end < start:
        raise ValueError("the end date is before the start date")
    if discount_pct is not None and not 0 <= discount_pct <= 100:
        raise ValueError("discount must be between 0 and 100")
    with repo._e.begin() as c:
        c.execute(text("INSERT INTO promotions (name, category_l, channel_name, start_date, end_date, discount_pct, note, created_by) "
                       "VALUES (:n,:c,:ch,:s,:e,:d,:no,:a)"),
                  {"n": name[:120], "c": category_l, "ch": channel, "s": start.isoformat(), "e": end.isoformat(), "d": discount_pct,
                   "no": note[:255], "a": actor.name})
    pid = int(repo._rows("SELECT MAX(promo_id) AS i FROM promotions")[0]["i"])
    repo.audit(actor.name, "PROMOTION_ADDED", None, {"promo_id": pid, "name": name, "start": start.isoformat(), "end": end.isoformat()}, now)
    return pid


def remove_promotion(repo, actor: Actor, now: datetime, promo_id: int) -> None:
    if actor.role not in ("MD", "ADMIN"):
        raise PermissionError("only MD or ADMIN can remove promotions")
    rows = repo._rows("SELECT * FROM promotions WHERE promo_id=:i", i=promo_id)
    if not rows:
        raise KeyError(promo_id)
    with repo._e.begin() as c:
        c.execute(text("DELETE FROM promotions WHERE promo_id=:i"), {"i": promo_id})
    repo.audit(actor.name, "PROMOTION_REMOVED", {"promo_id": promo_id, "name": rows[0]["name"]}, None, now)


def list_promotions(repo, today: date, window_days: int = 90) -> list[dict[str, Any]]:
    """Promotions that end within the last `window_days` or have not ended yet, with a state relative to today."""
    since = (today - timedelta(days=window_days)).isoformat()
    out = []
    for r in repo._rows("SELECT * FROM promotions WHERE end_date >= :s ORDER BY start_date, promo_id", s=since):
        s, e = _d(r["start_date"]), _d(r["end_date"])
        out.append({"promo_id": r["promo_id"], "name": r["name"], "category_l": r["category_l"], "channel_name": r["channel_name"],
                    "start_date": s.isoformat(), "end_date": e.isoformat(), "discount_pct": None if r["discount_pct"] is None else str(r["discount_pct"]),
                    "note": r["note"] or "", "created_by": r["created_by"],
                    "state": "ACTIVE" if s <= today <= e else "UPCOMING" if s > today else "ENDED"})
    return out


def active_on(repo, day: date, category_l: str = "", channel: str = "") -> list[dict[str, Any]]:
    return [p for p in list_promotions(repo, day, 0)
            if p["start_date"] <= day.isoformat() <= p["end_date"] and p["category_l"] in ("", category_l) and p["channel_name"] in ("", channel)]
