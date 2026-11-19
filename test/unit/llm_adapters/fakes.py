"""Fakes shared by the adapter tests: an MCP app and a recording LLM client."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


class FakeTool(SimpleNamespace):
    pass


class FakeMcpApp:
    """The two methods an adapter uses on a FastMCP app."""

    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self.handlers = handlers or {"echo": lambda args: {"echoed": args}}
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[FakeTool]:
        return [
            FakeTool(
                name=name,
                description=f"{name} tool",
                parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
            )
            for name in self.handlers
        ]

    async def call_tool(self, name: str, arguments: dict) -> Any:
        self.calls.append((name, arguments))
        out = self.handlers[name](arguments)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(structured_content=out, content=[])


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
