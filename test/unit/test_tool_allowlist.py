"""Verify that the curated 14-tool allowlist matches what the MCP server actually exposes.

Catches drift in either direction:
- a tool listed in HEADLINE / ABLATION_EXTRAS that the server doesn't expose
- a tool the server exposes that's not classified in any of the three sets
"""

from __future__ import annotations

import pytest

from benchmark.tool_allowlist import (
    ABLATION_EXTRAS,
    ABLATION_FULL,
    EXCLUDED_HARVESTER,
    HEADLINE,
)
from onedata_mcp.main import mcp


@pytest.mark.asyncio
async def test_headline_has_exactly_15_tools() -> None:
    assert len(HEADLINE) == 15


@pytest.mark.asyncio
async def test_three_classifications_are_disjoint() -> None:
    assert HEADLINE.isdisjoint(ABLATION_EXTRAS)
    assert HEADLINE.isdisjoint(EXCLUDED_HARVESTER)
    assert ABLATION_EXTRAS.isdisjoint(EXCLUDED_HARVESTER)


@pytest.mark.asyncio
async def test_every_classified_tool_is_actually_exposed_by_the_server() -> None:
    server_tools = {t.name for t in await mcp.list_tools()}
    classified = HEADLINE | ABLATION_EXTRAS | EXCLUDED_HARVESTER
    missing = classified - server_tools
    assert not missing, (
        f"Tools in allowlist classification but not exposed by the server: {sorted(missing)}. "
        "Either fix the allowlist or wire the missing tool in onedata_mcp/main.py."
    )


@pytest.mark.asyncio
async def test_no_server_tool_is_unclassified() -> None:
    server_tools = {t.name for t in await mcp.list_tools()}
    classified = HEADLINE | ABLATION_EXTRAS | EXCLUDED_HARVESTER
    unclassified = server_tools - classified
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
