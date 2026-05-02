"""Unit tests for OpenAICompatAdapter — reasoning_content echo path.

SiliconFlow / DeepSeek thinking-mode contract: when the response carries
`reasoning_content`, that field MUST be echoed back on the prior-turn
assistant message in the next request, or SiliconFlow returns HTTP 400
(code 20015). Exercised by the V4-pro leg of the PPAM 2026 panel.

These tests verify the wiring shape:
- response WITH `reasoning_content` → next-turn assistant entry includes it
- response WITHOUT `reasoning_content` → next-turn assistant entry does NOT
  include the key (no spurious empty echo for non-reasoning models)

Live SiliconFlow round-trip is exercised via the integration probe in the
deepseek-v4-pro K=1 sweep, not here (network + cost).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai.types.chat import ChatCompletionMessage

from benchmark.llm_adapters import LLMConfig
from benchmark.llm_adapters.openai_compat import OpenAICompatAdapter


def _make_response(content: str, reasoning_content: str | None = None, finish_reason: str = "stop"):
    """Build a mock OpenAI-compat ChatCompletion response with optional
    reasoning_content. The OpenAI SDK uses Pydantic `extra='allow'` so
    unknown fields land in `model_extra` and are reachable via `getattr`.
    """
    msg_payload: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "refusal": None,
    }
    if reasoning_content is not None:
        msg_payload["reasoning_content"] = reasoning_content

    msg = ChatCompletionMessage.model_validate(msg_payload)
    choice = MagicMock()
    choice.finish_reason = finish_reason
    choice.message = msg

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return response


def _make_adapter() -> OpenAICompatAdapter:
    config = LLMConfig(
        name="test-llm",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        model_id="test/model",
        max_tool_rounds=2,
    )
    return OpenAICompatAdapter(config)


class _FakeMcpApp:
    """Minimal FastMCP-shaped stand-in. Tests don't actually hit MCP because
    the responses we mock have no tool_calls — adapter exits the loop after
    one round.
    """

    async def call_tool(self, name: str, args: dict) -> Any:  # pragma: no cover
        raise AssertionError("mcp call_tool should not be invoked when no tool_calls in response")


@pytest.mark.asyncio
async def test_reasoning_content_extracted_from_response():
    """Sanity: when the model emits reasoning_content, the adapter can
    read it via getattr (Pydantic extra='allow' on ChatCompletionMessage).
    """
    response = _make_response(
        content="The answer is 4.",
        reasoning_content="2 + 2 = 4 because addition is commutative.",
    )
    msg = response.choices[0].message
    assert getattr(msg, "reasoning_content", None) == "2 + 2 = 4 because addition is commutative."

    response_no = _make_response(content="OK", reasoning_content=None)
    msg_no = response_no.choices[0].message
    assert getattr(msg_no, "reasoning_content", None) is None


@pytest.mark.asyncio
async def test_reasoning_content_echoed_in_round_two_request_when_tool_calls_chained():
    """Two-round chain: round-0 response has tool_calls + reasoning_content,
    round-1 response is final. The adapter must include reasoning_content
    in the assistant entry for round-0 when constructing round-1's request.
    """
    adapter_config = LLMConfig(
        name="test-llm",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        model_id="test/model",
        max_tool_rounds=3,
    )

    # Round 0: tool call + reasoning_content
    tool_call_obj = MagicMock()
    tool_call_obj.id = "call_1"
    tool_call_obj.type = "function"
    tool_call_obj.function = MagicMock(arguments="{}")
    tool_call_obj.function.name = "list_user_spaces"

    msg0 = ChatCompletionMessage.model_validate(
        {
            "role": "assistant",
            "content": None,
            "refusal": None,
            "reasoning_content": "I should list user spaces first.",
        }
    )
    msg0_choice = MagicMock()
    msg0_choice.finish_reason = "tool_calls"
    msg0_choice.message = msg0
    object.__setattr__(msg0, "tool_calls", [tool_call_obj])

    response0 = MagicMock()
    response0.choices = [msg0_choice]
    response0.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

    # Round 1: final answer, no tool calls
    response1 = _make_response(content="Done.", reasoning_content=None, finish_reason="stop")

    fake_app = MagicMock()
    fake_app.call_tool = AsyncMock(
        return_value=MagicMock(structured_content={"result": []}, content=None)
    )

    # Capture each call's messages list at call time (deep copy) — the adapter
    # mutates the same list across rounds, so by the time we inspect
    # await_args_list, the snapshots have shifted.
    captured_calls: list[list[dict]] = []
    side_effect_responses = [response0, response1]

    async def fake_create(**kwargs):
        import copy as _copy

        captured_calls.append(_copy.deepcopy(kwargs["messages"]))
        return side_effect_responses[len(captured_calls) - 1]

    with patch("benchmark.llm_adapters.openai_compat.AsyncOpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=fake_create)
        mock_openai_cls.return_value = mock_client

        adapter = OpenAICompatAdapter(adapter_config)
        with patch(
            "benchmark.llm_adapters._tool_export.build_openai_tools",
            new=AsyncMock(return_value=[]),
        ):
            result = await adapter.dispatch(
                system_prompt="sys",
                user_prompt="usr",
                mcp_app=fake_app,
                allowed_tools=frozenset({"list_user_spaces"}),
            )

    assert len(captured_calls) == 2
    # Inspect the round-1 (second) call's messages — at that moment, only
    # round-0's assistant entry has been appended.
    sent_messages = captured_calls[1]
    assistant_entries = [m for m in sent_messages if m["role"] == "assistant"]
    assert len(assistant_entries) == 1, f"expected 1 assistant entry, got {sent_messages!r}"
    entry = assistant_entries[0]
    assert "reasoning_content" in entry, (
        f"reasoning_content missing from assistant entry: {entry!r}"
    )
    assert entry["reasoning_content"] == "I should list user spaces first."
    # Round-0's assistant entry must also carry tool_calls (the original behavior)
    assert "tool_calls" in entry
    assert entry["tool_calls"][0]["function"]["name"] == "list_user_spaces"

    assert result.error is None
    assert result.rounds_used == 2


@pytest.mark.asyncio
async def test_no_reasoning_content_means_no_echo_key():
    """Backward-compat: providers that don't emit reasoning_content must
    NOT have the key spuriously added (would confuse the API contract on
    other providers).
    """
    adapter_config = LLMConfig(
        name="test-llm",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        model_id="test/model",
        max_tool_rounds=3,
    )

    tool_call_obj = MagicMock()
    tool_call_obj.id = "call_1"
    tool_call_obj.type = "function"
    tool_call_obj.function = MagicMock(arguments="{}")
    tool_call_obj.function.name = "list_user_spaces"

    msg0 = ChatCompletionMessage.model_validate(
        {"role": "assistant", "content": None, "refusal": None}
    )
    msg0_choice = MagicMock()
    msg0_choice.finish_reason = "tool_calls"
    msg0_choice.message = msg0
    object.__setattr__(msg0, "tool_calls", [tool_call_obj])

    response0 = MagicMock()
    response0.choices = [msg0_choice]
    response0.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

    response1 = _make_response(content="Done.", reasoning_content=None, finish_reason="stop")

    fake_app = MagicMock()
    fake_app.call_tool = AsyncMock(
        return_value=MagicMock(structured_content={"result": []}, content=None)
    )

    captured_calls: list[list[dict]] = []
    side_effect_responses = [response0, response1]

    async def fake_create(**kwargs):
        import copy as _copy

        captured_calls.append(_copy.deepcopy(kwargs["messages"]))
        return side_effect_responses[len(captured_calls) - 1]

    with patch("benchmark.llm_adapters.openai_compat.AsyncOpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=fake_create)
        mock_openai_cls.return_value = mock_client

        adapter = OpenAICompatAdapter(adapter_config)
        with patch(
            "benchmark.llm_adapters._tool_export.build_openai_tools",
            new=AsyncMock(return_value=[]),
        ):
            result = await adapter.dispatch(
                system_prompt="sys",
                user_prompt="usr",
                mcp_app=fake_app,
                allowed_tools=frozenset({"list_user_spaces"}),
            )

    assert len(captured_calls) == 2
    sent_messages = captured_calls[1]
    assistant_entries = [m for m in sent_messages if m["role"] == "assistant"]
    assert len(assistant_entries) == 1
    entry = assistant_entries[0]
    assert "reasoning_content" not in entry, f"reasoning_content unexpectedly present: {entry!r}"

    assert result.error is None
