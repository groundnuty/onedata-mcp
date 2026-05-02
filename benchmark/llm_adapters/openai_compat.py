"""OpenAI-compatible adapter — drives any provider that speaks the OpenAI
chat-completions + function-calling protocol.

Used for:
- PLGrid Forge (Llama 3.3 70B, Qwen 3 32B / Qwen 3.5 122B)
- AGH-hosted vLLM (Llama, Qwen)
- OpenRouter (when routing to any OpenAI-compat upstream)
- Future GLM-4.7-Flash / DeepSeek / etc. via Forge

Pattern adapted from M0rgho's existing test/plgrid/forge_harness.py with
two extensions: (a) captures tool_call results into ToolCall.result so
the oracle has the agent's full view; (b) factors the agent loop into
something the trial-runner can call uniformly across LLMs.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from openai import AsyncOpenAI

from benchmark._runtime_types import ToolCall
from benchmark.llm_adapters._protocol import LLMConfig, TrialDispatch

# Truncate per-tool-call results captured into ToolCall.result. The full
# result still drives the agent's next turn; this cap is just for the
# trace artefact so oracles + post-hoc analysis don't OOM on huge files.
TOOL_RESULT_CAP_BYTES = 8 * 1024


class OpenAICompatAdapter:
    """Adapter implementing the LLMAdapter protocol over OpenAI-compat endpoints."""

    def __init__(self, config: LLMConfig) -> None:
        if not config.api_base:
            raise ValueError(f"OpenAI-compat adapter requires api_base; got {config!r}")
        self.config = config
        self._client = AsyncOpenAI(api_key=config.api_key, base_url=config.api_base)

    async def dispatch(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        mcp_app: FastMCP,
        allowed_tools: frozenset[str],
    ) -> TrialDispatch:
        from benchmark.llm_adapters._tool_export import build_openai_tools

        openai_tools = await build_openai_tools(mcp_app, allowed_tools)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        loop_t0 = time.perf_counter()
        captured_calls: list[ToolCall] = []
        usage_in = 0
        usage_out = 0
        finish_reason: str | None = None
        final_text: str | None = None
        rounds_log: list[dict[str, Any]] = []

        # Per-LLM extras forwarded to the OpenAI-compat endpoint as
        # `extra_body`. Used for OpenRouter's `provider` routing block
        # (pin to a specific upstream provider) and any other vendor-
        # specific knobs configured per LLM via `LLMConfig.extra`.
        extra_body = self.config.extra or None

        for round_ix in range(self.config.max_tool_rounds):
            messages_pre = copy.deepcopy(messages)
            try:
                response = await self._client.chat.completions.create(
                    model=self.config.model_id,
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto",
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    extra_body=extra_body,
                )
            except Exception as e:  # noqa: BLE001
                return TrialDispatch(
                    final_answer="",
                    tool_calls=tuple(captured_calls),
                    rounds_used=round_ix,
                    finish_reason="adapter_error",
                    usage_in_tokens=usage_in,
                    usage_out_tokens=usage_out,
                    wall_clock_seconds=time.perf_counter() - loop_t0,
                    raw_trace={"rounds": rounds_log, "error_round": round_ix},
                    error=f"{type(e).__name__}: {e}",
                )

            usage = getattr(response, "usage", None)
            if usage:
                usage_in += getattr(usage, "prompt_tokens", 0) or 0
                usage_out += getattr(usage, "completion_tokens", 0) or 0

            choice = response.choices[0]
            finish_reason = choice.finish_reason
            msg = choice.message
            tool_calls = list(msg.tool_calls or [])

            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or None,
            }
            if tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": getattr(tc, "type", "function") or "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_entry)

            rounds_log.append(
                {
                    "round_index": round_ix,
                    "messages_pre_request": messages_pre,
                    "tool_calls_emitted": [tc.function.name for tc in tool_calls],
                    "finish_reason": finish_reason,
                    "content_text_length": len(msg.content or ""),
                }
            )

            if not tool_calls:
                final_text = msg.content
                break

            for tc in tool_calls:
                name = tc.function.name
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                    if not isinstance(arguments, dict):
                        arguments = {}
                except json.JSONDecodeError:
                    arguments = {}

                ok, content_text, error = await _dispatch_mcp(mcp_app, name, arguments)
                captured_calls.append(
                    ToolCall(
                        tool_name=name,
                        arguments=arguments,
                        succeeded=ok,
                        error=error,
                        result=content_text[:TOOL_RESULT_CAP_BYTES] if content_text else None,
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content_text,
                    }
                )
        else:
            final_text = None  # max_tool_rounds reached without final answer

        return TrialDispatch(
            final_answer=final_text or "",
            tool_calls=tuple(captured_calls),
            rounds_used=len(rounds_log),
            finish_reason=finish_reason,
            usage_in_tokens=usage_in,
            usage_out_tokens=usage_out,
            wall_clock_seconds=time.perf_counter() - loop_t0,
            raw_trace={"rounds": rounds_log},
            error=None,
        )


async def _dispatch_mcp(
    app: FastMCP, tool_name: str, arguments: dict[str, Any]
) -> tuple[bool, str, str | None]:
    """Invoke a tool through the MCP server, return (ok, content, error)."""
    try:
        result = await app.call_tool(tool_name, arguments)
        return True, _result_to_text(result), None
    except ToolError as e:
        err = str(e)
        return False, json.dumps({"error": err}, ensure_ascii=False), err
    except Exception as e:  # noqa: BLE001
        err = str(e)
        return False, json.dumps({"error": err}, ensure_ascii=False), err


def _result_to_text(result: Any) -> str:
    """Best-effort serialisation of FastMCP tool result → string."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        if isinstance(structured, dict) and "result" in structured and len(structured) == 1:
            try:
                return json.dumps(structured["result"], ensure_ascii=False)
            except (TypeError, ValueError):
                return repr(structured["result"])
        try:
            return json.dumps(structured, ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(structured)
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            return text
    return repr(result)
