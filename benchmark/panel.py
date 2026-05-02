"""LLM panel: which models the benchmark runs against.

A `PanelEntry` is an LLMConfig + the adapter class that consumes it. Models
whose credentials are missing from `.env` are silently omitted from the
panel — the trial runner only sees activated models, and the run-panel
script reports which legs were skipped.

Convention:
- `PLGRID_FORGE_API_KEY` + `PLGRID_FORGE_BASE_URL` → Forge models
  (Llama 3.3 70B, Qwen 3 Coder 30B, etc.) — uses `OpenAICompatAdapter`.
- Claude leg → uses `claude-agent-sdk` Python package, which authenticates
  via the local Claude Code session (no API key). The leg activates iff
  the `claude` binary is reachable. To opt out, set
  `BENCHMARK_DISABLE_CLAUDE=1`.
- `OPENAI_API_KEY` → GPT models (not currently in the panel).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from benchmark.llm_adapters import (
    ClaudeAgentSdkAdapter,
    LLMAdapter,
    LLMConfig,
    OpenAICompatAdapter,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_FORGE_BASE_URL = "https://llmlab.plgrid.pl/api/v1"


@dataclass(frozen=True)
class PanelEntry:
    """One LLM in the panel: name + config + adapter factory.

    The adapter factory is held as a callable rather than an already-built
    adapter so the trial runner can construct one per trial if it ever
    wants per-trial isolation (today it builds once and reuses).
    """

    name: str
    config: LLMConfig
    adapter_factory: Callable[[LLMConfig], LLMAdapter]

    def build(self) -> LLMAdapter:
        return self.adapter_factory(self.config)


def build_panel() -> tuple[tuple[PanelEntry, ...], tuple[str, ...]]:
    """Construct the canonical PPAM 2026 panel.

    Returns `(panel, skipped_reasons)` — the second element is a tuple of
    human-readable reasons why specific legs were omitted, so the run-panel
    script can surface them in the run summary.
    """
    load_dotenv(REPO_ROOT / ".env")

    panel: list[PanelEntry] = []
    skipped: list[str] = []

    forge_key = os.getenv("PLGRID_FORGE_API_KEY", "").strip()
    forge_base = os.getenv("PLGRID_FORGE_BASE_URL", DEFAULT_FORGE_BASE_URL).rstrip("/")

    if forge_key:
        # Forge model classification probed 2026-05-02. Only models with the
        # `FC` (function-calling) tag in the Forge web UI are panel-eligible
        # — non-FC models reject `tool_choice="auto"` with a BadRequestError
        # at the API boundary. The tag is NOT exposed via `/v1/models`, so
        # this list must be maintained empirically. See guide.plgrid.pl
        # (Polish doc) for the FC tag definition.
        #
        # Excluded as non-FC:
        #   - CYFRAGOVPL/pllum-12b-nc-chat-250715
        #   - speakleash/Bielik-11B-v2.6-Instruct
        #
        # Excluded as benchmark-unsuitable (probed 2026-05-02):
        #   - speakleash/Bielik-11B-v3.0-Instruct  — rejects max_tokens=32k+
        #     (server-side cap below our budget); 0/4 PASS at any cap
        #   - CYFRAGOVPL/Llama-PLLuM-70B-chat-250801 — 0/4 PASS even with
        #     32k tokens; tool-use weakness, not a token issue
        #
        # Inactive (Forge says "supported but not loaded"):
        #   - meta-llama/Llama-3.3-70B-Instruct
        for name, model_id in (
            ("glm-4.7-flash", "zai-org/GLM-4.7-Flash"),
            ("qwen3-coder-30b", "Qwen/Qwen3-Coder-30B-A3B-Instruct"),
            ("qwen3.6-35b", "Qwen/Qwen3.6-35B-A3B"),
            ("qwq-32b", "Qwen/QwQ-32B"),
        ):
            panel.append(
                PanelEntry(
                    name=name,
                    config=LLMConfig(
                        name=name,
                        api_base=forge_base,
                        api_key=forge_key,
                        model_id=model_id,
                    ),
                    adapter_factory=OpenAICompatAdapter,
                )
            )
    else:
        skipped.append("PLGrid Forge legs (Llama, Qwen): PLGRID_FORGE_API_KEY missing in .env")

    if os.getenv("BENCHMARK_DISABLE_CLAUDE", "").strip():
        skipped.append("Claude leg: disabled via BENCHMARK_DISABLE_CLAUDE")
    elif shutil.which("claude") is None:
        skipped.append(
            "Claude leg: `claude` binary not on PATH — install Claude Code "
            "(https://claude.ai/install.sh) to activate this leg."
        )
    elif shutil.which("onedata-mcp") is None:
        skipped.append(
            "Claude leg: `onedata-mcp` binary not on PATH — run "
            "`uv pip install -e .` from the MCP fork repo so the SDK can "
            "spawn it as the MCP transport."
        )
    else:
        # Claude Agent SDK uses local session auth — no API key needed.
        panel.append(
            PanelEntry(
                name="claude-sonnet-4-5",
                config=LLMConfig(
                    name="claude-sonnet-4-5",
                    api_base=None,
                    api_key=None,
                    model_id="claude-sonnet-4-5-20250929",
                ),
                adapter_factory=ClaudeAgentSdkAdapter,
            )
        )

    return tuple(panel), tuple(skipped)
