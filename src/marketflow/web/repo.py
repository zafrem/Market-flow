"""Read models and user administration for the API."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from ..decision.calc import net_margin, q2
from ..decision.config import load_decision_config
from ..ops_repo import Repo, _dec

Scope = list[tuple[str, str]] | None      # None = unrestricted (ADMIN)


def in_scope(scope: Scope, category_l: str, category_m: str) -> bool:
    return scope is None or any(l == category_l and (m == "" or m == category_m) for l, m in scope)


class AppRepository(Repo):
    # ---- users ---------------------------------------------------------------------------------
    def user_by_email(self, email: str) -> dict[str, Any] | None:
        r = self._rows("SELECT * FROM users WHERE email = :e", e=email.strip().lower())
        return r[0] if r else None

    def user_by_id(self, uid: int) -> dict[str, Any] | None:
        r = self._rows("SELECT * FROM users WHERE user_id = :i", i=uid)
        return r[0] if r else None

    def create_user(self, email: str, name: str, role: str, password_hash: str) -> int:
        with self._e.begin() as c:
            res = c.execute(text("INSERT INTO users (email, name, role, password_hash) VALUES (:e,:n,:r,:h)"),
                            {"e": email.strip().lower(), "n": name, "r": role, "h": password_hash})
            return int(res.lastrowid)

    def list_users(self) -> list[dict[str, Any]]:
        return self._rows("SELECT user_id, email, name, role, active FROM users ORDER BY user_id")

    def set_active(self, uid: int, active: bool) -> None:
        with self._e.begin() as c:
            c.execute(text("UPDATE users SET active = :a WHERE user_id = :i"), {"a": int(active), "i": uid})

    def set_role(self, uid: int, role: str) -> None:
        with self._e.begin() as c:
            c.execute(text("UPDATE users SET role = :r WHERE user_id = :i"), {"r": role, "i": uid})

    def scope_for(self, user: dict[str, Any]) -> Scope:
        if user["role"] == "ADMIN":
            return None
        rows = self._rows("SELECT category_l, category_m FROM user_category_assignments WHERE user_id = :i", i=user["user_id"])
        return [(r["category_l"], r["category_m"]) for r in rows]

    def set_assignments(self, uid: int, pairs: list[tuple[str, str]]) -> None:
        with self._e.begin() as c:
            c.execute(text("DELETE FROM user_category_assignments WHERE user_id = :i"), {"i": uid})
            for l, m in set(pairs):
                c.execute(text("INSERT INTO user_category_assignments (user_id, category_l, category_m) VALUES (:i,:l,:m)"),
                          {"i": uid, "l": l, "m": m})

    # ---- watchlist / preferences ---------------------------------------------------------------------
    def watchlist(self, uid: int) -> list[int]:
        return [r["std_item_id"] for r in self._rows(
            "SELECT std_item_id FROM user_watchlists WHERE user_id = :i ORDER BY sort_order, watch_id", i=uid)]

    def set_watchlist(self, uid: int, item_ids: list[int]) -> None:
        with self._e.begin() as c:
            c.execute(text("DELETE FROM user_watchlists WHERE user_id = :i"), {"i": uid})
            for order, item in enumerate(dict.fromkeys(item_ids)):
                c.execute(text("INSERT INTO user_watchlists (user_id, std_item_id, sort_order) VALUES (:i,:s,:o)"),
                          {"i": uid, "s": item, "o": order})

    def preferences(self, uid: int) -> dict[str, Any]:
        r = self._rows("SELECT alert_margin_bep, ui_layout FROM user_preferences WHERE user_id = :i", i=uid)
        if not r:
            return {"alert_margin_bep": "10.00", "ui_layout": {}}
        layout = r[0]["ui_layout"]
        return {"alert_margin_bep": f"{_dec(r[0]['alert_margin_bep']):.2f}",
                "ui_layout": json.loads(layout) if isinstance(layout, str) else (layout or {})}

    def set_preferences(self, uid: int, margin: Decimal, layout: dict[str, Any]) -> None:
        with self._e.begin() as c:
            c.execute(text("DELETE FROM user_preferences WHERE user_id = :i"), {"i": uid})
            c.execute(text("INSERT INTO user_preferences (user_id, alert_margin_bep, ui_layout) VALUES (:i,:m,:l)"),
                      {"i": uid, "m": str(margin), "l": json.dumps(layout)})

    # ---- read models --------------------------------------------------------------------------------------
    def item_scope_info(self, std_item_id: int) -> dict[str, Any] | None:
        r = self._rows("SELECT std_item_id, item_name, category_l, category_m FROM master_products WHERE std_item_id = :i", i=std_item_id)
        return r[0] if r else None

    def matrix(self, scope: Scope, settings, now: datetime, item_ids: list[int] | None = None) -> list[dict[str, Any]]:
        """Three-way comparison rows: public benchmark | internal cost & price | competitors. Includes cost fields;
        callers mask by role."""
        rows = self._rows(
            "SELECT i.*, m.category_l, m.category_m, m.item_name, m.standard_unit FROM int_operations_inventory i "
            "JOIN master_products m ON m.std_item_id = i.std_item_id WHERE i.recorded_date = "
            "(SELECT MAX(x.recorded_date) FROM int_operations_inventory x WHERE x.internal_sku = i.internal_sku "
            "AND x.channel_name = i.channel_name) ORDER BY m.item_name, i.internal_sku, i.channel_name")
        out = []
        for r in rows:
            if not in_scope(scope, r["category_l"], r["category_m"]) or (item_ids is not None and r["std_item_id"] not in item_ids):
                continue
            cfg = load_decision_config(settings.decision, r["category_l"])
            cost = {k: _dec(r[k]) for k in ("purchase_cost", "fixed_cost", "logistics_cost", "channel_fee_rate", "promo_cost")}
            amt, rate = net_margin(r["current_selling_price"], cost["purchase_cost"], cost["fixed_cost"], cost["logistics_cost"],
                                   cost["promo_cost"], cost["channel_fee_rate"], bool(r["vat_inclusive"]), cfg.vat_rate)
            bench = self._rows("SELECT recorded_date, AVG(normalized_price) AS p FROM ext_public_benchmarks WHERE std_item_id = :i "
                               "GROUP BY recorded_date ORDER BY recorded_date DESC LIMIT 1", i=r["std_item_id"])
            comps = self.competitor_snapshots(r["std_item_id"], now - timedelta(hours=float(cfg.competitor_max_age_hours)))
            comps = [c for c in comps if c["match_confidence"] >= cfg.min_match_confidence]
            live = [c["normalized_unit_price"] for c in comps if not c["is_out_of_stock"]]
            pack = _dec(r["pack_std_units"])
            life = json.loads(r["lifecycle_metric"]) if isinstance(r["lifecycle_metric"], str) else r["lifecycle_metric"]
            out.append({
                "std_item_id": r["std_item_id"], "item_name": r["item_name"], "category_l": r["category_l"],
                "category_m": r["category_m"], "internal_sku": r["internal_sku"], "channel_name": r["channel_name"],
                "recorded_date": str(r["recorded_date"]), "stock_qty": r["stock_qty"], "remaining_days": life.get("remaining_days"),
                "public_benchmark": ({"recorded_date": str(bench[0]["recorded_date"]), "unit": r["standard_unit"],
                                      "normalized_price": str(q2(_dec(bench[0]["p"])))} if bench else None),
                "internal": {"current_selling_price": r["current_selling_price"], "purchase_cost": str(cost["purchase_cost"]),
                             "fixed_cost": str(cost["fixed_cost"]), "logistics_cost": str(cost["logistics_cost"]),
                             "channel_fee_rate": str(cost["channel_fee_rate"]), "promo_cost": str(cost["promo_cost"]),
                             "min_price_floor": r["min_price_floor"], "margin_amount": str(amt), "margin_rate": str(rate)},
                "competitors": {"listings": len(comps), "out_of_stock": sum(c["is_out_of_stock"] for c in comps),
                                "lowest_unit_price": str(q2(min(live))) if live else None,
                                "lowest_pack_price": str(q2(min(live) * pack)) if live else None},
            })
        return out

    def recommendations_view(self, scope: Scope, status: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT p.*, m.item_name, m.category_l, m.category_m FROM price_recommendations p "
               "JOIN master_products m ON m.std_item_id = p.std_item_id" + (" WHERE p.status = :s" if status else "") + " ORDER BY p.rec_id DESC")
        out = []
        for r in self._rows(sql, **({"s": status} if status else {})):
            if not in_scope(scope, r["category_l"], r["category_m"]):
                continue
            r["flags"] = json.loads(r["flags"]) if isinstance(r["flags"], str) else (r["flags"] or {})
            out.append(r)
        return out
