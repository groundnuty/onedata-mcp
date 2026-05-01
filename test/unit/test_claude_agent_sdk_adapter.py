"""Unit tests for ClaudeAgentSdkAdapter.

Mocks the SDK's `query()` function to verify the adapter correctly:
- Translates HEADLINE tool names into the SDK's `mcp__onedata__*` form
- Captures ToolUseBlock → ToolCall (with arguments)
- Splices ToolResultBlock → ToolCall.result
- Emits the final ResultMessage.result as TrialDispatch.final_answer
- Records denied (out-of-allowlist) calls in raw_trace.denied_calls
- Surfaces "onedata-mcp binary missing" cleanly when PATH lacks it

No live LLM calls, no Anthropic API.
"""

from __future__ import annotations

from typing import Any  # noqa: F401  # used by helper signatures
from unittest.mock import MagicMock

import pytest

from benchmark.llm_adapters import ClaudeAgentSdkAdapter, LLMConfig

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAsyncIterator:
    """Minimal async iterator that yields a pre-baked list of SDK messages."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def _make_assistant_msg(content_blocks):
    """Build a stand-in AssistantMessage. We only need .content."""
    msg = MagicMock(spec=["content"])
    msg.content = content_blocks
    # Patch isinstance() to recognise this as AssistantMessage
    return msg


def _make_user_msg(content_blocks):
    msg = MagicMock(spec=["content"])
    msg.content = content_blocks
    return msg


def _make_result_msg(*, result_text: str, usage_in: int = 0, usage_out: int = 0):
    msg = MagicMock(spec=["result", "subtype", "usage"])
    msg.result = result_text
    msg.subtype = "success"
    msg.usage = {"input_tokens": usage_in, "output_tokens": usage_out}
    return msg


def _make_tool_use_block(*, id_: str, name: str, input_: dict):
    blk = MagicMock(spec=["id", "name", "input"])
    blk.id = id_
    blk.name = name
    blk.input = input_
    return blk


def _make_text_block(text: str):
    blk = MagicMock(spec=["text"])
    blk.text = text
    return blk


def _make_tool_result_block(*, tool_use_id: str, content, is_error: bool = False):
    blk = MagicMock(spec=["tool_use_id", "content", "is_error"])
    blk.tool_use_id = tool_use_id
    blk.content = content
    blk.is_error = is_error
    return blk


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_returns_error_when_onedata_mcp_missing(monkeypatch):
    """If `onedata-mcp` isn't on PATH, the adapter returns an actionable
    error TrialDispatch instead of crashing or proceeding."""
    from benchmark.llm_adapters import claude_agent_sdk as adapter_mod

    monkeypatch.setattr(adapter_mod.shutil, "which", lambda name: None)

    adapter = ClaudeAgentSdkAdapter(LLMConfig(name="c", model_id="claude-test"))
    out = await adapter.dispatch(
        system_prompt="sys",
        user_prompt="hi",
        mcp_app=None,
        allowed_tools=frozenset({"list_user_spaces"}),
    )
    assert out.finish_reason == "adapter_error"
    assert "onedata-mcp" in (out.error or "")


@pytest.mark.asyncio
async def test_adapter_parses_tool_use_and_result(monkeypatch):
    """The adapter turns ToolUseBlock + ToolResultBlock into one ToolCall
    with arguments, succeeded=True, and result text."""
    from benchmark.llm_adapters import claude_agent_sdk as adapter_mod

    monkeypatch.setattr(adapter_mod.shutil, "which", lambda name: f"/fake/{name}")

    # Pin the SDK types so MagicMock(spec=...) makes our fakes pass isinstance().
    real_assistant = adapter_mod.AssistantMessage
    real_user = adapter_mod.UserMessage
    real_result = adapter_mod.ResultMessage
    real_tool_use = adapter_mod.ToolUseBlock
    real_tool_result = adapter_mod.ToolResultBlock

    tool_use = MagicMock(spec=real_tool_use)
    tool_use.id = "tu_abc"
    tool_use.name = "mcp__onedata__list_user_spaces"
    tool_use.input = {}

    tool_result = MagicMock(spec=real_tool_result)
    tool_result.tool_use_id = "tu_abc"
    tool_result.is_error = False
    tool_result.content = [{"type": "text", "text": '[{"name":"ppam_2026_mcp_tests"}]'}]

    assistant_msg = MagicMock(spec=real_assistant)
    assistant_msg.content = [tool_use]

    user_msg = MagicMock(spec=real_user)
    user_msg.content = [tool_result]

    result_msg = MagicMock(spec=real_result)
    result_msg.result = "Done."
    result_msg.subtype = "success"
    result_msg.usage = {"input_tokens": 100, "output_tokens": 5}

    captured_query_kwargs = {}

    def fake_query(*, prompt, options):
        captured_query_kwargs["prompt"] = prompt
        captured_query_kwargs["options"] = options
        return _FakeAsyncIterator([assistant_msg, user_msg, result_msg])

    monkeypatch.setattr(adapter_mod, "query", fake_query)

    adapter = ClaudeAgentSdkAdapter(LLMConfig(name="c", model_id="claude-test", max_tool_rounds=5))
    out = await adapter.dispatch(
        system_prompt="sys",
        user_prompt="brief",
        mcp_app=None,
        allowed_tools=frozenset({"list_user_spaces"}),
    )

    # The adapter should have requested the SDK with the prefixed tool name.
    options = captured_query_kwargs["options"]
    assert options.allowed_tools == ["mcp__onedata__list_user_spaces"]
    assert options.max_turns == 5
    # The SDK gets the user prompt verbatim.
    assert captured_query_kwargs["prompt"] == "brief"

    # Tool call captured + result spliced in.
    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc.tool_name == "list_user_spaces"  # prefix stripped
    assert tc.succeeded is True
    assert tc.result == '[{"name":"ppam_2026_mcp_tests"}]'

    # Final answer + usage came from ResultMessage.
    assert out.final_answer == "Done."
    assert out.usage_in_tokens == 100
    assert out.usage_out_tokens == 5
    assert out.rounds_used == 1
    # No denied calls (every call was in the allowlist).
    assert out.raw_trace.get("denied_calls") == []


@pytest.mark.asyncio
async def test_adapter_records_denied_calls_for_out_of_allowlist(monkeypatch):
    """If can_use_tool denies a tool (because it's not on the allowlist),
    the adapter records it for the trial trace."""
    from benchmark.llm_adapters import claude_agent_sdk as adapter_mod

    monkeypatch.setattr(adapter_mod.shutil, "which", lambda name: f"/fake/{name}")

    captured = {}

    def fake_query(*, prompt, options):
        captured["can_use_tool"] = options.can_use_tool
        return _FakeAsyncIterator([])

    monkeypatch.setattr(adapter_mod, "query", fake_query)

    adapter = ClaudeAgentSdkAdapter(LLMConfig(name="c", model_id="claude-test"))
    await adapter.dispatch(
        system_prompt="sys",
        user_prompt="brief",
        mcp_app=None,
        allowed_tools=frozenset({"list_user_spaces"}),
    )

    # Now invoke the captured can_use_tool callback directly with a built-in
    # tool name and verify it returns deny.
    can_use_tool = captured["can_use_tool"]
    out = await can_use_tool("Bash", {"command": "ls"}, MagicMock())
    assert out.behavior == "deny"
    assert "not in the benchmark allowlist" in out.message

    # And with an allowed tool name → allow.
    out2 = await can_use_tool("mcp__onedata__list_user_spaces", {}, MagicMock())
    assert out2.behavior == "allow"
