"""The discovery view: what is new and what people are searching for, before any price comparison.

New competitor products (from our own scraped listings), keywords by search volume, fast-rising keywords, and for each
keyword whether the catalog has it at all. No cost or margin data is used, so every role may see it (within their categories)."""
from __future__ import annotations

from datetime import date
from typing import Any

from .insights import InsightConfig, new_products
from .mi.service import mi_report
from .mi.store import MiStore
from .ops_repo import OpsRepository
from .settings import Settings

TOP_N, RISING_N = 10, 8
RISING_STAGES = ("SEED", "EMERGING", "GROWING")


def _norm(s: str) -> str:
    return "".join(str(s).lower().split())


def catalog_terms(repo: OpsRepository) -> set[str]:
    """Names the catalog knows: item names and every alias (the Korean source names)."""
    rows = repo._rows("SELECT item_name AS n FROM master_products UNION SELECT alias AS n FROM master_product_aliases")
    return {_norm(r["n"]) for r in rows}


def discover(repo: OpsRepository, store: MiStore, settings: Settings, cfg: InsightConfig, allowed: set[str] | None,
             item_ok: dict[int, bool], today: date, lang: str, now) -> dict[str, Any]:
    report = mi_report(store, settings, allowed, True, lang, now)
    known = catalog_terms(repo)
    keywords = []
    for r in report["radar"]:
        series = r.get("search_series") or []
        searches = int(float(series[-1][1])) if series else 0
        keywords.append({"keyword": r["keyword"], "category": r["category"], "searches": searches, "wow": r.get("wow"), "yoy": r.get("yoy"),
                         "growth": r.get("growth"), "stage": r["stage"], "in_catalog": _norm(r["keyword"]) in known,
                         "action": r.get("action", ""), "changed": r.get("changed", ""), "series": series[-12:]})
    top = sorted(keywords, key=lambda k: k["searches"], reverse=True)[:TOP_N]
    rising = sorted((k for k in keywords if (k["growth"] or 0) > 0 and k["stage"] in RISING_STAGES), key=lambda k: k["growth"], reverse=True)[:RISING_N]
    items = {int(r["std_item_id"]): r for r in repo._rows("SELECT std_item_id, item_name, category_l, category_m FROM master_products")}
    ours = {int(r["std_item_id"]) for r in repo._rows("SELECT DISTINCT std_item_id FROM int_operations_inventory")}
    fresh = []
    for p in new_products(repo, today, cfg):
        if not item_ok.get(p["std_item_id"]):
            continue
        price = repo._rows("SELECT sale_price FROM ext_retail_prices WHERE platform=:p AND channel_item_code=:c ORDER BY scraped_at DESC LIMIT 1",
                           p=p["platform"], c=p["channel_item_code"])
        it = items.get(p["std_item_id"], {})
        fresh.append({**p, "item_name": it.get("item_name", ""), "category": it.get("category_l", ""),
                      "price": int(price[0]["sale_price"]) if price else None, "we_sell_item": p["std_item_id"] in ours})
    missing = [k for k in top if not k["in_catalog"]]
    return {"as_of": report["as_of"], "window_days": cfg.new_window_days, "top_searches": top, "rising": rising, "new_products": fresh,
            "summary": {"new_products": len(fresh), "rising": len(rising), "top_not_in_catalog": len(missing)}}
