"""LLM client seam. Phase 1 ships only the offline mock (MOCK_LLM=true); Gemini lands in Phase 2."""
from __future__ import annotations

import os
from typing import Any, Protocol


class LLMClient(Protocol):
    async def extract_json(self, content: str, schema: dict[str, Any]) -> dict[str, Any]: ...


class MockLLM:
    """Deterministic: returns a fixed payload (or one derived from the schema keys)."""

    def __init__(self, response: dict[str, Any] | None = None):
        self._response = response
        self.calls = 0

    async def extract_json(self, content: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self._response is not None:
            return dict(self._response)
        return {key: None for key in schema.get("properties", {})}


def get_llm() -> LLMClient:
    if os.getenv("MOCK_LLM", "true").lower() == "true":
        return MockLLM()
    raise NotImplementedError("Gemini client is a Phase 2 deliverable; set MOCK_LLM=true")
