"""Retail scraper: compliance gate -> fetch -> self-healing extract -> match -> normalize -> store."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from decimal import Decimal
from typing import Any

from ..ops_repo import ReviewItem
from ..repository import RetailRepository
from ..rules import current_rules
from ..time_provider import TimeProvider
from ..transport import Transport
from ..units import UnitError
from ..title_parser import unit_price
from .selfheal import SelfHealingExtractor

log = logging.getLogger(__name__)
MIN_MATCH_CONFIDENCE = Decimal("0.8")
ALIAS_MATCH_CONFIDENCE = Decimal("0.900")


class ComplianceError(RuntimeError):
    """Connector not cleared for production use (SRS F-01-2 compliance gate)."""


def assert_cleared(platform: str, rule: dict[str, Any]) -> None:
    if rule.get("access_method") == "SCRAPE" and not (rule.get("legal_review_ref") or "").strip():
        raise ComplianceError(f"{platform}: access_method=SCRAPE requires retail.{platform}.legal_review_ref "
                              "(record the ToS/legal review) before it can run")


@dataclass
class RetailResult:
    stored: int = 0
    source: str = "SELECTOR"
    unmatched: list[str] = field(default_factory=list)       # no catalog match -> review queue
    unparseable: list[str] = field(default_factory=list)     # F-03-1 UNPARSEABLE -> review queue
    quarantined: int = 0
    quarantined_detail: list[tuple[str, str]] = field(default_factory=list)   # (title, reason)
    candidate_pending: bool = False


def match_item(title: str, aliases: dict[str, tuple[int, str]]) -> tuple[int, str] | None:
    hits = [a for a in aliases if a in title]
    return aliases[max(hits, key=len)] if hits else None


async def scrape_platform(platform: str, transport: Transport, tp: TimeProvider, extractor: SelfHealingExtractor,
                          repo: RetailRepository, review: Callable[[list[ReviewItem], datetime], int] | None = None) -> RetailResult:
    rules = current_rules()
    rule = rules["retail"][platform]
    assert_cleared(platform, rule)
    aliases = repo.load_aliases("RETAIL")
    res = RetailResult()
    now = tp.now().replace(tzinfo=None)  # DATETIME columns are UTC (SRS)
    for url in rule.get("urls", []):
        healed = await extractor.extract(platform, await transport.get_text(url))
        res.source = healed.source if res.source == "SELECTOR" else res.source
        res.quarantined += len(healed.quarantined)
        res.quarantined_detail += [(i.get("product_title") or str(i.get("channel_item_code")), why) for i, why in healed.quarantined]
        res.candidate_pending |= healed.candidate_pending
        rows = []
        for it in healed.listings:
            hit = match_item(it["product_title"], aliases)
            if hit is None:
                res.unmatched.append(it["product_title"])
                continue
            std_id, std_unit = hit
            try:
                norm = unit_price(it["product_title"], it["sale_price"], std_unit, rules)
            except UnitError:
                res.unparseable.append(it["product_title"])
                continue
            coupon, ship = int(it.get("coupon_discount") or 0), int(it.get("shipping_fee") or 0)
            bonus = Decimal(str(it.get("bonus_qty") or 0))
            paid = max(it["sale_price"] - coupon, 0) + ship                  # what the buyer pays per purchase
            effective = (norm * Decimal(paid) / Decimal(it["sale_price"]) / (1 + bonus)).quantize(Decimal("0.01"))
            rows.append({
                "coupon_discount": coupon, "shipping_fee": ship, "bonus_qty": bonus, "effective_unit_price": effective,
                "scraped_at": now, "platform": platform, "std_item_id": std_id,
                "match_confidence": ALIAS_MATCH_CONFIDENCE, "channel_item_code": it["channel_item_code"],
                "product_title": it["product_title"][:255], "original_price": it["original_price"],
                "sale_price": it["sale_price"], "normalized_unit_price": norm,
                "is_out_of_stock": int(it["is_out_of_stock"]), "badge_info": it["badge_info"],
                "extraction_source": healed.source, "region_code": rule["region_code"],
            })
        res.stored += repo.insert_retail(rows)
        propose = getattr(repo, "propose_matches", None)
        if propose:                                    # unconfirmed candidates for a person to review (F-08-2.3)
            propose([{"platform": r["platform"], "channel_item_code": r["channel_item_code"], "std_item_id": r["std_item_id"],
                      "match_confidence": r["match_confidence"]} for r in rows], now)
    if review:
        review([*(ReviewItem("UNMATCHED_TITLE", platform, t, "no catalog item matches this title") for t in res.unmatched),
                *(ReviewItem("UNPARSEABLE_TITLE", platform, t, "quantity/unit could not be determined") for t in res.unparseable),
                *(ReviewItem("QUARANTINED_LISTING", platform, t, why) for t, why in res.quarantined_detail)], now)
    log.info("%s: stored=%d unmatched=%d unparseable=%d quarantined=%d source=%s", platform, res.stored,
             len(res.unmatched), len(res.unparseable), res.quarantined, res.source)
    return res
