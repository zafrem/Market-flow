"""F-01-2 human-like pacing as pure, seedable plans (testable without a browser)."""
from __future__ import annotations

import random

TYPE_DELAY_MS = (50, 150)
SCROLL_STEP_PX = (200, 500)
SCROLL_PAUSE_S = (0.3, 1.2)


def typing_delays_ms(text: str, rng: random.Random) -> list[int]:
    return [rng.randint(*TYPE_DELAY_MS) for _ in text]


def scroll_plan(page_height: int, viewport: int, rng: random.Random) -> list[tuple[int, float]]:
    """[(delta_px, pause_s), ...] until the page bottom is reached."""
    plan, pos = [], 0
    while pos + viewport < page_height:
        step = min(rng.randint(*SCROLL_STEP_PX), page_height - viewport - pos)
        plan.append((step, round(rng.uniform(*SCROLL_PAUSE_S), 2)))
        pos += step
    return plan


def viewport(rng: random.Random) -> tuple[int, int]:
    return rng.choice([(1366, 768), (1440, 900), (1536, 864), (1920, 1080)])
