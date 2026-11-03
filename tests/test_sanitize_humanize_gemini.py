import json
import random

import httpx
import pytest

from conftest import FIX
from marketflow.llm import GeminiClient, LLMError, MockLLM, get_llm
from marketflow.scrape.humanize import SCROLL_PAUSE_S, SCROLL_STEP_PX, TYPE_DELAY_MS, scroll_plan, typing_delays_ms, viewport
from marketflow.scrape.sanitize import sanitize_html
from marketflow.scrape.selfheal import LISTING_SCHEMA
from marketflow.settings import Settings


def test_sanitize_strips_noise_keeps_content_and_structure():
    out = sanitize_html((FIX / "html" / "shop_a.html").read_text(encoding="utf-8"))
    for noise in ("do-not-send", "base64", "<script", "<style", "<svg", "<img", "color:red"):
        assert noise not in out
    assert "성주 사과 1.2kg(600g x 2)" in out and "15,900원" in out and 'class="product-item"' in out
    assert len(out) < len((FIX / "html" / "shop_a.html").read_text(encoding="utf-8"))


def test_sanitize_truncates():
    assert len(sanitize_html("<p>" + "가" * 100_000 + "</p>", max_chars=500)) == 500


def test_typing_delays_within_50_150ms_and_seeded():
    a = typing_delays_ms("사과 1.2kg", random.Random(1))
    assert len(a) == 8 and all(TYPE_DELAY_MS[0] <= d <= TYPE_DELAY_MS[1] for d in a)
    assert a == typing_delays_ms("사과 1.2kg", random.Random(1))
    assert a != typing_delays_ms("사과 1.2kg", random.Random(2))


def test_scroll_plan_reaches_bottom_with_200_500px_steps_and_pauses():
    plan = scroll_plan(5000, 900, random.Random(7))
    assert sum(d for d, _ in plan) == 5000 - 900
    assert all(SCROLL_STEP_PX[0] <= d <= SCROLL_STEP_PX[1] for d, _ in plan[:-1]) and plan[-1][0] <= SCROLL_STEP_PX[1]
    assert all(SCROLL_PAUSE_S[0] <= p <= SCROLL_PAUSE_S[1] for _, p in plan)
    assert scroll_plan(500, 900, random.Random(7)) == []         # page fits the viewport


def test_viewport_varies_across_sessions():
    assert len({viewport(random.Random(i)) for i in range(30)}) > 1


async def test_mock_llm_default_is_empty_and_counts_calls():
    m = MockLLM()
    assert await m.extract_json("x", LISTING_SCHEMA) == {"items": []} and m.calls == 1


def test_get_llm_requires_key_when_not_mock():
    assert isinstance(get_llm(Settings(mock_llm=True)), MockLLM)
    with pytest.raises(RuntimeError, match="gemini_api_key"):
        get_llm(Settings(mock_llm=False))


def _gemini(handler):
    return GeminiClient("SECRET-KEY", "gemini-test", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_gemini_request_shape_and_response_parsing():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["key"], seen["body"] = str(req.url), req.headers.get("x-goog-api-key"), json.loads(req.content)
        payload = {"items": [{"product_title": "사과", "sale_price": 1000}]}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})

    out = await _gemini(handler).extract_json("<li>사과 1,000원</li>", LISTING_SCHEMA)
    assert out == {"items": [{"product_title": "사과", "sale_price": 1000}]}
    assert seen["url"].endswith("/models/gemini-test:generateContent") and "SECRET-KEY" not in seen["url"]
    assert seen["key"] == "SECRET-KEY"                                 # key in header, not URL
    cfg = seen["body"]["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and cfg["responseSchema"]["type"] == "OBJECT"
    assert cfg["responseSchema"]["properties"]["items"]["items"]["properties"]["sale_price"]["type"] == "INTEGER"
    assert "사과 1,000원" in seen["body"]["contents"][0]["parts"][0]["text"]


async def test_gemini_errors_are_typed_and_do_not_leak_body():
    with pytest.raises(LLMError, match="HTTP 429") as e:
        await _gemini(lambda r: httpx.Response(429, text="quota: SECRET-KEY page content")).extract_json("x", LISTING_SCHEMA)
    assert "SECRET-KEY" not in str(e.value)
    with pytest.raises(LLMError, match="unexpected payload"):
        await _gemini(lambda r: httpx.Response(200, json={"candidates": []})).extract_json("x", LISTING_SCHEMA)
    bad = {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}
    with pytest.raises(LLMError):
        await _gemini(lambda r: httpx.Response(200, json=bad)).extract_json("x", LISTING_SCHEMA)
