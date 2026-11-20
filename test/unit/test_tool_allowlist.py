"""Verify that the curated headline allowlist matches what the MCP server actually exposes.

Catches drift in either direction:
- a tool listed in HEADLINE / ABLATION_EXTRAS that the server doesn't expose
- a tool the server exposes that's not classified in any of the three sets

The allowlist module is the earlier benchmark's frozen record. It classifies six
inherited tools that the server now registers only with the preview flag
ONEDATA_MCP_EXPERIMENTAL (get_file_id, grep_file_content, list_marketplace_spaces and
the three harvester tools), so it is checked against the flag-on surface.

Headline cardinality is 16 as of 2026-05-02 (was 15; `get_qos_requirement`
promoted from ABLATION_EXTRAS for P6 — see tool_allowlist.py docstring).
"""

from __future__ import annotations

import pytest

from benchmark.tool_allowlist import (
    ABLATION_EXTRAS,
    ABLATION_FULL,
    EXCLUDED_HARVESTER,
    HEADLINE,
)

# The readiness tools (design/07-readiness-tools.md) are outside the PPAM benchmark's
# classification on purpose: adding them to ABLATION_EXTRAS would put them in the paper's
# ablation surface (ABLATION_FULL). The benchmark module stays as it is.
READINESS_TOOLS = frozenset({"check_readiness", "ensure_readiness"})

# Preview tools the flag-on surface adds that the PPAM benchmark never classified, for the
# same reason.
PREVIEW_ONLY_TOOLS = frozenset({"list_space_datasets", "set_file_xattrs"})


async def _flag_on_surface(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The tools the server registers with ONEDATA_MCP_EXPERIMENTAL on."""
    monkeypatch.setenv("ONEDATA_MCP_EXPERIMENTAL", "1")
    monkeypatch.delenv("ONEDATA_MCP_TOOLS", raising=False)
    monkeypatch.delenv("ONEDATA_MCP_MATURITY", raising=False)
    monkeypatch.setattr("onedata_mcp.main.resolve_register_write_tools_sync", lambda: True)
    from onedata_mcp import main

    return {t.name for t in await main._create_onedata_mcp_server().list_tools()}


@pytest.mark.asyncio
async def test_headline_has_expected_cardinality() -> None:
    assert len(HEADLINE) == 16


@pytest.mark.asyncio
async def test_three_classifications_are_disjoint() -> None:
    assert HEADLINE.isdisjoint(ABLATION_EXTRAS)
    assert HEADLINE.isdisjoint(EXCLUDED_HARVESTER)
    assert ABLATION_EXTRAS.isdisjoint(EXCLUDED_HARVESTER)


@pytest.mark.asyncio
async def test_every_classified_tool_is_actually_exposed_by_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_tools = await _flag_on_surface(monkeypatch)
    classified = HEADLINE | ABLATION_EXTRAS | EXCLUDED_HARVESTER
    missing = classified - server_tools
    assert not missing, (
        f"Tools in allowlist classification but not exposed by the server: {sorted(missing)}. "
        "Either fix the allowlist or wire the missing tool in onedata_mcp/main.py."
    )


@pytest.mark.asyncio
async def test_no_server_tool_is_unclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    server_tools = await _flag_on_surface(monkeypatch)
    classified = HEADLINE | ABLATION_EXTRAS | EXCLUDED_HARVESTER
    unclassified = server_tools - classified - READINESS_TOOLS - PREVIEW_ONLY_TOOLS
    assert not unclassified, (
        f"Server exposes tools not classified in the allowlist: {sorted(unclassified)}. "
        "Add each to HEADLINE, ABLATION_EXTRAS, or EXCLUDED_HARVESTER."
    )


@pytest.mark.asyncio
async def test_ablation_full_excludes_harvesters() -> None:
    """ABLATION_FULL is the union of HEADLINE and ABLATION_EXTRAS only —
    it intentionally excludes harvester tools because the federation has no
    harvester configured."""
    assert ABLATION_FULL.isdisjoint(EXCLUDED_HARVESTER)
    assert ABLATION_FULL == HEADLINE | ABLATION_EXTRAS
