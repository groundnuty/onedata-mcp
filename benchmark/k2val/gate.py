"""The federation health gate: a trial starts only when the federation answers.

The federation of a panel run may live on a shared host, where a load guard stops its containers
whenever the host is busy. A trial started then fails at once (`connection_error` while the fixture
is built: RESET_FAIL), and so does every later one: a night's batch is a heap of failures that say
nothing about the tools. The gate stands before each trial's fixture: it asks the federation (one
cheap request to each service, `FedApi.ping`), and when that fails it WAITS, polling every
`POLL_S`, for at most `[run] federation_wait_s`. A trial already running when the federation dies
keeps its usual handling (a RESET_FAIL or an error of its own); the next one waits here.

One gate serves every config of a run, so one outage is one wait: the first trial to find the
federation down starts the clock and polls; the others queue behind it and, once it is back, each
asks once and goes on. When the wait runs out, the one that waited gets `FederationWaitTimeout`,
and so does every trial that asks while the federation is still down (the clock is the outage's,
not the trial's), so a federation that never returns costs one `federation_wait_s`, not one per
remaining trial. The first probe that succeeds ends the outage.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from benchmark.k2val.clock import Clock
from benchmark.k2val.federation import FederationUnreachable
from benchmark.k2val.secrets import say

POLL_S = 60.0


class Pingable(Protocol):
    async def ping(self) -> None: ...


class FederationWaitTimeout(RuntimeError):
    """The federation stayed unreachable for the whole wait. The message is the RESET_FAIL
    reason: it begins `federation unreachable for N s`."""

    def __init__(self, waited_s: float, last: str) -> None:
        super().__init__(f"federation unreachable for {waited_s:.0f} s (last probe: {last})")
        self.waited_s = waited_s
        self.last = last


class FederationGate:
    def __init__(
        self, fed: Pingable, clock: Clock, *, wait_s: float, poll_s: float = POLL_S
    ) -> None:
        self.fed = fed
        self.clock = clock
        self.wait_s = wait_s
        self.poll_s = poll_s
        self._lock = asyncio.Lock()
        # when the current outage was first seen; None while the federation answers
        self._down_since: float | None = None

    async def _why_not(self) -> str | None:
        """None if the federation answers, else what it did not answer."""
        try:
            await self.fed.ping()
        except FederationUnreachable as e:
            return str(e)
        return None

    def _resumed(self) -> None:
        """The federation answers: the outage, if there was one, is over."""
        if self._down_since is not None:
            say(f"[k2val] federation resumed after {self.clock.time() - self._down_since:.0f} s")
            self._down_since = None

    async def wait(self) -> None:
        """Return when the federation answers; raise FederationWaitTimeout when it has been
        unreachable for `wait_s`."""
        async with self._lock:
            why = await self._why_not()
            if why is None:
                self._resumed()
                return
            if self._down_since is None:
                self._down_since = self.clock.time()
                if self.wait_s > 0:
                    say("[k2val] federation unreachable; waiting")
            while True:
                waited = self.clock.time() - self._down_since
                if waited >= self.wait_s:
                    raise FederationWaitTimeout(waited, why)
                await self.clock.sleep(min(self.poll_s, self.wait_s - waited))
                why = await self._why_not()
                if why is None:
                    self._resumed()
                    return
