"""Demo data for trying the dashboard (food: fresh produce, eggs and milk, and processed foods), and the clear-data tool that removes it (or any operational data) again.

Demo data is plausible, not real: a seeded random generator makes 60 days of wholesale prices, 30 days of competitor
snapshots, inventory and cost history, promotions, demand signals, match decisions and pending recommendations.
It is only loaded into an EMPTY store, so demo and real figures are never mixed. Local files (DuckDB/SQLite) only."""
from __future__ import annotations

import csv
import json
import math
import random
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .decision.calc import disposal_cost, price_floor
from .decision.config import load_decision_config
from .decision.service import Actor, run_decisions
from .mi.importer import import_signals
from .mi.store import MiStore
from .ops_repo import Repo
from .promotions import add_promotion
from .repository import SqlRepository
from .settings import Settings
from .time_provider import TimeProvider, today_kst

# Tables holding operational data, children first. Users, roles, category assignments, preferences, the catalog,
# crawler settings, synonyms and the audit trail are settings or records of who did what, so clearing keeps them.
OPERATIONAL_TABLES = ["flyer_items", "flyers", "price_recommendations", "inventory_revisions", "int_operations_inventory",
                      "ext_retail_prices", "ext_public_benchmarks", "product_channel_map", "promotions", "mi_observations",
                      "review_queue", "alert_log", "selector_candidates", "crawler_runs"]
SETTINGS_TABLES = ["users", "master_products", "audit_log", "crawler_settings", "mi_synonyms"]

# name: (category_l, category_m, Korean name, wholesale/100g, our cost per 1 kg pack, our price, competitor price/100g)
CATALOG: dict[str, tuple[str, str, str, int, int, int, int]] = {
    "Apple": ("Fresh", "Fruit", "사과", 520, 5600, 9900, 960),
    "Pear": ("Fresh", "Fruit", "배", 560, 6200, 10900, 1050),
    "Onion": ("Fresh", "Vegetable", "양파", 150, 1900, 3500, 340),
    "Potato": ("Fresh", "Vegetable", "감자", 200, 2500, 4500, 440),
    "Napa Cabbage": ("Fresh", "Vegetable", "배추", 130, 1700, 3200, 310),
    "Radish": ("Fresh", "Vegetable", "무", 110, 1400, 2900, 280),
    "Green Onion": ("Fresh", "Vegetable", "대파", 320, 3900, 6500, 620),
    "Garlic": ("Fresh", "Vegetable", "마늘", 900, 11000, 17900, 1750),
    "Egg": ("Fresh", "Egg", "계란", 430, 4800, 7500, 700),
    "Milk": ("Fresh", "Dairy", "우유", 230, 2600, 3900, 370),
    "Yogurt": ("Processed", "Dairy", "요거트", 400, 4500, 6900, 650),
    "Ramen": ("Processed", "Noodle", "라면", 360, 3600, 4980, 520),
    "Dumpling": ("Processed", "Frozen", "만두", 520, 5200, 8900, 920),
    "Gochujang": ("Processed", "Sauce", "고추장", 680, 6800, 11900, 1240),
}
EXTRA_ITEMS = ["Egg", "Milk", "Yogurt", "Ramen", "Dumpling", "Gochujang"]    # added to the starter catalog (fresh produce); removed again by clear
CHANNELS = {"OWN_MALL": (Decimal("3.5"), 1.0), "SHOPA": (Decimal("11.5"), 1.03)}   # fee %, price factor
PLATFORMS = {"COUPANG": 0.97, "KURLY": 1.12, "SSG": 1.05}
HISTORY_DAYS, SNAPSHOT_DAYS = 60, 30
ACTOR = Actor("demo", "ADMIN")


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: int(c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()) for t in OPERATIONAL_TABLES + ["users", "master_products"]}


def has_operational_data(engine: Engine) -> bool:
    n = counts(engine)
    return any(n[t] for t in OPERATIONAL_TABLES)


