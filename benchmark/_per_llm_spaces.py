"""Per-LLM space registry — populated by `setup_per_llm_spaces.py`.

Maps panel-LLM names to the dedicated Onedata spaces they own.
Each space is supported by both `cloud-pl` and `Cloud-SK` providers
(once provider-side support is configured manually after creation).

Created 2026-05-02 to enable LLM-level parallelism — different LLMs
running the same scenario concurrently no longer corrupt each other's
fixtures because they're in disjoint spaces. See
`research/empirical-mcp-server-findings.md` for the design rationale.

To add a new model: extend `benchmark/panel.py` then run
`uv run python -m benchmark.setup_per_llm_spaces` to create + register.
The setup script appends to this file (or you update by hand).
"""

from __future__ import annotations

# spaceId per LLM — the canonical names the harness uses.
PER_LLM_SPACE: dict[str, str] = {
    "claude-sonnet-4-5": "ppam_2026_mcp_tests_claude_sonnet_4_5",
    "qwen3.6-35b": "ppam_2026_mcp_tests_qwen3_6_35b",
    "glm-4.7-flash": "ppam_2026_mcp_tests_glm_4_7_flash",
    "deepseek-v3": "ppam_2026_mcp_tests_deepseek_v3",
    # Retained for legacy artefacts but not in the active panel:
    "qwen3-coder-30b": "ppam_2026_mcp_tests_qwen3_coder_30b",
    "qwq-32b": "ppam_2026_mcp_tests_qwq_32b",
}

# spaceId-by-name (informational; the harness queries Onezone at runtime
# to resolve names → IDs in case the operator regenerates a space).
PER_LLM_SPACE_ID: dict[str, str] = {
    "claude-sonnet-4-5": "d3a48a8d428c9a8ac1ffee471a2d8bb3ch0d5f",
    "qwen3.6-35b": "b724a1f754a37c38dc0615cb079f651fchf8b3",
    "glm-4.7-flash": "028ebe59f7d722b86ca61ac87810c6a4ch8964",
    "deepseek-v3": "5196f39b18f52908db22b8c1cd95d830chb568",
    "qwen3-coder-30b": "c0cf837e95297582188e8f2a6b8e1105chb3a8",
    "qwq-32b": "63d2dbfa95d29e5c44da94caf1e90367cha755",
}


def space_for(llm_name: str) -> str:
    """Return the canonical space name for an LLM. Raises KeyError if unknown
    so harness misconfiguration fails loud rather than silently using the
    shared default space.
    """
    if llm_name not in PER_LLM_SPACE:
        raise KeyError(
            f"No per-LLM space registered for {llm_name!r}. Register via "
            f"`uv run python -m benchmark.setup_per_llm_spaces` or update "
            f"`benchmark/_per_llm_spaces.py` directly."
        )
    return PER_LLM_SPACE[llm_name]
