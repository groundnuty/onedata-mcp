"""Anthropic Messages-API adapter: a minimal tool loop for the K2 validation panel.

Used for the Haiku effort sweep and the Sonnet control (`design/08`). Written against the
SDK's own parameter names, checked in the installed source (anthropic 2.10):

- `thinking={"type": "adaptive"}`           (`ThinkingConfigAdaptiveParam`)
- `output_config={"effort": "low"|"medium"|"high"|"xhigh"|"max"}` (`OutputConfigParam`)
- no `temperature`, `top_p` or `top_k`: Haiku 5.5 and Sonnet 5.5 reject non-default values.
- no `tool_choice` and no `fallbacks`: the loop is plain `auto`.

If a future SDK lacks a field, `AnthropicSettings.extra_body` carries it through the SDK's
`extra_body` request option.

The loop:

- sends the tool results of one turn back in ONE user message, with `is_error` on a failed call;
- appends `response.content` to the history unchanged. That includes `thinking` blocks, even
  with empty text (the default is `display: omitted`, and the block keeps its `signature`):
  the API requires them back as they were;
- reads blocks by type, never by position;
- treats a `refusal` or `max_tokens` stop as "no final answer" (the trial is a NO_RESULT).

The adapter never sees a price: the runner passes `on_usage`, which returns the cost of one
call, and `before_call`, which may raise `AdapterStop` to end the trial before the next call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP

from benchmark._runtime_types import ToolCall
from benchmark.llm_adapters._protocol import LLMConfig, TrialDispatch
from benchmark.llm_adapters._tool_export import build_anthropic_tools
from benchmark.llm_adapters.openai_compat import (
    TOOL_RESULT_CAP_BYTES,
    _dispatch_mcp,
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Non-streaming requests need `max_tokens` small enough for the SDK's 10 minute rule
# (`_calculate_nonstreaming_timeout`: expected time = 3600 s * max_tokens / 128000 <= 600 s,
# so at most 21333). A larger value raises before any request is made.
MAX_NONSTREAMING_TOKENS = 21_333


class AdapterStop(Exception):
    """Raised by `before_call` to end the trial before the next API call."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AnthropicSettings:
    effort: str = "medium"
    thinking: dict[str, Any] = field(default_factory=lambda: {"type": "adaptive"})
    extra_body: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {EFFORT_LEVELS}; got {self.effort!r}")


# on_usage(model_name, response_id, prompt_tokens, output_tokens) -> cost in USD
UsageHook = Callable[[str, str, int, int], float]