def clear_data(engine: Engine) -> dict[str, int]:
    """Delete every row of the operational tables and the catalog items the demo added. Returns rows removed per table."""
    removed: dict[str, int] = {}
    with engine.begin() as c:
        for t in OPERATIONAL_TABLES:
            removed[t] = int(c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar())
            c.execute(text(f"DELETE FROM {t}"))
        for name in EXTRA_ITEMS:
            for q in ("DELETE FROM user_watchlists WHERE std_item_id IN (SELECT std_item_id FROM master_products WHERE item_name=:n)",
                      "DELETE FROM master_product_aliases WHERE std_item_id IN (SELECT std_item_id FROM master_products WHERE item_name=:n)",
                      "DELETE FROM master_products WHERE item_name=:n"):
                c.execute(text(q), {"n": name})
    return removed


def _wave(rng: random.Random, n: int, amp: float, drift_end: float = 0.0) -> list[float]:
    """A smooth random multiplier series of n days around 1.0, ending `drift_end` higher than it would otherwise."""
    out, v = [], 0.0
    for i in range(n):
        v = 0.85 * v + rng.gauss(0, amp)
        tail = drift_end * max(0.0, (i - (n - 8)) / 7)
        out.append(1 + v + tail)
    return out


def load_demo(engine: Engine, settings: Settings, tp: TimeProvider, today: date | None = None) -> dict[str, Any]:
    if engine.dialect.name not in ("sqlite", "duckdb"):
        raise ValueError("demo data can only be loaded into a local file database (DuckDB or SQLite)")
    if has_operational_data(engine):
        raise ValueError("the database already holds data; clear it first so demo and real data are not mixed")
    rng = random.Random(20261113)
    today = today or today_kst(tp)
    now = tp.now().replace(tzinfo=None)
    repo, store, bench = Repo(engine), MiStore(engine), SqlRepository(engine)

    with engine.begin() as c:                                   # catalog: the starter 8 plus three more categories
        for name in EXTRA_ITEMS:
            l, m, ko = CATALOG[name][:3]
            c.execute(text("INSERT INTO master_products (category_l, category_m, item_name, standard_unit) VALUES (:l,:m,:n,'100g')"),
                      {"l": l, "m": m, "n": name})
        for name, (_, _, ko, *_rest) in CATALOG.items():
            for src in ("KAMIS", "GARAK", "RETAIL"):
                c.execute(text("INSERT OR IGNORE INTO master_product_aliases (source_type, alias, std_item_id) "
                               "SELECT :s, :a, std_item_id FROM master_products WHERE item_name=:n"), {"s": src, "a": ko, "n": name})
        ids = {r[1]: r[0] for r in c.execute(text("SELECT std_item_id, item_name FROM master_products"))}

    # ---- wholesale benchmarks: 60 days, KAMIS and Garak; pear and garlic rise over the last week ----------------------
    trend = {n: _wave(rng, HISTORY_DAYS, 0.012, 0.14 if n in ("Pear", "Garlic") else 0.0) for n in CATALOG}
    rows = []
    for k in range(HISTORY_DAYS):
        day = today - timedelta(days=HISTORY_DAYS - 1 - k)
        for name, (l, m, ko, ws, *_x) in CATALOG.items():
            if name in EXTRA_ITEMS:
                continue                                         # public wholesale sources cover produce only
            for src, bias in (("KAMIS", 1.0), ("GARAK", 1.04)):
                p = ws * trend[name][k] * bias * rng.uniform(0.985, 1.015)
                rows.append({"recorded_date": day, "source_type": src, "std_item_id": ids[name], "raw_item_name": ko,
                             "avg_price": Decimal(round(p * 10)), "min_price": Decimal(round(p * 9.2)), "max_price": Decimal(round(p * 10.9)),
                             "normalized_price": Decimal(f"{p:.2f}"), "extra_metrics": {"unit": "kg"}})
    bench.upsert_benchmarks(rows)
    n_bench = len(rows)

    # ---- inventory, cost and sales history: 60 days x 11 items x 2 channels ---------------------------------------------
    promos = [  # (name, start offset, end offset, category, channel, discount %, uplift)
        ("가을 사과·배 특가", -34, -28, "Fresh", "", 20, 0.38),
        ("김장 채소 페스타", -22, -16, "Fresh", "", 10, 0.02),
        ("주말 신선 반값", -11, -8, "Fresh", "OWN_MALL", 30, 0.27),
        ("라면·만두 행사", -15, -12, "Processed", "", 15, 0.15),
    ]
    def uplift(day: date, cat: str, ch: str) -> float:
        for _n, a, b, pc, pch, _d, up in promos:
            if pc == cat and pch in ("", ch) and today + timedelta(days=a) <= day <= today + timedelta(days=b):
                return up
        return 0.0
    base_sales = {"Apple": 38, "Pear": 24, "Onion": 62, "Potato": 55, "Napa Cabbage": 30, "Radish": 26,
                  "Green Onion": 40, "Garlic": 18, "Milk": 90, "Yogurt": 48, "Egg": 70, "Ramen": 120, "Dumpling": 44, "Gochujang": 16}
    for k in range(HISTORY_DAYS):
        day = today - timedelta(days=HISTORY_DAYS - 1 - k)
        batch, weekend = [], 1.25 if day.weekday() >= 5 else 1.0
        for name, (cat, mid, ko, ws, cost, sell, comp) in CATALOG.items():
            cfg = load_decision_config(settings.decision, cat)
            for ch, (fee, pf) in CHANNELS.items():
                purchase = Decimal(round(cost * trend.get(name, [1.0] * HISTORY_DAYS)[k] / 10) * 10)
                fixed, logi = Decimal(300), Decimal(700 if name in ("Milk", "Yogurt", "Dumpling") else 500)
                price = int(round(sell * pf / 100) * 100)
                if name == "Apple" and k >= HISTORY_DAYS - 10:
                    price = int(round(price * 0.95 / 100) * 100)          # we cut the apple price 10 days ago
                sales = max(1, round(base_sales[name] * weekend * (1 + uplift(day, cat, ch)) * rng.uniform(0.88, 1.12) * (0.6 if ch == "SHOPA" else 1.0)))
                cover = {"Onion": 45, "Milk": 4}.get(name, 9)
                stock = int(sales * cover * rng.uniform(0.9, 1.1))
                life = {"remaining_days": 3 if name == "Milk" else (6 if name == "Yogurt" else None),
                        "holding_age_days": 25 if name == "Onion" else None}
                disp = disposal_cost(purchase, None, cfg.disposal_rate_pct)
                floor = price_floor(purchase, fixed, logi, Decimal(0), fee, True, cfg, disposal=disp)
                batch.append({"recorded_date": day, "std_item_id": ids[name], "internal_sku": f"{name.upper().replace(' ', '-')}-1KG",
                              "channel_name": ch, "purchase_cost": purchase, "fixed_cost": fixed, "logistics_cost": logi,
                              "channel_fee_rate": fee, "promo_cost": Decimal(0), "disposal_cost": None, "vat_inclusive": 1,
                              "pack_std_units": Decimal(10), "current_selling_price": price, "stock_qty": stock,
                              "daily_sales_qty": sales, "min_price_floor": floor, "lifecycle_metric": life})
        repo.replace_inventory(batch, datetime.combine(day, datetime.min.time()) + timedelta(hours=1))
    n_inv = HISTORY_DAYS * len(CATALOG) * len(CHANNELS)

    # ---- competitor snapshots: 30 days, 3 platforms; the newest is one hour old -----------------------------------------
    retail, listings = [], {}
    for k in range(SNAPSHOT_DAYS):
        at = now - timedelta(days=SNAPSHOT_DAYS - 1 - k, hours=1)
        for name, (cat, mid, ko, ws, cost, sell, comp) in CATALOG.items():
            for plat, f in PLATFORMS.items():
                code = f"{plat}-{name.upper().replace(' ', '-')}"
                unit = comp * f * trend[name][HISTORY_DAYS - SNAPSHOT_DAYS + k] * rng.uniform(0.985, 1.015)
                if name == "Potato" and plat == "COUPANG":
                    unit *= 0.66                                         # a price war on potatoes
                if name == "Garlic" and plat == "COUPANG":
                    unit *= 0.88
                sale = int(round(unit * 10 / 10) * 10)
                discounted = rng.random() < (0.72 if (name, plat) == ("Garlic", "COUPANG") else 0.08)
                original = int(round(sale / 0.85 / 10) * 10) if discounted else sale
                coupon = 500 if rng.random() < 0.06 else 0
                bonus = Decimal(1) if (plat == "KURLY" and name == "Yogurt" and k >= SNAPSHOT_DAYS - 6) else Decimal(0)
                out = (name == "Pear" and plat == "SSG" and k >= SNAPSHOT_DAYS - 2) or rng.random() < 0.02
                eff = Decimal(sale - coupon) / (Decimal(10) * (1 + bonus))
                retail.append({"scraped_at": at, "platform": plat, "std_item_id": ids[name], "match_confidence": Decimal("0.93"),
                               "channel_item_code": code, "product_title": f"{ko} 1kg", "original_price": original, "sale_price": sale,
                               "normalized_unit_price": Decimal(sale) / 10, "is_out_of_stock": int(out), "badge_info": "특가" if discounted else None,
                               "extraction_source": "SELECTOR", "region_code": "SEOUL", "coupon_discount": coupon, "shipping_fee": 0,
                               "bonus_qty": bonus, "effective_unit_price": eff.quantize(Decimal("0.01"))})
                listings[(plat, code)] = ids[name]
        if k >= SNAPSHOT_DAYS - 5:                                       # a new Kurly product, first seen five days ago
            sale = 7900
            retail.append({"scraped_at": at, "platform": "KURLY", "std_item_id": ids["Yogurt"], "match_confidence": Decimal("0.71"),
                           "channel_item_code": "KURLY-NEW-GREEK", "product_title": "그릭 요거트 프리미엄 1kg", "original_price": sale,
                           "sale_price": sale, "normalized_unit_price": Decimal(sale) / 10, "is_out_of_stock": 0, "badge_info": "신상",
                           "extraction_source": "SELECTOR", "region_code": "SEOUL", "coupon_discount": 0, "shipping_fee": 0,
                           "bonus_qty": Decimal(0), "effective_unit_price": Decimal(sale) / 10})
    n_retail = bench.insert_retail(retail)
    for (plat, code), item in listings.items():                          # a person has confirmed all but one as SAME
        if (plat, code) == ("SSG", "SSG-EGG"):
            continue
        repo.save_match(plat, code, item, "SAME", "demo", now)
    bench.propose_matches([{"platform": "SSG", "channel_item_code": "SSG-EGG", "std_item_id": ids["Egg"], "match_confidence": Decimal("0.62")},
                           {"platform": "KURLY", "channel_item_code": "KURLY-NEW-GREEK", "std_item_id": ids["Yogurt"], "match_confidence": Decimal("0.71")}], now)
    with engine.begin() as c:                                            # these two wait for review (UNCONFIRMED is never used)
        for plat, code in (("SSG", "SSG-EGG"), ("KURLY", "KURLY-NEW-GREEK")):
            c.execute(text("DELETE FROM product_channel_map WHERE platform=:p AND channel_item_code=:c"), {"p": plat, "c": code})
        c.execute(text("INSERT INTO product_channel_map (platform, channel_item_code, std_item_id, pack_size_factor, match_method, match_confidence, match_grade) "
                       "VALUES ('SSG','SSG-EGG',:e,1,'RULE',0.62,'SIMILAR'), ('KURLY','KURLY-NEW-GREEK',:y,1,'RULE',0.71,'SIMILAR')"),
                  {"e": ids["Egg"], "y": ids["Yogurt"]})

    # ---- promotion calendar (past results and one coming up) ------------------------------------------------------------
    upcoming = ("겨울 대파·마늘 기획전", 3, 9, "Fresh", "", 15, 0.0)
    for name, a, b, cat, ch, pct, _u in promos + [upcoming]:
        add_promotion(repo, ACTOR, now, name, today + timedelta(days=a), today + timedelta(days=b), cat, ch, pct, "demo")

    # ---- demand signals through the real importer -------------------------------------------------------------------------
    n_mi = _load_signals(store, rng, today)

    # ---- decisions for today: some become PENDING, some SUPPRESSED -----------------------------------------------------
    made = run_decisions(repo, settings, tp, today)
    repo.audit(ACTOR.name, "DEMO_LOADED", None, {"inventory_rows": n_inv, "benchmarks": n_bench, "retail": n_retail}, now)
    return {"benchmarks": n_bench, "inventory_rows": n_inv, "competitor_prices": n_retail, "signals": n_mi,
            "promotions": len(promos) + 1, "recommendations": len(made)}


