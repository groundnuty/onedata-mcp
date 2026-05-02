"""LLMAdapter protocol — the uniform interface every per-LLM adapter
implements. Trial loop dispatches through this; each adapter handles
its own SDK-specific tool-call envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMConfig:
    """Per-LLM configuration. Held by the adapter at construction time."""

    name: str  # 'claude-sonnet-4-5', 'llama-3.3-70b', 'qwen-3-32b' etc.
    api_base: str | None = None  # OpenAI-compat: base URL; Anthropic: None
    api_key: str | None = None
    model_id: str = ""  # the specific model identifier passed to the SDK
    temperature: float = 1.0
    # Generous cap to keep reasoning-style models (QwQ-32B chain-of-thought,
    # Qwen3.6-35B thinking mode) from being truncated mid-answer. Forge
    # quota is per-token billed but the user has plenty; better to spend
    # tokens than to have trials fail for a trivial cap. Bumped 2026-05-02
    # from 4096 after QwQ-32B 0/4 panel run with empty answers.
    max_tokens: int = 32768
    # Bumped 2026-05-02 (was 15): Claude was hitting `error_max_turns` on P3
    # before it could narrate a final answer (rule-add + multi-poll work
    # genuinely needs more turns than terse scenarios). See artefact
    # 20260502T033713 for the worked example. 40 turns gives slack for
    # multi-tool scenarios without enabling indefinite loops.
    max_tool_rounds: int = 40
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrialDispatch:
    """Result of dispatching one scenario brief through one LLM.

    The trial loop wraps this with OracleResult to produce a
    per-(LLM, scenario, trial) record.
    """

    final_answer: str
    tool_calls: tuple  # tuple[ToolCall, ...] — keeping loose to avoid circular import
    rounds_used: int
    finish_reason: str | None
    usage_in_tokens: int = 0
    usage_out_tokens: int = 0
    wall_clock_seconds: float = 0.0
    raw_trace: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class LLMAdapter(Protocol):
    """The contract trial-runner uses to dispatch a brief through an LLM.

    Implementations:
    - benchmark.llm_adapters.openai_compat.OpenAICompatAdapter — Forge,
      AGH-vLLM, OpenRouter, anything OpenAI-compat
    - benchmark.llm_adapters.anthropic_native.AnthropicAdapter (planned)
      — Claude via the Anthropic Python SDK

    The adapter's responsibility:
    - Translate the agent's brief into the SDK's request shape
    - Execute the agent loop (model call → tool call → result → repeat)
    - Dispatch each tool call back through `mcp_app.call_tool(...)` and
      capture the result string into ToolCall.result
    - Track token usage, rounds, wall-clock time
    """

    config: LLMConfig

    async def dispatch(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        mcp_app,  # FastMCP — kept untyped to avoid circular import
        allowed_tools: frozenset[str],
    ) -> TrialDispatch:
        """Run one trial: hand brief to LLM, dispatch tool calls via mcp_app,
        capture trace + final answer."""
        ...