def jsonable(value: Any) -> Any:
    """A plain-JSON form of an SDK block (or a dict, list, scalar), for the trace."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


def _block_type(block: Any) -> str | None:
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


class AnthropicApiAdapter:
    """Adapter implementing the LLMAdapter protocol over the Anthropic Messages API."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        settings: AnthropicSettings | None = None,
        client: Any = None,
        on_usage: UsageHook | None = None,
        before_call: Callable[[], None] | None = None,
    ) -> None:
        if config.max_tokens > MAX_NONSTREAMING_TOKENS:
            raise ValueError(
                f"max_tokens={config.max_tokens} is too large for a non-streaming request "
                f"(at most {MAX_NONSTREAMING_TOKENS}); use about 16000"
            )
        self.config = config
        self.settings = settings or AnthropicSettings()
        self._on_usage = on_usage
        self._before_call = before_call
        if client is None:
            from anthropic import AsyncAnthropic  # dev dependency; the server never imports it

            client = AsyncAnthropic(api_key=config.api_key)
        self._client = client

    def request_params(self, *, system_prompt: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        """The fixed part of every request of a trial (the messages are added per call)."""
        params: dict[str, Any] = {
            "model": self.config.model_id,
            "max_tokens": self.config.max_tokens,
            "system": system_prompt,
            "tools": tools,
            "thinking": dict(self.settings.thinking),
            "output_config": {"effort": self.settings.effort},
        }
        if self.settings.extra_body:
            params["extra_body"] = dict(self.settings.extra_body)
        return params

    async def dispatch(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        mcp_app: FastMCP,
        allowed_tools: frozenset[str],
    ) -> TrialDispatch:
        tools = await build_anthropic_tools(mcp_app, allowed_tools)
        fixed = self.request_params(system_prompt=system_prompt, tools=tools)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]

        t0 = time.perf_counter()
        captured: list[ToolCall] = []
        rounds: list[dict[str, Any]] = []
        usage_in = usage_out = 0
        cost = 0.0
        finish_reason: str | None = None
        final_text: str | None = None
        sent = 0  # messages already logged in an earlier round
        response_model: str | None = None
        request_ids: list[str] = []

        def result(error: str | None = None, reason: str | None = None) -> TrialDispatch:
            return TrialDispatch(
                final_answer=final_text or "",
                tool_calls=tuple(captured),
                rounds_used=len(rounds),
                finish_reason=reason or finish_reason,
                usage_in_tokens=usage_in,
                usage_out_tokens=usage_out,
                wall_clock_seconds=time.perf_counter() - t0,
                raw_trace={
                    "rounds": rounds,
                    "cost_usd": cost,
                    "response_model": response_model,
                    "request_ids": request_ids,
                },
                error=error,
            )

        for round_ix in range(self.config.max_tool_rounds):
            if self._before_call is not None:
                try:
                    self._before_call()
                except AdapterStop as stop:
                    return result(reason=stop.reason)
            try:
                response = await self._client.messages.create(**fixed, messages=messages)
            except Exception as e:  # noqa: BLE001 - the runner masks secrets in the text
                return result(error=f"{type(e).__name__}: {e}", reason="adapter_error")

            usage = getattr(response, "usage", None)
            in_tokens = (
                (getattr(usage, "input_tokens", 0) or 0)
                + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            )
            out_tokens = getattr(usage, "output_tokens", 0) or 0
            usage_in += in_tokens
            usage_out += out_tokens
            response_id = getattr(response, "id", "") or ""
            request_ids.append(response_id)
            response_model = getattr(response, "model", None) or response_model
            call_cost = 0.0
            if self._on_usage is not None:
                call_cost = self._on_usage(self.config.name, response_id, in_tokens, out_tokens)
                cost += call_cost

            stop_reason = getattr(response, "stop_reason", None)
            finish_reason = stop_reason
            blocks = list(response.content or [])
            rounds.append(
                {
                    "round_index": round_ix,
                    "request_new_messages": jsonable(messages[sent:]),
                    "response_content": jsonable(blocks),
                    "stop_reason": stop_reason,
                    "stop_details": jsonable(getattr(response, "stop_details", None)),
                    "usage": {"prompt_tokens": in_tokens, "output_tokens": out_tokens},
                    "cost_usd": call_cost,
                    "response_id": response_id,
                }
            )
            # The assistant turn goes back exactly as received, thinking blocks included.
            messages.append({"role": "assistant", "content": blocks})
            sent = len(messages)

            if stop_reason in ("refusal", "max_tokens"):
                return result(reason=stop_reason)  # no final answer

            tool_uses = [b for b in blocks if _block_type(b) == "tool_use"]
            if not tool_uses:
                if stop_reason == "pause_turn":
                    continue  # nothing to answer; ask again
                texts = [getattr(b, "text", "") for b in blocks if _block_type(b) == "text"]
                final_text = "".join(texts)
                return result()

            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                arguments = block.input if isinstance(block.input, dict) else {}
                ok, content_text, error = await _dispatch_mcp(mcp_app, block.name, arguments)
                captured.append(
                    ToolCall(
                        tool_name=block.name,
                        arguments=arguments,
                        succeeded=ok,
                        error=error,
                        result=content_text[:TOOL_RESULT_CAP_BYTES] if content_text else None,
                    )
                )
                item: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content_text,
                }
                if not ok:
                    item["is_error"] = True
                tool_results.append(item)
            messages.append({"role": "user", "content": tool_results})

        return result(reason="max_rounds")  # the loop ran out of rounds: no final answer
