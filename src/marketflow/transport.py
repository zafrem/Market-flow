"""Pluggable network transport (SRS §6.2 #1)."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol

import httpx


class Transport(Protocol):
    async def get_text(self, url: str, params: Mapping[str, str] | None = None) -> str: ...


class HttpTransport:
    def __init__(self, timeout: float = 30.0, retries: int = 2):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept-Language": "ko-KR"},
            transport=httpx.AsyncHTTPTransport(retries=retries),
        )

    async def get_text(self, url: str, params: Mapping[str, str] | None = None) -> str:
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.text

    async def aclose(self) -> None:
        await self._client.aclose()


class FixtureTransport:
    """Serves recorded responses. `routes` maps a URL substring to a file path or literal text."""

    def __init__(self, routes: Mapping[str, str | Path]):
        self._routes = dict(routes)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get_text(self, url: str, params: Mapping[str, str] | None = None) -> str:
        self.calls.append((url, dict(params or {})))
        for needle, body in self._routes.items():
            if needle in url:
                if isinstance(body, Path):          # Path -> file; str -> literal response body
                    return body.read_text(encoding="utf-8")
                return body
        raise LookupError(f"no fixture route for {url}")
