"""FastMCP tools → OpenAI tool-schema export.

Mirrors test/plgrid/mcp_openai_bridge.py but lives in the benchmark
package so the adapters can import it without depending on test/plgrid/.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP


async def build_openai_tools(app: FastMCP, allowed: frozenset[str]) -> list[dict[str, Any]]:
    """Filter the FastMCP tool catalogue to `allowed` and convert to the
    OpenAI tool-schema list format.

    Raises KeyError if `allowed` references a tool not exposed by `app`.
    """
    all_tools = await app.list_tools()
    by_name = {t.name: t for t in all_tools}
    missing = allowed - set(by_name)
    if missing:
        raise KeyError(f"Tools requested but not exposed by MCP server: {sorted(missing)}")
    selected = [by_name[name] for name in sorted(allowed)]
    return [_fastmcp_tool_to_openai(t) for t in selected]


def _fastmcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    parameters = (
        tool.parameters if tool.parameters is not None else {"type": "object", "properties": {}}
    )
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": parameters,
        },
    }
