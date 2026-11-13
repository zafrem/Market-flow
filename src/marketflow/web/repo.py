"""Read models and user administration for the API."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from ..db import insert_id
from ..decision.calc import disposal_cost, net_margin, q2, round_price, simulate_price_match
from ..decision.config import load_decision_config
from ..ops_repo import Repo, _dec, _dt, newest_per_listing

TREND_DAYS = 14
def _as_date(v: Any) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


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
            return insert_id(c, "INSERT INTO users (email, name, role, password_hash) VALUES (:e,:n,:r,:h)",
                             {"e": email.strip().lower(), "n": name, "r": role, "h": password_hash}, "user_id")

    def list_users(self) -> list[dict[str, Any]]:
        return self._rows("SELECT user_id, email, name, role, active FROM users ORDER BY user_id")

    def set_active(self, uid: int, active: bool) -> None:
        with self._e.begin() as c:
            c.execute(text("UPDATE users SET active = :a WHERE user_id = :i"), {"a": int(active), "i": uid})

    def set_role(self, uid: int, role: str) -> None:
        with self._e.begin() as c:
            c.execute(text("UPDATE users SET role = :r WHERE user_id = :i"), {"r": role, "i": uid})

    def update_user(self, uid: int, role: str | None, active: bool | None) -> bool:
        """One transaction. Refuses (returns False) if it would leave no active ADMIN."""
        with self._e.begin() as c:
            row = c.execute(text("SELECT role, active FROM users WHERE user_id=:i FOR UPDATE" if self._e.dialect.name not in ("sqlite", "duckdb")
                                 else "SELECT role, active FROM users WHERE user_id=:i"), {"i": uid}).mappings().first()
            if row is None:
                return False
            new_role, new_active = role or row["role"], int(row["active"] if active is None else active)
            if row["role"] == "ADMIN" and row["active"] and (new_role != "ADMIN" or not new_active):
                others = c.execute(text("SELECT COUNT(*) FROM users WHERE role='ADMIN' AND active=1 AND user_id<>:i"), {"i": uid}).scalar()
                if not others:
                    return False
            c.execute(text("UPDATE users SET role=:r, active=:a WHERE user_id=:i"), {"r": new_role, "a": new_active, "i": uid})
            return True

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
        rows = [r for r in rows if in_scope(scope, r["category_l"], r["category_m"]) and (item_ids is None or r["std_item_id"] in item_ids)]
        history = self._history(rows, now, settings)                                 # one query per table, however many items
        out = []
        for r in rows:
            cfg = load_decision_config(settings.decision, r["category_l"])
            cost = {k: _dec(r[k]) for k in ("purchase_cost", "fixed_cost", "logistics_cost", "channel_fee_rate", "promo_cost")}
            disposal = disposal_cost(cost["purchase_cost"], None if r.get("disposal_cost") is None else _dec(r["disposal_cost"]),
                                     cfg.disposal_rate_pct)
            amt, rate = net_margin(r["current_selling_price"], cost["purchase_cost"], cost["fixed_cost"], cost["logistics_cost"],
                                   cost["promo_cost"], cost["channel_fee_rate"], bool(r["vat_inclusive"]), cfg.vat_rate, disposal=disposal)
            bench = history["latest_bench"].get(int(r["std_item_id"]))
            since = now - timedelta(hours=float(cfg.competitor_max_age_hours))
            comps = newest_per_listing([c for c in history["listings"].get(int(r["std_item_id"]), []) if c["scraped_at"] >= since])
            comps = [c for c in comps if c["match_confidence"] >= cfg.min_match_confidence]
            seen = len(comps)
            if cfg.require_confirmed_match:                     # only reviewed SAME matches count (F-08-2.3)
                comps = [c for c in comps if (c["platform"], c["channel_item_code"], int(r["std_item_id"])) in history["confirmed"]]
            live = [c["normalized_unit_price"] for c in comps if not c["is_out_of_stock"]]
            pack = _dec(r["pack_std_units"])
            simulation = None
            if live:                                            # matching the lowest competitor effective price: margin and BEP
                target = max(1, round_price(min(live) * pack, cfg.round_to))
                simulation = simulate_price_match(
                    int(r["current_selling_price"]),
                    target, Decimal(r["daily_sales_qty"]) * 7,
                    {"purchase": cost["purchase_cost"], "packaging": cost["fixed_cost"], "logistics": cost["logistics_cost"],
                     "promo": cost["promo_cost"], "fee_pct": cost["channel_fee_rate"], "disposal": disposal},
                    bool(r["vat_inclusive"]), cfg.vat_rate)
                simulation["below_floor"] = target < int(r["min_price_floor"])
            life = json.loads(r["lifecycle_metric"]) if isinstance(r["lifecycle_metric"], str) else r["lifecycle_metric"]
            out.append({
                "std_item_id": r["std_item_id"], "item_name": r["item_name"], "category_l": r["category_l"],
                "category_m": r["category_m"], "internal_sku": r["internal_sku"], "channel_name": r["channel_name"],
                "recorded_date": str(r["recorded_date"]), "stock_qty": r["stock_qty"], "remaining_days": life.get("remaining_days"),
                "public_benchmark": ({"recorded_date": str(bench[0]), "unit": r["standard_unit"],
                                      "normalized_price": str(q2(bench[1]))} if bench else None),
                "internal": {"current_selling_price": r["current_selling_price"], "purchase_cost": str(cost["purchase_cost"]),
                             "fixed_cost": str(cost["fixed_cost"]), "logistics_cost": str(cost["logistics_cost"]),
                             "channel_fee_rate": str(cost["channel_fee_rate"]), "promo_cost": str(cost["promo_cost"]),
                             "disposal_cost": str(q2(disposal)), "match_simulation": simulation,
                             "min_price_floor": r["min_price_floor"], "margin_amount": str(amt), "margin_rate": str(rate),
                             "margin_trend": self._margin_trend(r, cfg, history)},
                "trend": self._trend(r, cfg, pack, history),
                "competitors": {"listings": len(comps), "unconfirmed": seen - len(comps) if cfg.require_confirmed_match else 0,
                                "out_of_stock": sum(c["is_out_of_stock"] for c in comps),
                                "lowest_unit_price": str(q2(min(live))) if live else None,
                                "lowest_pack_price": str(q2(min(live) * pack)) if live else None},
            })
        return out

    @staticmethod
    def _last_day(r: dict[str, Any]) -> date:
        return r["recorded_date"] if isinstance(r["recorded_date"], date) else date.fromisoformat(str(r["recorded_date"])[:10])

    def _history(self, rows: list[dict[str, Any]], now: datetime, settings=None) -> dict[str, Any]:
        """Everything the trend charts need for these matrix rows, fetched once (ids are ints, so inlining them is safe)."""
        if not rows:
            return {"bench": {}, "inv": {}, "retail": {}, "latest_bench": {}, "listings": {}, "confirmed": set()}
        ids = ",".join(str(i) for i in sorted({int(r["std_item_id"]) for r in rows}))
        first = min(self._last_day(r) for r in rows) - timedelta(days=TREND_DAYS - 1)
        last = max(self._last_day(r) for r in rows)
        bench: dict[int, list[tuple[date, Decimal]]] = {}
        for x in self._rows(f"SELECT std_item_id, recorded_date, AVG(normalized_price) AS v FROM ext_public_benchmarks WHERE std_item_id IN ({ids}) "
                            "AND recorded_date >= :s AND recorded_date <= :e GROUP BY std_item_id, recorded_date ORDER BY recorded_date", s=first, e=last):
            bench.setdefault(int(x["std_item_id"]), []).append((_as_date(x["recorded_date"]), _dec(x["v"])))
        inv: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for x in self._rows(f"SELECT recorded_date, internal_sku, channel_name, current_selling_price, purchase_cost, fixed_cost, logistics_cost, "
                            f"channel_fee_rate, promo_cost, disposal_cost, vat_inclusive FROM int_operations_inventory WHERE std_item_id IN ({ids}) "
                            "AND recorded_date >= :s AND recorded_date <= :e ORDER BY recorded_date", s=first, e=last):
            x["recorded_date"] = _as_date(x["recorded_date"])
            inv.setdefault((x["internal_sku"], x["channel_name"]), []).append(x)
        retail: dict[int, list[dict[str, Any]]] = {}
        for x in self._rows(f"SELECT std_item_id, platform, channel_item_code, scraped_at, COALESCE(effective_unit_price, normalized_unit_price) "
                            f"AS normalized_unit_price, match_confidence FROM ext_retail_prices WHERE std_item_id IN ({ids}) "
                            "AND is_out_of_stock = 0 AND scraped_at >= :s AND scraped_at < :e",
                            s=datetime.combine(first, time.min), e=datetime.combine(last + timedelta(days=1), time.min)):
            retail.setdefault(int(x["std_item_id"]), []).append(x)
        latest_bench = {int(x["std_item_id"]): (x["recorded_date"], _dec(x["v"])) for x in self._rows(
            f"SELECT b.std_item_id, b.recorded_date, AVG(b.normalized_price) AS v FROM ext_public_benchmarks b WHERE b.std_item_id IN ({ids}) "
            "AND b.recorded_date = (SELECT MAX(m.recorded_date) FROM ext_public_benchmarks m WHERE m.std_item_id = b.std_item_id) "
            "GROUP BY b.std_item_id, b.recorded_date")}
        oldest = now - timedelta(hours=max(float(load_decision_config(settings.decision, c).competitor_max_age_hours)
                                           for c in {r["category_l"] for r in rows}))
        listings: dict[int, list[dict[str, Any]]] = {}
        for x in self._rows(f"SELECT std_item_id, platform, channel_item_code, COALESCE(effective_unit_price, normalized_unit_price) "
                            f"AS normalized_unit_price, is_out_of_stock, match_confidence, "
                            f"scraped_at FROM ext_retail_prices WHERE std_item_id IN ({ids}) AND scraped_at >= :since", since=oldest):
            x["scraped_at"] = _dt(x["scraped_at"])
            listings.setdefault(int(x["std_item_id"]), []).append(x)
        return {"bench": bench, "inv": inv, "retail": retail, "latest_bench": latest_bench, "listings": listings,
                "confirmed": self.confirmed_listings([int(i) for i in ids.split(",")])}

    def _trend(self, r: dict[str, Any], cfg, pack: Decimal, h: dict[str, Any]) -> dict[str, list[list[str]]]:
        """Daily series for the last TREND_DAYS days ending at the item's latest inventory date; days without data are
        simply absent. Wholesale and competitor values are per standard unit / per pack as in the matrix columns.
        Contains no cost data, so it is safe for every role."""
        last = self._last_day(r)
        since = last - timedelta(days=TREND_DAYS - 1)
        wholesale = [[str(d), str(q2(v))] for d, v in h["bench"].get(int(r["std_item_id"]), []) if since <= d <= last]
        ours = [[str(x["recorded_date"]), str(int(x["current_selling_price"]))]
                for x in h["inv"].get((r["internal_sku"], r["channel_name"]), []) if since <= x["recorded_date"] <= last]
        per_day: dict[str, Decimal] = {}
        for c in h["retail"].get(int(r["std_item_id"]), []):
            at = _dt(c["scraped_at"])
            if not (since <= at.date() <= last) or _dec(c["match_confidence"]) < cfg.min_match_confidence:
                continue
            if cfg.require_confirmed_match and (c["platform"], c["channel_item_code"], int(r["std_item_id"])) not in h["confirmed"]:
                continue
            day, price = str(at.date()), _dec(c["normalized_unit_price"]) * pack
            per_day[day] = min(price, per_day.get(day, price))
        return {"wholesale": wholesale, "ours": ours, "competitor": [[d, str(q2(v))] for d, v in sorted(per_day.items())]}

    def _margin_trend(self, r: dict[str, Any], cfg, h: dict[str, Any]) -> list[list[str]]:
        """Net margin % per inventory day (that day's price and costs). COST DATA: it is listed in COST_KEYS so BUYERs never get it."""
        last = self._last_day(r)
        since = last - timedelta(days=TREND_DAYS - 1)
        out = []
        for x in h["inv"].get((r["internal_sku"], r["channel_name"]), []):
            if not since <= x["recorded_date"] <= last:
                continue
            _, rate = net_margin(x["current_selling_price"], _dec(x["purchase_cost"]), _dec(x["fixed_cost"]), _dec(x["logistics_cost"]),
                                 _dec(x["promo_cost"]), _dec(x["channel_fee_rate"]), bool(x["vat_inclusive"]), cfg.vat_rate,
                                 disposal=disposal_cost(_dec(x["purchase_cost"]), None if x.get("disposal_cost") is None else _dec(x["disposal_cost"]),
                                                        cfg.disposal_rate_pct))
            out.append([str(x["recorded_date"]), str(rate)])
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
