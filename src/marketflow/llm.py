"""LLM client seam: offline MockLLM (MOCK_LLM=true, default) and a Gemini REST client."""
from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMClient(Protocol):
    async def extract_json(self, content: str, schema: dict[str, Any]) -> dict[str, Any]: ...


class MockLLM:
    """Deterministic: returns a fixed payload (or an empty one derived from the schema keys)."""

    def __init__(self, response: dict[str, Any] | None = None):
        self._response = response
        self.calls = 0

    async def extract_json(self, content: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self._response is not None:
            return json.loads(json.dumps(self._response))
        return {key: [] if schema["properties"][key].get("type") == "array" else None for key in schema.get("properties", {})}


class LLMError(RuntimeError):
    pass


class GeminiClient:
    """Schema-constrained JSON extraction via the Gemini REST API.
    UNVERIFIED against the live API in this repo's tests (needs a key); request shape follows the public docs."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", client: httpx.AsyncClient | None = None):
        self._key, self._model = api_key, model
        self._client = client or httpx.AsyncClient(timeout=60)

    async def extract_json(self, content: str, schema: dict[str, Any]) -> dict[str, Any]:
        body = {
            "contents": [{"parts": [{"text":
                "Extract the product listings from this sanitized e-commerce page. Prices are integer KRW. "
                "Return only data present in the page; never invent values.\n\n" + content}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": _gemini_schema(schema),
                                 "temperature": 0},
        }
        resp = await self._client.post(GEMINI_URL.format(model=self._model), json=body,
                                       headers={"x-goog-api-key": self._key})
        if resp.status_code != 200:
            raise LLMError(f"Gemini HTTP {resp.status_code}")   # body omitted: may echo request content
        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            out = json.loads(text)
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError("Gemini returned an unexpected payload") from e
        if not isinstance(out, dict):
            raise LLMError("Gemini returned non-object JSON")
        return out


def _gemini_schema(s: dict[str, Any]) -> dict[str, Any]:
    """Gemini's schema dialect uses upper-case type names."""
    if isinstance(s, dict):
        return {k: (v.upper() if k == "type" and isinstance(v, str) else _gemini_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_gemini_schema(x) for x in s]
    return s


def get_llm(settings=None) -> LLMClient:
    from .settings import load_settings
    s = settings or load_settings()
    if s.mock_llm:
        return MockLLM()
    if not s.gemini_api_key:
        raise RuntimeError("llm.mock=false but llm.gemini_api_key is empty (config/settings.toml)")
    return GeminiClient(s.gemini_api_key, s.gemini_model)
