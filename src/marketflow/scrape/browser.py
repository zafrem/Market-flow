"""Optional Playwright transport (pip install '.[browser]' && playwright install chromium).
Localised for Korea (ko-KR, Asia/Seoul) and paced humanely. Deliberately NO stealth/IP-rotation:
those stay off until a connector's legal review is recorded (SRS F-01-2 compliance gate)."""
from __future__ import annotations

import asyncio
import random
from typing import Mapping

from .humanize import scroll_plan, viewport


class PlaywrightTransport:
    def __init__(self, rng: random.Random | None = None, headless: bool = True):
        self._rng, self._headless = rng or random.Random(), headless
        self._pw = self._browser = None

    async def get_text(self, url: str, params: Mapping[str, str] | None = None) -> str:
        from playwright.async_api import async_playwright   # lazy: optional dependency
        if self._pw is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
        w, h = viewport(self._rng)
        ctx = await self._browser.new_context(viewport={"width": w, "height": h}, locale="ko-KR", timezone_id="Asia/Seoul")
        try:
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            height = await page.evaluate("document.body.scrollHeight")
            for delta, pause in scroll_plan(height, h, self._rng):
                await page.mouse.wheel(0, delta)
                await asyncio.sleep(pause)
            return await page.content()
        finally:
            await ctx.close()

    async def aclose(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
