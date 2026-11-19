"""Unit tests for AnthropicApiAdapter: request shape, thinking round trip, tool round trip.

A fake client stands in for `AsyncAnthropic`; responses are real SDK `Message` objects.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage

from benchmark.llm_adapters import LLMConfig
from benchmark.llm_adapters.anthropic_api import (
    MAX_NONSTREAMING_TOKENS,
    AdapterStop,
    AnthropicApiAdapter,
    AnthropicSettings,
)
from test.unit.llm_adapters.fakes import FakeMcpApp


def message(*blocks: Any, stop: str = "end_turn", tokens: tuple[int, int] = (10, 5)) -> Message:
    return Message(
        id=f"msg_{id(blocks)}",
        type="message",
        role="assistant",
        model="claude-haiku-5-5",
        content=list(blocks),
        stop_reason=stop,
        stop_sequence=None,
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def thinking() -> ThinkingBlock:
    # display "omitted" is the default: empty text, a signature to send back.
    return ThinkingBlock(type="thinking", thinking="", signature="c2lnbmF0dXJl")


def tool_use(name: str = "echo", tool_id: str = "tu_1", **input: Any) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=tool_id, name=name, input=input)


class FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        # the adapter keeps appending to `messages`; keep what was sent at this call
        self.requests.append({**kwargs, "messages": copy.copy(kwargs["messages"])})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = FakeMessages(responses)


def adapter(
    responses: list[Any], *, effort: str = "medium", rounds: int = 40, **hooks: Any
) -> tuple[AnthropicApiAdapter, FakeClient]:
    client = FakeClient(responses)
    config = LLMConfig(
        name="haiku-medium",
        api_key="sk-test",
        model_id="claude-haiku-5-5",
        max_tokens=16000,
        max_tool_rounds=rounds,
    )
    return (
        AnthropicApiAdapter(
            config, settings=AnthropicSettings(effort=effort), client=client, **hooks
        ),
        client,
    )


async def run(ad: AnthropicApiAdapter, app: FakeMcpApp | None = None):
    return await ad.dispatch(
        system_prompt="You are an assistant.",
        user_prompt="Do the task.",
        mcp_app=app or FakeMcpApp(),
        allowed_tools=frozenset({"echo"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_request_shape(effort: str) -> None:
    ad, client = adapter([message(TextBlock(type="text", text="done"))], effort=effort)

    out = await run(ad)

    (req,) = client.messages.requests
    assert req["model"] == "claude-haiku-5-5"
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"] == {"effort": effort}
    assert req["system"] == "You are an assistant."
    assert req["max_tokens"] == 16000
    assert req["messages"] == [{"role": "user", "content": "Do the task."}]
    assert req["tools"] == [
        {
            "name": "echo",
            "description": "echo tool",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
        }
    ]
    # Haiku 5.5 and Sonnet 5.5 reject sampling fields; no forced tool use, no fallbacks.
    for absent in ("temperature", "top_p", "top_k", "tool_choice", "fallbacks", "extra_body"):
        assert absent not in req
    assert out.final_answer == "done"
    assert out.error is None


@pytest.mark.asyncio
async def test_thinking_blocks_go_back_unchanged_and_results_in_one_message() -> None:
    think = thinking()
    first = message(
        think,
        tool_use(x=1),
        tool_use(tool_id="tu_2", name="boom"),
        stop="tool_use",
        tokens=(100, 20),
    )
    second = message(thinking(), TextBlock(type="text", text="all done"), tokens=(200, 30))
    app = FakeMcpApp({"echo": lambda a: {"echoed": a}, "boom": lambda a: ValueError("bad")})
    ad, client = adapter([first, second])

    out = await ad.dispatch(
        system_prompt="s", user_prompt="u", mcp_app=app, allowed_tools=frozenset({"echo", "boom"})
    )

    second_request = client.messages.requests[1]["messages"]
    assert [m["role"] for m in second_request] == ["user", "assistant", "user"]
    # the assistant turn is the response content itself: same blocks, thinking one included
    assert second_request[1]["content"] == list(first.content)
    assert second_request[1]["content"][0] is think
    assert second_request[1]["content"][0].signature == "c2lnbmF0dXJl"
    # both tool results in a single user message, the failed one marked
    results = second_request[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]
    assert "is_error" not in results[0]
    assert results[1]["is_error"] is True
    assert [c.tool_name for c in out.tool_calls] == ["echo", "boom"]
    assert out.tool_calls[0].arguments == {"x": 1}
    assert out.tool_calls[1].succeeded is False
    assert out.usage_in_tokens == 300
    assert out.usage_out_tokens == 50
    assert out.final_answer == "all done"
    assert out.rounds_used == 2


@pytest.mark.asyncio
async def test_the_trace_is_json_and_logs_each_round_once() -> None:
    import json

    ad, _ = adapter(
        [
            message(thinking(), tool_use(x=1), stop="tool_use"),
            message(TextBlock(type="text", text="ok")),
        ]
    )

    out = await run(ad)

    rounds = json.loads(json.dumps(out.raw_trace["rounds"]))
    assert [r["round_index"] for r in rounds] == [0, 1]
    assert rounds[0]["response_content"][0]["type"] == "thinking"
    assert rounds[0]["response_content"][0]["signature"] == "c2lnbmF0dXJl"
    # round 1 logs only what is new since round 0. The assistant turn is round 0's response
    # (logged there); what is new is the user message with the tool results.
    (new,) = rounds[1]["request_new_messages"]
    assert new["role"] == "user"
    assert new["content"][0]["type"] == "tool_result"
    assert out.raw_trace["response_model"] == "claude-haiku-5-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
async def test_refusal_and_max_tokens_are_no_final_answer(stop: str) -> None:
    ad, _ = adapter([message(TextBlock(type="text", text="partial"), stop=stop)])

    out = await run(ad)

    assert out.final_answer == ""
    assert out.finish_reason == stop
    assert out.error is None


@pytest.mark.asyncio
async def test_round_limit_is_no_final_answer() -> None:
    ad, _ = adapter(
        [message(tool_use(tool_id=f"t{i}", x=i), stop="tool_use") for i in range(3)], rounds=3
    )

    out = await run(ad)

    assert out.final_answer == ""
    assert out.finish_reason == "max_rounds"
    assert out.rounds_used == 3


@pytest.mark.asyncio
async def test_usage_hook_prices_each_call_and_before_call_can_stop() -> None:
    seen: list[tuple[str, int, int]] = []

    def on_usage(name: str, response_id: str, tokens_in: int, tokens_out: int) -> float:
        seen.append((name, tokens_in, tokens_out))
        return 0.25

    stops = iter([None, "budget_exhausted"])

    def before_call() -> None:
        reason = next(stops)
        if reason:
            raise AdapterStop(reason)

    ad, client = adapter(
        [message(tool_use(x=1), stop="tool_use", tokens=(7, 3))],
        on_usage=on_usage,
        before_call=before_call,
    )

    out = await run(ad)

    assert seen == [("haiku-medium", 7, 3)]
    assert out.raw_trace["cost_usd"] == 0.25
    assert out.finish_reason == "budget_exhausted"
    assert len(client.messages.requests) == 1  # no second call after the stop
    assert out.error is None


@pytest.mark.asyncio
async def test_a_client_error_is_an_adapter_error_keeping_the_calls_so_far() -> None:
    ad, _ = adapter([message(tool_use(x=1), stop="tool_use"), RuntimeError("upstream 500")])

    out = await run(ad)

    assert out.finish_reason == "adapter_error"
    assert out.error == "RuntimeError: upstream 500"
    assert len(out.tool_calls) == 1


def test_a_max_tokens_the_sdk_would_refuse_to_send_is_rejected_up_front() -> None:
    with pytest.raises(ValueError, match="non-streaming"):
        AnthropicApiAdapter(
            LLMConfig(name="x", model_id="m", max_tokens=MAX_NONSTREAMING_TOKENS + 1),
            client=FakeClient([]),
        )


def test_effort_must_be_a_known_level() -> None:
    with pytest.raises(ValueError, match="effort"):
        AnthropicSettings(effort="ultra")


def test_extra_body_carries_a_field_the_sdk_lacks() -> None:
    ad, _ = adapter([])
    ad2 = AnthropicApiAdapter(
        ad.config,
        settings=AnthropicSettings(extra_body={"some_new_field": 1}),
        client=FakeClient([]),
    )
    params = ad2.request_params(system_prompt="s", tools=[])
    assert params["extra_body"] == {"some_new_field": 1}


def test_the_config_repr_does_not_carry_the_key() -> None:
    cfg = LLMConfig(name="x", api_key="sk-super-secret", model_id="m")
    assert "sk-super-secret" not in repr(cfg)
