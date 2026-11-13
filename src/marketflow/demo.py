"""Demo data for trying the dashboard (food: fresh produce, eggs and milk, and processed foods), and the clear-data tool that removes it (or any operational data) again.

Demo data is plausible, not real: a seeded random generator makes 60 days of wholesale prices, 30 days of competitor
snapshots, inventory and cost history, promotions, demand signals, match decisions and pending recommendations.
It is only loaded into an EMPTY store, so demo and real figures are never mixed. Local files (DuckDB/SQLite) only."""
from __future__ import annotations

import csv
import json
import math
import random
import struct
import tempfile
import zlib
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
from .crawlers.service import resolve_inbox
from .flyers import add_flyer, get_flyer, review_flyer
from .ops_repo import Repo, ReviewItem
from .vision import FlyerItem
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
    "Strawberry": ("Fresh", "Fruit", "딸기", 1300, 14500, 21900, 2350),
    "Tomato": ("Fresh", "Vegetable", "토마토", 480, 5400, 8900, 960),
    "Sweet Potato": ("Fresh", "Vegetable", "고구마", 300, 3300, 5900, 600),
    "Kimchi": ("Processed", "Side Dish", "김치", 420, 4600, 7900, 830),
    "Instant Rice": ("Processed", "Rice", "즉석밥", 380, 4000, 6500, 680),
}
EXTRA_ITEMS = ["Egg", "Milk", "Yogurt", "Ramen", "Dumpling", "Gochujang", "Strawberry", "Tomato", "Sweet Potato", "Kimchi", "Instant Rice"]    # added to the starter catalog (fresh produce); removed again by clear
CHANNELS = {"OWN_MALL": (Decimal("3.5"), 1.0), "SHOPA": (Decimal("11.5"), 1.03)}   # fee %, price factor
PLATFORMS = {"COUPANG": 0.97, "KURLY": 1.12, "SSG": 1.05}
HISTORY_DAYS, SNAPSHOT_DAYS = 60, 30
ACTOR = Actor("demo", "ADMIN")
DEMO_IMAGE_PREFIX = "demo-flyer-"


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: int(c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()) for t in OPERATIONAL_TABLES + ["users", "master_products"]}


def has_operational_data(engine: Engine) -> bool:
    n = counts(engine)
    return any(n[t] for t in OPERATIONAL_TABLES)


