"""A recording proxy around the in-process MCP app.

`ToolCall` (the PPAM runtime type) has no timestamps and its result is truncated, and a trial
cut by the wall cap loses the adapter's own trace. The proxy sits between an adapter and the
FastMCP app: it enforces the allow-list, bounds each call (the agent study's harness bounds one tool
call at 300 s), and records every call with its start and end time and its full result.
The records are what the oracles and the report read; the adapters are unchanged.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any

from fastmcp.exceptions import ToolError

from benchmark.llm_adapters.openai_compat import _result_to_text
from onedata_mcp.api.readiness import CALL_RECORD
from onedata_mcp.tool_maturity import OFFERED_TOOLS


@dataclass
class CallRecord:
    index: int
    tool: str
    arguments: dict
    started_at: float
    ended_at: float = 0.0
    ok: bool = False
    result_text: str | None = None
    error: str | None = None
    source: str = "mcp"  # "mcp" or "builtin" (CLI legs report both)
    # What the readiness tools recorded of their own call (`readiness.CALL_RECORD`: the files
    # resolved, the holds requested and the rules made), model-invisible; None for any other
    # tool, and in artefacts made before it existed (the scorer then reads the arguments).
    audit: dict | None = None

    def to_json(self) -> dict:
        return asdict(self)


class RecordingApp:
    def __init__(
        self,
        app: Any,
        *,
        allowed: frozenset[str],
        call_cap_s: float = 300.0,
        clock=time.time,
    ) -> None:
        self._app = app
        self.allowed = allowed
        self.call_cap_s = call_cap_s
        self._clock = clock
        self.calls: list[CallRecord] = []
        self._offered: frozenset[str] | None = None

    async def list_tools(self) -> list[Any]:
        """The scenario's tools only: a model never sees a tool it may not call."""
        return [t for t in await self._app.list_tools() if t.name in self.allowed]

    async def offered(self) -> frozenset[str]:
        """The tools the model is offered: those the app registers AND the scenario allows (a
        name on the allow-list that the app does not register is not offered)."""
        if self._offered is None:
            self._offered = frozenset(t.name for t in await self.list_tools())
        return self._offered

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        record = CallRecord(
            index=len(self.calls), tool=name, arguments=dict(arguments), started_at=self._clock()
        )
        self.calls.append(record)
        # The tools see what the model was offered, not every tool the app registers nor every
        # name allowed: a tool's text names another tool only when it is offered
        # (`tool_maturity.OFFERED_TOOLS`).
        offered = OFFERED_TOOLS.set(await self.offered())
        audit: dict = {}
        recording = CALL_RECORD.set(audit)
        try:
            if name not in self.allowed:
                raise ToolError(f"tool {name!r} is not offered in this scenario")
            try:
                result = await asyncio.wait_for(
                    self._app.call_tool(name, arguments), timeout=self.call_cap_s
                )
            except TimeoutError:
                raise ToolError(f"tool call exceeded {self.call_cap_s:.0f}s") from None
            record.ok = True
            record.result_text = _result_to_text(result)
            return result
        except Exception as e:
            record.error = str(e)
            record.result_text = record.result_text or None
            raise
        finally:
            CALL_RECORD.reset(recording)
            OFFERED_TOOLS.reset(offered)
            record.audit = audit or None
            record.ended_at = self._clock()
