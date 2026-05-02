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
        # PPAM 2026 paper panel: deliberate 4-model selection.
        #   - Qwen3.6-35B (Cyfronet-Forge OSS, top performer 17/18)
        #   - GLM-4.7-Flash (Cyfronet-Forge OSS, weaker; included to show
        #     where the harness exposes capability gaps)
        # Plus Claude Sonnet 4.5 + DeepSeek-V3 via separate endpoints below.
        # Mistral-Small-4-119B candidate to be added when access is
        # confirmed.
        #
        # Earlier-tested-and-dropped Forge models (probed 2026-05-02):
        #   - Qwen/Qwen3-Coder-30B-A3B-Instruct   weaker than Qwen3.6
        #   - Qwen/QwQ-32B                         flaky, RESET-prone
        #   - meta-llama/Llama-3.3-70B-Instruct    inactive on Forge
        #   - speakleash/Bielik-11B-v3             0/4 in 4-scenario probe
        #   - CYFRAGOVPL/Llama-PLLuM-70B           0/4 + lost FC support
        #   - CYFRAGOVPL/pllum-12b-nc-chat-250715  non-FC + Polish-default
        #   - speakleash/Bielik-11B-v2.6           non-FC
        for name, model_id in (
            ("qwen3.6-35b", "Qwen/Qwen3.6-35B-A3B"),
            ("glm-4.7-flash", "zai-org/GLM-4.7-Flash"),
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

    # OpenRouter — additional non-Cyfronet OSS legs. Currently used to
    # access DeepSeek-V3 which isn't on Forge. Same OpenAI-compat adapter
    # as Forge; only the endpoint URL + key differ. Cost: cents per K=1
    # sweep at OpenRouter's DeepSeek pricing.
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    openrouter_base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    if openrouter_key:
        for name, model_id in (("deepseek-v3", "deepseek/deepseek-chat-v3-0324"),):
            panel.append(
                PanelEntry(
                    name=name,
                    config=LLMConfig(
                        name=name,
                        api_base=openrouter_base,
                        api_key=openrouter_key,
                        model_id=model_id,
                    ),
                    adapter_factory=OpenAICompatAdapter,
                )
            )
    else:
        skipped.append("OpenRouter legs (DeepSeek-V3): OPENROUTER_API_KEY missing in .env")

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
