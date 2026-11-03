"""F-02: AI self-healing extraction.

selectors OK            -> source 'SELECTOR'
selectors fault (F-02-1)-> run-cache candidate? use it : sanitize -> LLM (F-02-2) -> validate (F-02-4)
                           -> synthesize + verify candidate selectors (F-02-3), pending HUMAN approval
approve()               -> RuleStore.promote(): candidate becomes the persistent rule (audited by actor)
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from bs4 import BeautifulSoup, Tag

from ..collectors.base import parse_price
from ..llm import LLMClient
from ..rules import current_rules, default_store
from .extract import ScrapeExtractionFault, extract_listings
from .sanitize import sanitize_html

log = logging.getLogger(__name__)

LISTING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "channel_item_code": {"type": "string"}, "product_title": {"type": "string"},
        "sale_price": {"type": "integer"}, "original_price": {"type": "integer"},
        "is_out_of_stock": {"type": "boolean"}, "badge_info": {"type": "string"}},
        "required": ["product_title", "sale_price"]}}},
    "required": ["items"],
}
FAULT_THRESHOLD = 3  # consecutive faults on a platform before escalating the alert


@dataclass
class HealResult:
    listings: list[dict[str, Any]]
    source: str                                   # SELECTOR | LLM
    quarantined: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    candidate_pending: bool = False


def validate_listing(item: dict[str, Any], max_dev_pct: float, last_price: int | None) -> str | None:
    """F-02-4. Returns a rejection reason, or None when the record is sane."""
    sale, orig = item.get("sale_price"), item.get("original_price") or item.get("sale_price")
    if not item.get("product_title"):
        return "empty title"
    if not isinstance(sale, int) or isinstance(sale, bool) or sale <= 0:
        return f"sale_price {sale!r} not a positive integer"
    if orig is not None and sale > orig:
        return f"sale_price {sale} > original_price {orig}"
    if last_price and abs(sale - last_price) / last_price * 100 > max_dev_pct:
        return f"sale_price {sale} deviates >{max_dev_pct:g}% from last accepted {last_price}"
    return None


def grounded(item: dict[str, Any], page_text: str, rules: dict) -> bool:
    """Anti-hallucination: the title and price the LLM returned must literally occur in the page."""
    clean = re.compile(rules["common"]["price_clean_pattern"])
    return " ".join(item["product_title"].split()) in page_text and str(item["sale_price"]) in clean.sub("", page_text)


def _css(el: Tag, nth: bool) -> str:
    if el.get("id"):
        return f"{el.name}#{el['id']}"
    if el.get("class"):
        return el.name + "".join("." + c for c in el["class"])
    if nth and el.parent:
        same = [s for s in el.parent.find_all(el.name, recursive=False)]
        return f"{el.name}:nth-of-type({same.index(el) + 1})"
    return el.name


def _deepest_with_text(soup: BeautifulSoup, match: Callable[[str], bool]) -> Tag | None:
    """First element whose text matches and none of whose descendants do (i.e. the innermost holder)."""
    for el in soup.find_all(True):
        if match(el.get_text(" ", strip=True)) and not any(match(c.get_text(" ", strip=True)) for c in el.find_all(True)):
            return el
    return None


def synthesize_selectors(html: str, current: dict[str, Any], items: list[dict[str, Any]], rules: dict) -> dict[str, Any] | None:
    """Derive item/title/sale_price selectors from where the LLM's values sit in the DOM, then VERIFY the
    candidate reproduces the LLM's (title, sale_price) list. Unverified candidates are discarded."""
    if not items:
        return None
    soup = BeautifulSoup(html, "html.parser")
    first = items[0]
    t_el = _deepest_with_text(soup, lambda s: s == first["product_title"])
    p_el = _deepest_with_text(soup, lambda s: parse_price(s, rules) == first["sale_price"])
    if not t_el or not p_el:
        return None
    container = t_el
    while container and p_el not in container.descendants:
        container = container.parent
    if not isinstance(container, Tag) or container.name in {"body", "html", "[document]"}:
        return None
    wanted = [(i["product_title"], i["sale_price"]) for i in items]
    for nth in (False, True):
        cand = copy.deepcopy(current)
        cand["item"] = _css(container, nth=False)
        cand["fields"]["title"] = {"css": _css(t_el, nth)}
        cand["fields"]["sale_price"] = {"css": _css(p_el, nth)}
        try:
            got = [(g["product_title"], g["sale_price"]) for g in extract_listings(html, cand, rules)]
        except Exception:
            continue
        if got == wanted:
            return cand
    return None


