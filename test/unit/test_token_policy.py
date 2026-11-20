"""Tests for the read-only-token write-tool gating (operational, not MCP-exposed)."""

import pytest

from onedata_mcp import token_policy
from onedata_mcp.main import mcp


def _clear_onezone_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEDATA_ONEZONE_HOST", raising=False)
    monkeypatch.delenv("ONEDATA_ONEZONE_TOKEN", raising=False)
    monkeypatch.delenv("ONEDATA_ONEPROVIDER_TOKEN", raising=False)


# --- gating decision logic -------------------------------------------------


@pytest.mark.asyncio
async def test_keeps_writers_when_check_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_onezone_env(monkeypatch)
    # No Onezone host/token configured → check is skipped → keep writers.
    assert await token_policy.resolve_register_write_tools() is True


@pytest.mark.asyncio
async def test_keeps_writers_when_provider_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "user-token")
    monkeypatch.delenv("ONEDATA_ONEPROVIDER_TOKEN", raising=False)
    # Gate is configured but there is no provider token to examine → fail open.
    assert await token_policy.resolve_register_write_tools() is True


@pytest.mark.asyncio
async def test_hides_writers_when_token_readonly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "user-token")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "ro-token")

    async def fake_examine(_token: str) -> dict:
        return {"caveats": [{"type": "data.readonly"}]}

    monkeypatch.setattr(token_policy, "examine_access_token", fake_examine)
    assert await token_policy.resolve_register_write_tools() is False


@pytest.mark.asyncio
async def test_keeps_writers_when_token_not_readonly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "user-token")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "rw-token")

    async def fake_examine(_token: str) -> dict:
        return {"caveats": [{"type": "time"}]}

    monkeypatch.setattr(token_policy, "examine_access_token", fake_examine)
    assert await token_policy.resolve_register_write_tools() is True


@pytest.mark.asyncio
async def test_fails_open_when_examine_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "user-token")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "ro-token")

    async def boom(_token: str) -> dict:
        raise TypeError("decode failed")

    monkeypatch.setattr(token_policy, "examine_access_token", boom)
    # Examine failure must fail open (keep writers) — never silently disable.
    assert await token_policy.resolve_register_write_tools() is True


# --- drift guard + prune behaviour -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "1"], ids=["flag-off", "flag-on"])
async def test_write_tool_names_match_the_destructive_annotations(
    monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    """WRITE_TOOL_NAMES == the destructive-annotated tools + ensure_readiness.

    ensure_readiness places QoS rules but is annotated destructiveHint=False (documented
    in token_policy.py), so it is the one write tool without the annotation. Checked with
    the preview flag off and on, because the flag registers more tools.
    """
    if flag is None:
        monkeypatch.delenv("ONEDATA_MCP_EXPERIMENTAL", raising=False)
    else:
        monkeypatch.setenv("ONEDATA_MCP_EXPERIMENTAL", flag)
    monkeypatch.delenv("ONEDATA_MCP_TOOLS", raising=False)
    monkeypatch.delenv("ONEDATA_MCP_MATURITY", raising=False)
    monkeypatch.setattr("onedata_mcp.main.resolve_register_write_tools_sync", lambda: True)
    from onedata_mcp import main

    tools = await main._create_onedata_mcp_server().list_tools()
    destructive = {
        t.name for t in tools if t.annotations is not None and t.annotations.destructiveHint is True
    }
    assert destructive | {"ensure_readiness"} == token_policy.WRITE_TOOL_NAMES
    assert "ensure_readiness" not in destructive


@pytest.mark.asyncio
async def test_every_write_tool_name_is_a_real_registered_tool() -> None:
    """WRITE_TOOL_NAMES must reference tools that actually exist (catch renames)."""
    server_tools = {t.name for t in await mcp.list_tools()}
    missing = token_policy.WRITE_TOOL_NAMES - server_tools
    assert not missing, f"WRITE_TOOL_NAMES references unknown tools: {sorted(missing)}"


@pytest.mark.asyncio
async def test_pruning_removes_exactly_the_write_tools() -> None:
    """Removing WRITE_TOOL_NAMES leaves the read surface intact (prune semantics)."""
    from fastmcp import FastMCP

    from onedata_mcp.modules import files, qos, readiness, transfers

    probe = FastMCP(name="probe")
    files.register_module(probe)
    qos.register_module(probe)
    readiness.register_module(probe)
    transfers.register_module(probe)

    before = {t.name for t in await probe.list_tools()}
    assert before >= token_policy.WRITE_TOOL_NAMES

    for name in token_policy.WRITE_TOOL_NAMES:
        probe.local_provider.remove_tool(name)

    after = {t.name for t in await probe.list_tools()}
    assert after == before - token_policy.WRITE_TOOL_NAMES
    # A representative read tool survives the prune.
    assert "get_file_metadata" in after
    # check_readiness only reads; ensure_readiness places QoS rules and is pruned.
    assert "check_readiness" in after
    assert "ensure_readiness" not in after


def test_ensure_readiness_is_a_write_tool_and_check_readiness_is_not() -> None:
    assert "ensure_readiness" in token_policy.WRITE_TOOL_NAMES
    assert "check_readiness" not in token_policy.WRITE_TOOL_NAMES
