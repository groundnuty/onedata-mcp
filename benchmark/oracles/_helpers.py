"""Small parsing + tool-call inspection helpers used across oracles."""

from __future__ import annotations

import re
from collections.abc import Callable

from benchmark._runtime_types import AgentTrace, ToolCall

# Path-like substring extractor: anything starting with /<space>/...
PATH_RE = re.compile(r"/[A-Za-z][A-Za-z0-9_\-]*/[^\s,;'\"`)\]]+")


def extract_paths(text: str, anchor: str | None = None) -> set[str]:
    """Pull paths from a free-form agent answer.

    Robust against bullet points, backticks, list separators. Optionally
    filter to paths starting with `anchor` (e.g. '/ppam_2026_mcp_tests/').
    """
    found = set(PATH_RE.findall(text))
    if anchor:
        found = {p for p in found if p.startswith(anchor)}
    return found


def extract_int(text: str, key: str) -> int | None:
    """Pull `<key>=<int>` from text. Returns None if not found / not int."""
    m = re.search(rf"{re.escape(key)}\s*[=:]\s*(\d+)", text)
    return int(m.group(1)) if m else None


def extract_kv_lines(text: str) -> dict[str, str]:
    """Pull 'key: value' lines into a dict (best-effort)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().strip("`\"'")
            v = v.strip().strip("`\"',").strip()
            if k and v:
                out[k] = v
    return out


def contains_token(text: str, token: str) -> bool:
    """Case-insensitive substring match — used for 'agent claimed X' checks."""
    return token.lower() in text.lower()


# ---------------------------------------------------------------------------
# Tool-call inspection (mcp_pass axis)
# ---------------------------------------------------------------------------


def find_calls(
    trace: AgentTrace,
    tool_name: str,
    args_predicate: Callable[[dict], bool] | None = None,
) -> list[ToolCall]:
    """Return tool calls matching `tool_name` (and optionally `args_predicate`).

    Used by static + dynamic oracles to verify the agent's MCP-call sequence.
    Both successful and failed calls are returned — Onedata-side failures do
    NOT disqualify the agent (per design/06).
    """
    matches: list[ToolCall] = []
    for call in trace.tool_calls:
        if call.tool_name != tool_name:
            continue
        if args_predicate is None or args_predicate(call.arguments):
            matches.append(call)
    return matches


def has_successful_call(
    trace: AgentTrace,
    tool_name: str,
    args_predicate: Callable[[dict], bool] | None = None,
) -> bool:
    """True if at least one matching tool call returned a non-error response.

    Strictly speaking, per design/06 we count any RECEIVED call as MCP
    success — but for the static-oracle predicate "did the agent
    successfully complete this action" we want at least one call to have
    not errored, since otherwise the agent's reasoning never had a chance
    to read a successful response.

    Use this for "agent successfully wrote X" predicates.
    Use `find_calls` (returns all, including failed) for "agent attempted X".
    """
    return any(call.succeeded for call in find_calls(trace, tool_name, args_predicate))
