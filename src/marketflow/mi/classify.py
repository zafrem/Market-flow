"""Category classifier for keywords (SRS F-08-2.5): tags a search keyword with the internal large category.

Deterministic and explainable, built from what the system already knows:
  1. keywords already carrying a category in imported data (labels),
  2. catalog item names and aliases (master_products, master_product_aliases).
Exact matches are trusted; a keyword that merely contains a known item name is a weaker guess. Anything below the
confidence floor stays unclassified (admin-only, like any row without a category) instead of being guessed."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Guess:
    category: str | None
    confidence: float
    basis: str            # LABEL | CATALOG | CONTAINS | NONE


class Classifier:
    CONFIDENCE_FLOOR = 0.8

    def __init__(self, labelled: dict[str, Counter], catalog: dict[str, str]):
        self._labels = {k.lower(): c for k, c in labelled.items()}
        self._catalog = {k.lower(): v for k, v in catalog.items() if len(k) >= 2}

    def classify(self, keyword: str) -> Guess:
        k = (keyword or "").strip().lower()
        if not k:
            return Guess(None, 0.0, "NONE")
        votes = self._labels.get(k)
        if votes:
            cat, n = votes.most_common(1)[0]
            share = n / sum(votes.values())
            return Guess(cat if share >= 0.8 else None, round(0.6 + 0.35 * share, 2) if share >= 0.8 else 0.0, "LABEL" if share >= 0.8 else "NONE")
        if k in self._catalog:
            return Guess(self._catalog[k], 0.95, "CATALOG")
        hits = [(len(name), cat) for name, cat in self._catalog.items() if name in k]
        if hits:
            top = max(h[0] for h in hits)
            cats = {c for n, c in hits if n == top}
            if len(cats) == 1:                       # the longest item name inside the keyword decides, if unambiguous
                return Guess(next(iter(cats)), 0.8 if top >= 2 else 0.5, "CONTAINS")
        return Guess(None, 0.0, "NONE")

    def category_for(self, keyword: str) -> str | None:
        g = self.classify(keyword)
        return g.category if g.category and g.confidence >= self.CONFIDENCE_FLOOR else None


def evaluate(labelled: dict[str, Counter], catalog: dict[str, str]) -> dict:
    """Leave-one-out accuracy on keywords that already have a category: how often a guess is right and how often it abstains."""
    right = wrong = abstain = 0
    for kw, votes in labelled.items():
        truth = votes.most_common(1)[0][0]
        rest = {k: v for k, v in labelled.items() if k != kw}
        cat = Classifier(rest, catalog).category_for(kw)
        if cat is None:
            abstain += 1
        elif cat == truth:
            right += 1
        else:
            wrong += 1
    answered = right + wrong
    return {"keywords": len(labelled), "answered": answered, "abstained": abstain,
            "accuracy": round(right / answered, 3) if answered else None,
            "coverage": round(answered / len(labelled), 3) if labelled else None}
