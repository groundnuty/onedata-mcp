"""One clock for the panel, so tests can run a 20 minute schedule in no time."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def time(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def time(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


REAL_CLOCK = RealClock()
