"""Flyer review (SRS F-08-1.6): items read from an image wait for a person; only approved items reach the analysis.

The reviewer sees the image beside the items the reader found, fixes a name or a price, and approves or rejects each one.
Approved items become weekly PROMOTION mentions (one per product per flyer). Deciding again later recomputes the week."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from .mi.classify import Classifier
from .mi.privacy import contains_pii
from .mi.store import MiStore
from .vision import FlyerItem

LOW_CONFIDENCE = 0.7


def add_flyer(store: MiStore, source_name: str, image_path: str, week: date, items: list[FlyerItem], now: datetime) -> int:
    classifier = Classifier(*store.classifier_knowledge())
    with store._e.begin() as c:
        c.execute(text("INSERT INTO flyers (source_name, image_path, week_start, status, created_at) VALUES (:s,:p,:w,'PENDING',:t)"),
                  store._b({"s": source_name[:120], "p": image_path, "w": week.isoformat(), "t": now}))
    fid = int(store._rows("SELECT MAX(flyer_id) AS i FROM flyers")[0]["i"])
    if items:
        with store._e.begin() as c:
            c.execute(text("INSERT INTO flyer_items (flyer_id, name, price, confidence, category_l, status) VALUES (:f,:n,:p,:c,:cat,'PENDING')"),
                      [{"f": fid, "n": i.name, "p": i.price, "c": i.confidence, "cat": classifier.category_for(i.name) or ""} for i in items])
    return fid


def list_flyers(store: MiStore, status: str | None = None) -> list[dict[str, Any]]:
    rows = store._rows("SELECT f.flyer_id, f.source_name, f.week_start, f.status, f.created_at, "
                       "(SELECT COUNT(*) FROM flyer_items i WHERE i.flyer_id = f.flyer_id) AS items, "
                       "(SELECT COUNT(*) FROM flyer_items i WHERE i.flyer_id = f.flyer_id AND i.status = 'PENDING') AS pending "
                       "FROM flyers f WHERE (:s IS NULL OR f.status = :s) ORDER BY f.flyer_id DESC", s=status)
    return [{**r, "week_start": str(r["week_start"])[:10], "created_at": str(r["created_at"]), "items": int(r["items"]), "pending": int(r["pending"])} for r in rows]


def get_flyer(store: MiStore, flyer_id: int) -> dict[str, Any] | None:
    rows = store._rows("SELECT * FROM flyers WHERE flyer_id = :i", i=flyer_id)
    if not rows:
        return None
    items = [{"item_id": i["item_id"], "name": i["name"], "price": i["price"], "confidence": None if i["confidence"] is None else float(i["confidence"]),
              "low_confidence": i["confidence"] is None or float(i["confidence"]) < LOW_CONFIDENCE, "category_l": i["category_l"], "status": i["status"]}
             for i in store._rows("SELECT * FROM flyer_items WHERE flyer_id = :i ORDER BY item_id", i=flyer_id)]
    f = rows[0]
    return {"flyer_id": f["flyer_id"], "source_name": f["source_name"], "week_start": str(f["week_start"])[:10], "status": f["status"], "items": items}


def image_path(store: MiStore, flyer_id: int) -> str | None:
    rows = store._rows("SELECT image_path FROM flyers WHERE flyer_id = :i", i=flyer_id)
    return rows[0]["image_path"] if rows else None


def review_flyer(store: MiStore, actor: str, now: datetime, flyer_id: int, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """decisions: [{item_id, action: approve|reject, name?, price?}]. Unknown items are refused; a changed name is checked for personal data."""
    flyer = get_flyer(store, flyer_id)
    if flyer is None:
        raise KeyError(flyer_id)
    known = {i["item_id"]: i for i in flyer["items"]}
    for d in decisions:
        if d.get("item_id") not in known:
            raise ValueError(f"item {d.get('item_id')} is not part of this flyer")
        if d.get("action") not in ("approve", "reject"):
            raise ValueError("action must be approve or reject")
        name = d.get("name")
        if name is not None and (len(" ".join(str(name).split())) < 2 or contains_pii(str(name))):
            raise ValueError("the name is too short or contains personal data")
        price = d.get("price")
        if price is not None and (not isinstance(price, int) or isinstance(price, bool) or not 0 < price < 10_000_000):
            raise ValueError("price must be a positive whole number")
    with store._e.begin() as c:
        for d in decisions:
            sets, p = ["status = :s"], {"s": "APPROVED" if d["action"] == "approve" else "REJECTED", "i": d["item_id"]}
            if d.get("name") is not None:
                sets.append("name = :n"); p["n"] = " ".join(str(d["name"]).split())[:80]
            if "price" in d:
                sets.append("price = :p"); p["p"] = d["price"]
            c.execute(text(f"UPDATE flyer_items SET {', '.join(sets)} WHERE item_id = :i"), p)
        left = c.execute(text("SELECT COUNT(*) FROM flyer_items WHERE flyer_id = :f AND status = 'PENDING'"), {"f": flyer_id}).scalar()
        c.execute(text("UPDATE flyers SET status = :s, reviewed_by = :a, reviewed_at = :t WHERE flyer_id = :f"),
                  store._b({"s": "PENDING" if left else "REVIEWED", "a": actor, "t": now, "f": flyer_id}))
    sync_week(store, date.fromisoformat(flyer["week_start"]))
    store.audit(actor, "FLYER_REVIEWED", {"flyer_id": flyer_id}, {"decided": len(decisions), "approved": sum(d["action"] == "approve" for d in decisions)}, now)
    return get_flyer(store, flyer_id)


def sync_week(store: MiStore, week: date) -> int:
    """One PROMOTION mention per approved product per flyer, for that week. Recomputed from scratch so a changed decision is reflected."""
    rows = store._rows("SELECT i.name, i.category_l FROM flyer_items i JOIN flyers f USING (flyer_id) WHERE f.week_start = :w AND i.status = 'APPROVED'", w=week.isoformat())
    counts = Counter((r["category_l"], r["name"]) for r in rows)
    with store._e.begin() as c:
        c.execute(text("DELETE FROM mi_observations WHERE signal_type = 'PROMOTION' AND source = 'flyer:image' AND period_start = :w"), {"w": week.isoformat()})
    return store.upsert_observations([{"signal_type": "PROMOTION", "period_start": week, "category_l": cat, "keyword": name, "metric": "count", "value": n, "source": "flyer:image"}
                                      for (cat, name), n in counts.items()])
