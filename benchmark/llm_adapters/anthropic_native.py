"""Anthropic-native adapter — STUB.

Activates when ANTHROPIC_API_KEY is set in the environment. Until then,
imports this module raise a clear NotImplementedError that the
trial-runner can catch and skip the Claude leg.

The Claude leg of the panel is not gated on us — it's gated on the
operator providing an Anthropic API key (Pro/Max OAuth tokens are
server-side blocked from the SDK since 2026-01-09 per
research/02-claude-code-with-non-anthropic-models/02-native-config-and-sdk.md).

When the key arrives, fill out the dispatch loop following the OpenAI
adapter's structure, but using the Anthropic SDK's tool_use / tool_result
content blocks instead of OpenAI's tool_calls envelope. Notably:
- Anthropic responses arrive as a list of content blocks with type
  'text' or 'tool_use'
- Tool results are sent back as content blocks with type 'tool_result'
  inside a user-role message, NOT as a separate role
- Token usage is on `response.usage.input_tokens` / `output_tokens`
- finish_reason is `stop_reason` ('end_turn' / 'tool_use' / etc)
"""

from __future__ import annotations

from benchmark.llm_adapters._protocol import LLMConfig


class AnthropicAdapter:
    """Anthropic SDK adapter (stub).

    Raises on dispatch until ANTHROPIC_API_KEY + the real implementation
    land. The trial runner catches the exception and records the trial as
    SKIP rather than FAIL, so the rest of the panel still produces results.
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    async def dispatch(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        mcp_app,
        allowed_tools,
    ):
        raise NotImplementedError(
            "AnthropicAdapter not yet implemented. Provide ANTHROPIC_API_KEY "
            "and request the implementation. Per research/02-claude-code-with-"
            "non-anthropic-models/02-native-config-and-sdk.md, OAuth/Pro/Max "
            "subscription tokens cannot be used here — only paid Anthropic API keys, "
            "AWS Bedrock, GCP Vertex, or OpenRouter."
        )