class SelfHealingExtractor:
    def __init__(self, llm: LLMClient, last_price: Callable[[str, str], int | None] = lambda p, c: None,
                 alert: Callable[[str], None] = lambda m: log.warning("ALERT %s", m),
                 on_candidate: Callable[[str, dict[str, Any]], None] | None = None):
        self._llm, self._last_price, self._alert, self._on_candidate = llm, last_price, alert, on_candidate
        self._run_cache: dict[str, dict[str, Any]] = {}      # candidate selectors, this run only
        self.pending: dict[str, dict[str, Any]] = {}         # awaiting human approval
        self.consecutive_faults: dict[str, int] = {}

    async def extract(self, platform: str, html: str) -> HealResult:
        rules = current_rules()
        rule = rules["retail"][platform]
        try:
            res = HealResult(extract_listings(html, rule["selectors"], rules), "SELECTOR")
            self.consecutive_faults[platform] = 0
            return res
        except ScrapeExtractionFault as fault:
            n = self.consecutive_faults[platform] = self.consecutive_faults.get(platform, 0) + 1
            self._alert(f"{platform}: selectors failed ({fault}); consecutive={n}"
                        + (" - ESCALATE" if n >= FAULT_THRESHOLD else ""))
        if (cand := self._run_cache.get(platform)) is not None:     # avoid repeat LLM calls (F-02-3)
            try:
                return HealResult(extract_listings(html, cand, rules), "SELECTOR", candidate_pending=True)
            except ScrapeExtractionFault:
                self._run_cache.pop(platform, None)
        return await self._llm_fallback(platform, html, rule, rules)

    async def _llm_fallback(self, platform: str, html: str, rule: dict, rules: dict) -> HealResult:
        data = await self._llm.extract_json(sanitize_html(html), LISTING_SCHEMA)
        good, bad = [], []
        page_text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())
        for it in data.get("items", []) or []:
            it = {"channel_item_code": it.get("channel_item_code") or "", "is_out_of_stock": bool(it.get("is_out_of_stock")),
                  "badge_info": it.get("badge_info") or None, **{k: it.get(k) for k in ("product_title", "sale_price", "original_price")}}
            if it["original_price"] is None:
                it["original_price"] = it["sale_price"]
            reason = validate_listing(it, rule.get("max_deviation_pct", 50), self._last_price(platform, it["channel_item_code"]))
            if reason is None and not grounded(it, page_text, rules):
                reason = "value not found in page text (possible hallucination)"
            (bad.append((it, reason)) if reason else good.append(it))
        for it, why in bad:
            self._alert(f"{platform}: quarantined {it.get('product_title')!r}: {why}")
        if not good:
            raise ScrapeExtractionFault(f"{platform}: LLM fallback produced no valid records")
        for it in good:  # codes absent from LLM output: stable hash, same as selector path
            if not it["channel_item_code"]:
                it["channel_item_code"] = hashlib.sha1(it["product_title"].encode()).hexdigest()[:12]
        cand = synthesize_selectors(html, rule["selectors"], good, rules)
        if cand:
            self._run_cache[platform] = self.pending[platform] = cand
            if self._on_candidate:
                self._on_candidate(platform, cand)                       # persist so a person can approve it later
            self._alert(f"{platform}: new selectors proposed; approve to persist")
        return HealResult(good, "LLM", bad, candidate_pending=cand is not None)

    def approve(self, platform: str, actor: str) -> None:
        """In-process approval gate (tests/REPL). Production approval goes through promote_candidate()."""
        promote_candidate(platform, self.pending.pop(platform), actor)
        self._run_cache.pop(platform, None)


def promote_candidate(platform: str, cand: dict[str, Any], actor: str) -> None:
    """Persist a verified candidate into config/overrides.json (validated by RuleStore.promote)."""
    store = default_store()
    base = ["retail", platform, "selectors"]
    store.promote(base + ["item"], cand["item"], actor)
    for f in ("title", "sale_price"):
        store.promote(base + ["fields", f], cand["fields"][f], actor)
