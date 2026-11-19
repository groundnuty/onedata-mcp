"""OpenAICompatAdapter: the sampling and extra fields of the K2 validation panel.

glm: temperature + top_p. qwen36: temperature only. qwencoder: temperature + top_p, with
top_k and repetition_penalty in `extra_body`. A config that sets none of the new fields sends
the request it always did.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import ChatCompletionMessage

from benchmark.llm_adapters import LLMConfig
from benchmark.llm_adapters.openai_compat import OpenAICompatAdapter
from test.unit.llm_adapters.fakes import FakeMcpApp


def completion(message: dict[str, Any], finish: str = "stop") -> SimpleNamespace:
    msg = ChatCompletionMessage.model_validate({"role": "assistant", "refusal": None, **message})
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=msg)],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
    )


class FakeCompletions:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def build(config: LLMConfig, responses: list[SimpleNamespace]) -> tuple[OpenAICompatAdapter, Any]:
    adapter = OpenAICompatAdapter(config)
    fake = FakeCompletions(responses)
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return adapter, fake


async def run(adapter: OpenAICompatAdapter):
    return await adapter.dispatch(
        system_prompt="s",
        user_prompt="u",
        mcp_app=FakeMcpApp(),
        allowed_tools=frozenset({"echo"}),
    )


def config(**kw: Any) -> LLMConfig:
    return LLMConfig(
        name="m",
        api_base="https://llm.example/v1",
        api_key="lk-secret",
        model_id="vendor/model",
        **kw,
    )


@pytest.mark.asyncio
async def test_glm_sends_temperature_and_top_p_and_nothing_else() -> None:
    adapter, fake = build(config(temperature=1.0, top_p=0.95), [completion({"content": "ok"})])

    await run(adapter)

    (req,) = fake.requests
    assert req["temperature"] == 1.0
    assert req["top_p"] == 0.95
    assert req["extra_body"] is None
    assert "reasoning_effort" not in req


@pytest.mark.asyncio
async def test_qwen36_sends_temperature_only() -> None:
    adapter, fake = build(config(temperature=1.0), [completion({"content": "ok"})])

    await run(adapter)

    (req,) = fake.requests
    assert req["temperature"] == 1.0
    assert "top_p" not in req
    assert req["extra_body"] is None


@pytest.mark.asyncio
async def test_qwencoder_puts_top_k_and_repetition_penalty_in_extra_body() -> None:
    adapter, fake = build(
        config(
            temperature=0.7,
            top_p=0.8,
            extra={"top_k": 20, "repetition_penalty": 1.05},
        ),
        [completion({"content": "ok"})],
    )

    await run(adapter)

    (req,) = fake.requests
    assert (req["temperature"], req["top_p"]) == (0.7, 0.8)
    assert req["extra_body"] == {"top_k": 20, "repetition_penalty": 1.05}
    assert "top_k" not in req
    assert "repetition_penalty" not in req


@pytest.mark.asyncio
async def test_a_tool_call_round_trip_echoes_reasoning_content() -> None:
    call = completion(
        {
            "content": None,
            "reasoning_content": "thinking about it",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"x": 1}'},
                }
            ],
        },
        finish="tool_calls",
    )
    adapter, fake = build(config(), [call, completion({"content": "done"})])

    out = await run(adapter)

    second = fake.requests[1]["messages"]
    assistant = second[2]
    assert assistant["reasoning_content"] == "thinking about it"
    assert assistant["tool_calls"][0]["function"]["name"] == "echo"
    assert second[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"echoed": {"x": 1}}'}
    assert out.final_answer == "done"
    assert out.tool_calls[0].arguments == {"x": 1}


def test_an_adapter_error_does_not_show_the_key() -> None:
    with pytest.raises(ValueError) as err:
        OpenAICompatAdapter(LLMConfig(name="m", api_key="lk-secret", model_id="x"))
    assert "lk-secret" not in str(err.value)


@pytest.mark.asyncio
async def test_the_serving_fingerprint_is_kept_off_the_trace_but_on_the_adapter() -> None:
    first = completion({"content": "ok"})
    first.system_fingerprint = "fp_123"
    adapter, _ = build(config(), [first])

    out = await run(adapter)

    assert adapter.fingerprints == ["fp_123"]
    assert "fingerprint" not in json.dumps(out.raw_trace)
