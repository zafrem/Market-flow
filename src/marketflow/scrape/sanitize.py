"""F-02-2: strip scripts/styles/SVG/base64 so the page is token-cheap before it goes to the LLM."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment

_DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")
_NOISE = ("script", "style", "svg", "noscript", "iframe", "link", "meta", "head", "img", "picture", "video")


def sanitize_html(html: str, max_chars: int = 30_000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(list(_NOISE)):
        el.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for el in soup.find_all(True):  # keep only structure-bearing attributes
        el.attrs = {k: _DATA_URI.sub("", v if isinstance(v, str) else " ".join(v))
                    for k, v in el.attrs.items() if k in {"class", "id", "data-id", "data-code"}}
    out = re.sub(r"\s+", " ", str(soup)).strip()
    return out[:max_chars]
