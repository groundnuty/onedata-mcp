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
async def test_every_write_tool_name_is_a_real_registered_tool() -> None:
    """WRITE_TOOL_NAMES must reference tools that actually exist (catch renames)."""
    server_tools = {t.name for t in await mcp.list_tools()}
    missing = token_policy.WRITE_TOOL_NAMES - server_tools
    assert not missing, f"WRITE_TOOL_NAMES references unknown tools: {sorted(missing)}"


@pytest.mark.asyncio
async def test_pruning_removes_exactly_the_write_tools() -> None:
    """Removing WRITE_TOOL_NAMES leaves the read surface intact (prune semantics)."""
    from fastmcp import FastMCP

    from onedata_mcp.modules import files, qos, transfers

    probe = FastMCP(name="probe")
    files.register_module(probe)
    qos.register_module(probe)
    transfers.register_module(probe)

    before = {t.name for t in await probe.list_tools()}
    assert before >= token_policy.WRITE_TOOL_NAMES

    for name in token_policy.WRITE_TOOL_NAMES:
        probe.local_provider.remove_tool(name)

    after = {t.name for t in await probe.list_tools()}
    assert after == before - token_policy.WRITE_TOOL_NAMES
    # A representative read tool survives the prune.
    assert "get_file_metadata" in after