def clear_data(engine: Engine, inbox: Path | str | None = None) -> dict[str, int]:
    """Delete every row of the operational tables and the catalog items the demo added (and the placeholder flyer images the
    demo wrote). Returns rows removed per table."""
    removed: dict[str, int] = {}
    if inbox is not None:
        for f in (resolve_inbox(str(inbox)) / "flyer" / "review").glob(DEMO_IMAGE_PREFIX + "*.png"):
            if f.is_file() and not f.is_symlink():
                f.unlink()
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
    trend = {n: _wave(rng, HISTORY_DAYS, 0.012, 0.14 if n in ("Pear", "Garlic") else 0.18 if n == "Strawberry" else -0.08 if n == "Tomato" else 0.0) for n in CATALOG}
    rows = []
    for k in range(HISTORY_DAYS):
        day = today - timedelta(days=HISTORY_DAYS - 1 - k)
        for name, (l, m, ko, ws, *_x) in CATALOG.items():
            if name in EXTRA_ITEMS and name not in ("Egg", "Strawberry", "Tomato", "Sweet Potato"):
                continue                                         # public wholesale sources cover produce and eggs only
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
                  "Green Onion": 40, "Garlic": 18, "Milk": 90, "Yogurt": 48, "Egg": 70, "Ramen": 120, "Dumpling": 44, "Gochujang": 16,
                  "Strawberry": 20, "Tomato": 34, "Sweet Potato": 36, "Kimchi": 58, "Instant Rice": 85}
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
                if name in ("Ramen", "Milk") and k >= HISTORY_DAYS - 3:
                    stock = 0                                            # sold out for the last days: the search-to-purchase gap shows why
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
                if name == "Dumpling" and plat == "COUPANG":
                    unit *= 0.85
                sale = int(round(unit * 10 / 10) * 10)
                discounted = rng.random() < (0.72 if (name, plat) == ("Garlic", "COUPANG") else 0.08)
                original = int(round(sale / 0.85 / 10) * 10) if discounted else sale
                coupon = 500 if rng.random() < 0.06 else 0
                bonus = Decimal(1) if (plat == "KURLY" and name == "Yogurt" and k >= SNAPSHOT_DAYS - 6) else Decimal(0)
                out = (name == "Pear" and plat == "SSG" and k >= SNAPSHOT_DAYS - 2) or rng.random() < 0.02 or (name == "Strawberry" and plat == "COUPANG" and k >= SNAPSHOT_DAYS - 3)
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
    for plat, code, title, item, sale, first in NEW_LISTINGS:               # more new competitor products, each first seen `first` days ago
        for k in range(SNAPSHOT_DAYS - first, SNAPSHOT_DAYS):
            retail.append({"scraped_at": now - timedelta(days=SNAPSHOT_DAYS - 1 - k, hours=1), "platform": plat, "std_item_id": ids[item],
                           "match_confidence": Decimal("0.7"), "channel_item_code": code, "product_title": title, "original_price": sale,
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

    # ---- the other screens: flyers, review queue, selector candidates, crawler history, alerts ------------------------------
    extras = _load_workflow_rows(engine, repo, store, settings, now, today, ids)

    # ---- decisions for today: some become PENDING, some SUPPRESSED -----------------------------------------------------
    made = run_decisions(repo, settings, tp, today)
    repo.audit(ACTOR.name, "DEMO_LOADED", None, {"inventory_rows": n_inv, "benchmarks": n_bench, "retail": n_retail}, now)
    return {"benchmarks": n_bench, "inventory_rows": n_inv, "competitor_prices": n_retail, "signals": n_mi,
            "promotions": len(promos) + 1, "recommendations": len(made), **extras}


# keyword, category, searches 20 weeks ago, weekly growth over the last 20 weeks, buyers per search
KEYWORDS = [("사과", "Fresh", 900, 0.004, 0.09), ("배", "Fresh", 600, 0.003, 0.08), ("마늘", "Fresh", 400, 0.03, 0.06),
            ("대파", "Fresh", 500, 0.02, 0.07), ("양파", "Fresh", 700, 0.0, 0.10), ("우유", "Fresh", 1500, 0.005, 0.025),
            ("계란", "Fresh", 1200, -0.01, 0.11), ("샤인머스캣", "Fresh", 350, 0.06, 0.05),
            ("요거트", "Processed", 800, 0.035, 0.11), ("그릭요거트", "Processed", 200, 0.09, 0.06), ("라면", "Processed", 1700, 0.002, 0.02),
            ("만두", "Processed", 1100, 0.012, 0.025), ("고추장", "Processed", 350, -0.005, 0.09), ("밀키트", "Processed", 1400, 0.02, 0.015),
            ("곤약면", "Processed", 120, 0.11, 0.07), ("냉동김밥", "Processed", 260, 0.07, 0.08), ("비건라면", "Processed", 40, 0.14, 0.05),
            ("두부", "Processed", 500, -0.02, 0.10), ("컵밥", "Processed", 700, -0.03, 0.08),
            ("딸기", "Fresh", 650, 0.05, 0.07), ("토마토", "Fresh", 520, -0.012, 0.08), ("고구마", "Fresh", 480, 0.02, 0.09),
            ("김치", "Processed", 900, 0.004, 0.06), ("즉석밥", "Processed", 1300, 0.006, 0.03),
            ("단백질바", "Processed", 90, 0.13, 0.06), ("오트밀", "Processed", 150, 0.09, 0.07), ("두유", "Processed", 380, 0.05, 0.09)]
NEWS = {"그릭요거트": 0.5, "곤약면": 0.4, "비건라면": 0.3, "샤인머스캣": 0.35, "밀키트": 0.25, "마늘": 0.2, "단백질바": 0.3, "딸기": 0.15}      # media mentions per week, scaled by recency
NEW_LISTINGS = [("COUPANG", "COUPANG-NEW-KIMCHI", "비건 김치 1kg", "Kimchi", 9800, 4), ("SSG", "SSG-NEW-RICE", "저당 즉석밥 6입", "Instant Rice", 7900, 6),
                ("KURLY", "KURLY-NEW-STRAW", "설향 딸기 프리미엄 500g", "Strawberry", 15900, 3), ("COUPANG", "COUPANG-NEW-TOMATO", "대추방울토마토 1kg", "Tomato", 9900, 9)]
NEW_PRODUCTS = {"그릭요거트": 2, "곤약면": 3, "냉동김밥": 2, "밀키트": 1, "단백질바": 2, "오트밀": 1, "두유": 1}


def _load_signals(store: MiStore, rng: random.Random, today: date) -> int:
    monday = today - timedelta(days=today.weekday())
    weeks = [monday - timedelta(weeks=w) for w in range(59, -1, -1)]          # 60 weeks: enough for year-over-year
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        def write(name: str, header: list[str], rows: list[list[Any]]) -> Path:
            p = Path(tmp) / name
            with p.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(header); w.writerows(rows)
            return p
        search = []
        for kw, cat, base, g, conv in KEYWORDS:
            for i, w in enumerate(weeks):
                season = 1 + 0.08 * math.sin(2 * math.pi * i / 52)
                searches = int(base * math.exp(g * max(0, i - 40)) * season * rng.uniform(0.92, 1.08))
                buyers = int(searches * conv * rng.uniform(0.9, 1.1))
                search.append([str(w), kw, cat, searches, buyers, buyers * 5200, buyers * 2])
        total += import_signals(write("s.csv", ["week", "keyword", "category", "searches", "buyers", "gmv", "unitsold"], search), store, "SEARCH", "demo").loaded
        months = [date(today.year, today.month, 1)]
        for _ in range(11):
            months.append((months[-1] - timedelta(days=1)).replace(day=1))
        buyer = [[str(mth), cat, int(g * (1 + 0.025 * i) * rng.uniform(0.95, 1.05)), int(u * (1 + 0.025 * i))]
                 for cat, g, u in (("Fresh", 120_000_000, 15000), ("Processed", 90_000_000, 12000))
                 for i, mth in enumerate(reversed(months))]
        total += import_signals(write("b.csv", ["month", "category", "gmv", "unitsold"], buyer), store, "BUYER", "demo").loaded
        promo = [[str(w), kw, cat, rng.randint(0, 3) + (3 if kw in ("마늘", "요거트", "라면") and i > 55 else 0)]
                 for kw, cat, *_ in KEYWORDS for i, w in enumerate(weeks)]
        total += import_signals(write("p.csv", ["week", "keyword", "category", "count"], promo), store, "PROMOTION", "demo").loaded
        media = [[str(w), kw, cat, int(max(0, rate * max(0, i - 44) * rng.uniform(0.7, 1.3)))] for kw, rate in NEWS.items()
                 for cat in [next(c for k, c, *_ in KEYWORDS if k == kw)] for i, w in enumerate(weeks)]
        total += import_signals(write("m.csv", ["week", "keyword", "category", "count"], media), store, "MEDIA", "demo").loaded
        prod = [[str(w), kw, next(c for k, c, *_ in KEYWORDS if k == kw), n if i > 54 else 0] for kw, n in NEW_PRODUCTS.items() for i, w in enumerate(weeks)]
        total += import_signals(write("n.csv", ["week", "keyword", "category", "count"], prod), store, "PRODUCT", "demo").loaded
    return total


def _png(width: int, height: int, paint) -> bytes:
    """A small PNG made without any imaging library; paint(x, y) returns an (r, g, b) tuple."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(paint(x, y))
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def _flyer_png(seed: int, header: tuple[int, int, int]) -> bytes:
    """A placeholder flyer: coloured header, a grid of product cards with price tags (no readable text)."""
    cols, rows, w, h, head = 3, 4, 360, 480, 70
    cw, ch = w // cols, (h - head) // rows
    rng = random.Random(seed)
    cards = [[(rng.randint(150, 235), rng.randint(150, 235), rng.randint(120, 220)) for _ in range(cols)] for _ in range(rows)]
    def paint(x: int, y: int):
        if y < head:
            return header
        gx, gy = x // cw, (y - head) // ch
        lx, ly = x % cw, (y - head) % ch
        if gy >= rows or gx >= cols or lx < 6 or ly < 6 or lx > cw - 6 or ly > ch - 6:
            return (245, 243, 238)
        if ly > ch - 26 and lx < cw - 24:
            return (200, 40, 40)                                   # price tag
        return cards[gy][gx]
    return _png(w, h, paint)


FLYERS = [  # source, header colour, items (name, price, confidence), review
    ("이마트 주간 전단", (0, 120, 80), [("사과 1.5kg", 9900, 0.96), ("배 3입", 8900, 0.93), ("대파 1단", 2980, 0.9), ("라면 5입", 3980, 0.95),
                                       ("만두 800g", 7900, 0.88), ("계란 30구", 6990, 0.52), ("우유 1L", 2480, 0.64), ("마늘 500g", None, 0.4)], "pending"),
    ("쿠팡 가을 기획전 배너", (20, 90, 160), [("샤인머스캣 2kg", 19900, 0.91), ("양파 3kg", 3990, 0.94), ("고추장 500g", 5900, 0.9),
                                              ("그릭요거트 4입", 5480, 0.87)], "approved"),
    ("컬리 신상품 배너", (110, 60, 140), [("곤약면 6입", 8900, 0.83), ("냉동김밥 10입", 9900, 0.8), ("비건라면 4입", 6900, 0.72),
                                          ("밀키트 부대찌개", 12900, 0.58)], "pending"),
]


def _load_workflow_rows(engine, repo, store, settings, now: datetime, today: date, ids: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    # flyers with placeholder images; one already reviewed so its items feed the weekly promotion mentions
    review_dir = resolve_inbox(settings.inbox_dir) / "flyer" / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    monday = today - timedelta(days=today.weekday())
    for n, (source, colour, items, state) in enumerate(FLYERS, start=1):
        name = f"{DEMO_IMAGE_PREFIX}{n}.png"
        (review_dir / name).write_bytes(_flyer_png(n, colour))
        fid = add_flyer(store, source, f"flyer/review/{name}", monday, [FlyerItem(a, b, c) for a, b, c in items], now)
        if state == "approved":
            review_flyer(store, ACTOR.name, now, fid, [{"item_id": i["item_id"], "action": "approve"} for i in get_flyer(store, fid)["items"]])
    out["flyers"] = len(FLYERS)

    review = [ReviewItem("UNMATCHED_TITLE", "COUPANG", "[산지직송] 샤인머스캣 2kg", "no catalog item matches this title", {"price": 19900}),
              ReviewItem("UNMATCHED_TITLE", "COUPANG", "유기농 단호박 1.5kg", "no catalog item matches this title", {"price": 6900}),
              ReviewItem("UNMATCHED_TITLE", "KURLY", "제주 감귤 3kg 로얄과", "no catalog item matches this title", {"price": 14900}),
              ReviewItem("UNPARSEABLE_TITLE", "SSG", "프리미엄 과일 세트 (구성 상이)", "quantity or unit could not be read", {}),
              ReviewItem("QUARANTINED_LISTING", "COUPANG", "COUPANG-APPLE-X", "price 100 differs from the last price by more than 50%", {"sale_price": 100, "last_price": 9400}),
              ReviewItem("UNMAPPED_BENCHMARK", "KAMIS", "감귤", "no master_product_aliases entry", {}),
              ReviewItem("UNCONVERTIBLE_BENCHMARK", "GARAK", "방울토마토(박스)", "unit cannot be converted to the standard unit", {"unit": "박스"})]
    out["review_items"] = repo.add_review(review, now)

    sel = {"item": "article.product-card", "fields": {"title": {"css": "h3.title"}, "sale_price": {"css": "span.price strong"}, "item_code": {"css": "", "attr": "data-sku"}}}
    c1 = repo.add_candidate("COUPANG", sel, now - timedelta(days=2))
    c2 = repo.add_candidate("KURLY", {**sel, "item": "li.goods"}, now - timedelta(days=1))
    c3 = repo.add_candidate("SSG", {**sel, "item": "div.tmpl_item"}, now - timedelta(days=5))
    repo.decide_candidate(c3, "REJECTED", "demo", now - timedelta(days=4))
    out["selector_candidates"] = 3

    runs = []
    for cid, rows, every in (("report", 1, 1), ("buyer", 1, 1), ("flyer", 3, 7), ("newproduct", 2, 1), ("news", 6, 1), ("benchmark_kamis", 11, 1), ("benchmark_garak", 11, 1), ("competitor", 33, 1)):
        for d in range(7):
            if d % every:
                continue
            at = now - timedelta(days=d, hours=2)
            failed = (cid == "news" and d == 1) or (cid == "report" and d == 3)
            runs.append({"c": cid, "t": "MANUAL" if (cid == "flyer" and d == 0) else "SCHEDULED", "b": "admin@demo" if (cid == "flyer" and d == 0) else "",
                         "s": "FAILED" if failed else "OK", "q": at - timedelta(minutes=1), "a": at, "f": at + timedelta(seconds=40), "r": 0 if failed else rows,
                         "m": "feed returned HTTP 503" if cid == "news" else "file could not be read (encoding)" if failed else ""})
    with engine.begin() as c:
        for r in runs:
            c.execute(text("INSERT INTO crawler_runs (crawler_id, trigger_kind, requested_by, status, queued_at, started_at, finished_at, rows_loaded, message) "
                           "VALUES (:c,:t,:b,:s,:q,:a,:f,:r,:m)"), repo._b(r))
        for i, (key, msg) in enumerate((("REVERSE_MARGIN:APPLE-1KG:SHOPA", "Reverse margin risk: APPLE-1KG on SHOPA"), ("COMPETITOR_OOS:PEAR", "Competitor out of stock: Pear on SSG"),
                                        ("CRAWLER_FAILED:news", "Crawler failed: news"))):
            c.execute(text("INSERT INTO alert_log (created_at, alert_key, status, attempts, last_error, message) VALUES (:t,:k,'SENT',1,NULL,:m)"),
                      repo._b({"t": now - timedelta(hours=6 * (i + 1)), "k": key, "m": msg}))
    out["crawler_runs"] = len(runs)
    return out