def _load_signals(store: MiStore, rng: random.Random, today: date) -> int:
    monday = today - timedelta(days=today.weekday())
    weeks = [monday - timedelta(weeks=w) for w in range(19, -1, -1)]
    kws = [("사과", "Fresh", 900, 0.004), ("배", "Fresh", 600, 0.003), ("마늘", "Fresh", 400, 0.03), ("대파", "Fresh", 500, 0.02),
           ("양파", "Fresh", 700, 0.0), ("우유", "Fresh", 1500, 0.005), ("계란", "Fresh", 1200, -0.01),
           ("요거트", "Processed", 800, 0.035), ("그릭요거트", "Processed", 200, 0.09), ("라면", "Processed", 2000, 0.002),
           ("만두", "Processed", 900, 0.012), ("고추장", "Processed", 350, -0.005), ("밀키트", "Processed", 300, 0.05),
           ("곤약면", "Processed", 120, 0.11), ("냉동김밥", "Processed", 260, 0.07)]
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        def write(name: str, header: list[str], rows: list[list[Any]]) -> Path:
            p = Path(tmp) / name
            with p.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(header); w.writerows(rows)
            return p
        search = [[str(w), kw, cat, int(base * math.exp(g * i) * rng.uniform(0.9, 1.1)), 0, 0, 0]
                  for kw, cat, base, g in kws for i, w in enumerate(weeks)]
        for r in search:
            r[4] = int(r[3] * 0.08); r[5] = r[4] * 5200; r[6] = r[4] * 2
        total += import_signals(write("s.csv", ["week", "keyword", "category", "searches", "buyers", "gmv", "unitsold"], search), store, "SEARCH", "demo").loaded
        months = [date(today.year, today.month, 1)]
        for _ in range(7):
            months.append((months[-1] - timedelta(days=1)).replace(day=1))
        buyer = [[str(mth), cat, int(g * (1 + 0.03 * i) * rng.uniform(0.95, 1.05)), int(u * (1 + 0.03 * i))]
                 for cat, g, u in (("Fresh", 120_000_000, 15000), ("Processed", 90_000_000, 12000))
                 for i, mth in enumerate(reversed(months))]
        total += import_signals(write("b.csv", ["month", "category", "gmv", "unitsold"], buyer), store, "BUYER", "demo").loaded
        promo = [[str(w), kw, cat, rng.randint(0, 3) + (3 if kw in ("마늘", "요거트", "라면") and i > 15 else 0)]
                 for kw, cat, *_ in kws for i, w in enumerate(weeks)]
        total += import_signals(write("p.csv", ["week", "keyword", "category", "count"], promo), store, "PROMOTION", "demo").loaded
    return total
