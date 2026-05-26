from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _cap_textual_pilot_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from textual.pilot import Pilot
    except Exception:
        return

    async def fast_wait_for_screen(self: Pilot, timeout: float = 30.0) -> bool:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return True

    async def fast_pause(self: Pilot, delay: float | None = None) -> None:
        await self._wait_for_screen()
        await asyncio.sleep(0 if delay is None else min(delay, 0.01))
        await asyncio.sleep(0)
        self.app.screen._on_timer_update()

    monkeypatch.setattr(Pilot, "_wait_for_screen", fast_wait_for_screen)
    monkeypatch.setattr(Pilot, "pause", fast_pause)
