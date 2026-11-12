"""Flyer / banner reading through the OpenAI API (chat completions with an image).

Outward-facing: the image leaves this machine. It is sent only when an OpenAI key is configured AND the flyer crawler is
enabled, only for files an operator put in the flyer inbox, never logged, and only the product names and prices that come
back are kept (rows containing personal data are dropped). The API key and the response body are never logged.
UNVERIFIED against the live API in this repo's tests (needs a key); the request shape follows the public docs."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .mi.privacy import contains_pii

log = logging.getLogger(__name__)
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 200

Post = Callable[[str, dict[str, Any], dict[str, str]], Awaitable[tuple[int, Any]]]

PROMPT = ("This is a retail promotion flyer or banner. List every product that is advertised with a name and, if shown, its sale "
          "price in Korean won (integer). Use only what is printed in the image; never guess a price. Skip store addresses, "
          "phone numbers and anything about people. Return JSON: {\"items\": [{\"name\": str, \"price\": int or null}]}.")


class VisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FlyerItem:
    name: str
    price: int | None


def image_mime(data: bytes) -> str | None:
    """The type is decided from the bytes, not from the file name."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _httpx_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(url, json=body, headers=headers)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def parse_items(payload: Any) -> list[FlyerItem]:
    """Validate what the model returned: names are short text without personal data, prices are positive integers or null."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise VisionError("the model returned an unexpected payload")
    out: list[FlyerItem] = []
    for it in payload["items"][:MAX_ITEMS]:
        if not isinstance(it, dict) or not isinstance(it.get("name"), str):
            continue
        name = " ".join(it["name"].split())[:80]
        if len(name) < 2 or contains_pii(name):
            continue
        price = it.get("price")
        price = price if isinstance(price, int) and not isinstance(price, bool) and 0 < price < 10_000_000 else None
        out.append(FlyerItem(name, price))
    return out


class OpenAIVision:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini", post: Post = _httpx_post):
        if not api_key:
            raise VisionError("no OpenAI key configured")
        self._key, self._model, self._post = api_key, model, post

    async def read_flyer(self, image: bytes) -> list[FlyerItem]:
        mime = image_mime(image)
        if mime is None:
            raise VisionError("not a PNG, JPEG or WebP image")
        if len(image) > MAX_IMAGE_BYTES:
            raise VisionError("image larger than 5 MB")
        body = {"model": self._model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}", "detail": "high"}}]}]}
        status, data = await self._post(OPENAI_URL, body, {"Authorization": "Bearer " + self._key})
        if status != 200:
            raise VisionError(f"OpenAI HTTP {status}")                      # body omitted: it may echo request content
        try:
            return parse_items(json.loads(data["choices"][0]["message"]["content"]))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            if isinstance(e, VisionError):
                raise
            raise VisionError("OpenAI returned an unexpected payload") from e
