"""LLM adapters for the multi-LLM benchmark panel.

The panel runs each scenario against multiple LLMs in turn. Adapters
implement a uniform protocol so the trial loop is LLM-agnostic; each
adapter handles its own SDK quirks (OpenAI's tool_calls envelope vs
Anthropic's tool_use blocks vs whatever).

See research/02-claude-code-with-non-anthropic-models/04-community-pain-points.md
for the categories of translation drift that motivated keeping each
SDK's native tool-call protocol native rather than translating
through a single intermediate.
"""

from __future__ import annotations

from benchmark.llm_adapters._protocol import LLMAdapter, LLMConfig, TrialDispatch
from benchmark.llm_adapters.claude_agent_sdk import ClaudeAgentSdkAdapter
from benchmark.llm_adapters.openai_compat import OpenAICompatAdapter

__all__ = [
    "ClaudeAgentSdkAdapter",
    "LLMAdapter",
    "LLMConfig",
    "OpenAICompatAdapter",
    "TrialDispatch",
]
