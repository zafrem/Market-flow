"""Selector-driven listing extraction. Selectors come from config (retail.<PLATFORM>.selectors)."""
from __future__ import annotations

import hashlib
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..collectors.base import parse_price
from ..rules import Rules

CRITICAL = ("title", "sale_price")


class ScrapeExtractionFault(RuntimeError):
    """Selectors no longer match the page (F-02-1)."""


def _read(el: Tag, spec: dict[str, Any]) -> Any:
    target = el if spec.get("css", "") == "" else el.select_one(spec["css"])
    if spec.get("exists"):
        return target is not None
    if target is None:
        return None
    if spec.get("attr"):
        return target.get(spec["attr"])
    return target.get_text(" ", strip=True) or None


def extract_listings(html: str, selectors: dict[str, Any], rules: Rules) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(selectors["item"])
    if not items:
        raise ScrapeExtractionFault(f"no elements match item selector {selectors['item']!r}")
    out = []
    for el in items:
        raw = {name: _read(el, spec) for name, spec in selectors["fields"].items()}
        title = (raw.get("title") or "").strip() if raw.get("title") else ""
        sale = parse_price(raw.get("sale_price"), rules)
        if not title or sale is None:
            missing = [n for n, v in (("title", title), ("sale_price", sale)) if not v]
            raise ScrapeExtractionFault(f"critical field(s) {missing} NULL")
        orig = parse_price(raw.get("original_price"), rules) or sale
        out.append({
            "channel_item_code": raw.get("item_code") or hashlib.sha1(title.encode()).hexdigest()[:12],
            "product_title": title, "sale_price": int(sale), "original_price": int(max(orig, sale)),
            "is_out_of_stock": bool(raw.get("out_of_stock")), "badge_info": raw.get("badge"),
        })
    return out
