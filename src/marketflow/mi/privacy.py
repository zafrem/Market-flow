"""Personal data must not enter the analysis (SRS F-08-N3). Search logs sometimes contain what a customer typed by
mistake: a phone number, an e-mail, a resident number or a street address. Rows whose keyword or category matches are
dropped on import (and counted), never stored."""
from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"(?<!\d)01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)"),                  # mobile phone
    re.compile(r"(?<!\d)0\d{1,2}[-\s.]\d{3,4}[-\s.]\d{4}(?!\d)"),                      # landline with separators
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                                              # e-mail
    re.compile(r"(?<!\d)\d{6}[-\s]?[1-4]\d{6}(?!\d)"),                                    # resident registration number
    re.compile(r"[가-힣0-9]+(?:로|길)\s*\d+(?:-\d+)?(?:\s*번?길)?(?:\s|$)"),                # road address with a number
    re.compile(r"[가-힣]+(?:시|도)\s+[가-힣]+(?:구|군|시)\s+[가-힣0-9]+(?:동|읍|면|리)"),       # city + district + neighbourhood
]


def contains_pii(text: str) -> bool:
    return any(p.search(text) for p in _PATTERNS)
