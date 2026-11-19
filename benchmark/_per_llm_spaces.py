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
    "deepseek-v4-pro": "ppam_2026_mcp_tests_deepseek_v4_pro",
    "gemma-4-31b-it": "ppam_2026_mcp_tests_gemma_4_31b_it",
    "granite-4.1-30b": "ppam_2026_mcp_tests_granite_4_1_30b",
    "devstral-2-123b": "ppam_2026_mcp_tests_devstral_2_123b",
    # Journal-extension cross-comparison legs (issue #16, 2026-07-06):
    "llama-3.3-70b": "ppam_2026_mcp_tests_llama_3_3_70b",
    "qwen3-coder-30b": "ppam_2026_mcp_tests_qwen3_coder_30b",
    # Retained for legacy artefacts but not in the active panel:
    "deepseek-v3": "ppam_2026_mcp_tests_deepseek_v3",
    "qwq-32b": "ppam_2026_mcp_tests_qwq_32b",
}

# spaceId-by-name (informational; the harness queries Onezone at runtime
# to resolve names → IDs in case the operator regenerates a space).
# Refreshed 2026-07-06 after the federation rebuild (old IDs were dead;
# spaces re-created via setup_per_llm_spaces). deepseek-v3 / qwq-32b not
# re-created (not in the active panel) — their IDs remain historical/stale.
PER_LLM_SPACE_ID: dict[str, str] = {
    "claude-sonnet-4-5": "29e079b5412ba8aaf15a7cbb1500c00dch4c13",
    "qwen3.6-35b": "a66495179ca5d75e67c903f56a0fada1ch78bb",
    "glm-4.7-flash": "157d6f9f1ff964ab94e380c2446edd43cha7a7",
    "deepseek-v4-pro": "7e53cda05da3bb7627f99bd37b28b731cha8a3",
    "gemma-4-31b-it": "e653bba8dca0ae1ae61b2c5fe8a811cbch72dd",
    "granite-4.1-30b": "8a477febbe13dff39211301217304ab4chd0dc",
    "devstral-2-123b": "f7540eab8fd945db2f4e9f4451cc80bcchd9bb",
    "llama-3.3-70b": "1a8c3d164392a7d9459e292f498180a7chc598",
    "qwen3-coder-30b": "bb2d5855af0906e7507f16cb4f7e5e54cha989",
    # not re-created on the rebuilt federation (inactive panel):
    "deepseek-v3": "5196f39b18f52908db22b8c1cd95d830chb568",
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
