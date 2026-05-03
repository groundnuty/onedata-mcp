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
        # OpenRouter enforces per-request `max_tokens` against the user's
        # credit balance — requesting our 32k default returns HTTP 402
        # ("This request requires more credits, or fewer max_tokens"). We
        # cap OpenRouter-served LLMs at 4096; the working K=1 sweep used
        # ~86 output tokens per trial on average, so 4k is ample headroom.
        #
        # Swapped V3 → V4-pro 2026-05-02. V4-pro is the current generation:
        # 1.6T total / 49B active MoE, 1M context, plain MIT license.
        # Same DeepSeek family + same open-weights story.
        #
        # Provider routing: V4-pro probe 2026-05-02 — DeepInfra does NOT
        # host V4-pro yet (DeepInfra works for V4-flash only). Working
        # providers for V4-pro: SiliconFlow, Novita, Together. Picked
        # SiliconFlow (mature, serves DeepSeek's own China-region
        # deployments). `allow_fallbacks=false` makes the leg fail loud
        # if SiliconFlow is briefly unavailable rather than routing to
        # a quality-variant provider (Novita gave French gibberish on a
        # V3 trial earlier this session).
        for name, model_id, provider in (
            ("deepseek-v4-pro", "deepseek/deepseek-v4-pro", "SiliconFlow"),
        ):
            panel.append(
                PanelEntry(
                    name=name,
                    config=LLMConfig(
                        name=name,
                        api_base=openrouter_base,
                        api_key=openrouter_key,
                        model_id=model_id,
                        max_tokens=4096,
                        extra={
                            "provider": {
                                "order": [provider],
                                "allow_fallbacks": False,
                            }
                        },
                    ),
                    adapter_factory=OpenAICompatAdapter,
                )
            )
    else:
        skipped.append("OpenRouter legs (DeepSeek-V3): OPENROUTER_API_KEY missing in .env")

    # Local vLLM endpoints — for OSS models served on the operator's
    # machine rather than via Cyfronet Forge / OpenRouter. Multi-tenant:
    # each leg is configured by an indexed env-var block. The legacy
    # un-indexed form (LOCAL_VLLM_BASE_URL etc.) is treated as index 1
    # for backward compat.
    #
    #   LOCAL_VLLM_<N>_BASE_URL   e.g. http://localhost:8002/v1
    #   LOCAL_VLLM_<N>_MODEL_ID   e.g. gemma  (alias the server returns)
    #   LOCAL_VLLM_<N>_NAME       panel-side display name
    #                              (default: MODEL_ID)
    #   LOCAL_VLLM_<N>_API_KEY    optional; defaults to "vllm-local"
    #   LOCAL_VLLM_<N>_MAX_TOKENS optional; defaults to 4096 (vLLM
    #                              enforces prompt+max_tokens<=ctx, and
    #                              the LLMConfig 32k default collides
    #                              with 32K-context models like Gemma-4
    #                              and Granite-4.1)
    #
    # Currently configured: Gemma-4-31B-it (1, port 8002) and
    # Granite-4.1-30B-bf16 (2, port 8003). Both Apache-2.0, both
    # released April 2026.
    def _local_vllm_leg(idx: str, *, legacy: bool = False) -> None:
        prefix = "LOCAL_VLLM" if legacy else f"LOCAL_VLLM_{idx}"
        base = os.getenv(f"{prefix}_BASE_URL", "").strip().rstrip("/")
        model_id = os.getenv(f"{prefix}_MODEL_ID", "").strip()
        if not base and not model_id:
            return
        if not (base and model_id):
            skipped.append(
                f"Local vLLM leg {prefix}: need BOTH _BASE_URL and _MODEL_ID set in .env"
            )
            return
        name = os.getenv(f"{prefix}_NAME", "").strip() or model_id
        max_tokens = int(os.getenv(f"{prefix}_MAX_TOKENS", "").strip() or "4096")
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip() or "vllm-local"
        panel.append(
            PanelEntry(
                name=name,
                config=LLMConfig(
                    name=name,
                    api_base=base,
                    api_key=api_key,
                    model_id=model_id,
                    max_tokens=max_tokens,
                ),
                adapter_factory=OpenAICompatAdapter,
            )
        )

    # Legacy un-indexed form, still honoured.
    _local_vllm_leg("1", legacy=True)
    # Indexed forms — extend as more local LLMs come online.
    for idx in ("1", "2", "3", "4"):
        _local_vllm_leg(idx)

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
