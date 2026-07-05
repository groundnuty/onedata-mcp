"""Tests for the opt-in experimental hardening candidates (onedata-mcp#1)."""

from __future__ import annotations

import json

import pytest
from fastmcp import FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp import experimental
from onedata_mcp.modules import files, harvesters, metadata, qos, spaces, transfers


def _build(exp: bool) -> FastMCP:
    """Replicate main.py's module registration without telemetry / token prune."""
    m = FastMCP(name="test")
    files.register_module(m, experimental=exp)
    harvesters.register_module(m, experimental=exp)
    metadata.register_module(m)
    qos.register_module(m)
    spaces.register_module(m, experimental=exp)
    transfers.register_module(m)
    return m


# --- gate parsing ----------------------------------------------------------


@pytest.mark.parametrize("val", ["", "0", "false", "False", "no", "off"])
def test_gate_off_for_falsy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("ONEDATA_MCP_EXPERIMENTAL", val)
    assert experimental.experimental_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_gate_on_for_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("ONEDATA_MCP_EXPERIMENTAL", val)
    assert experimental.experimental_enabled() is True


def test_gate_off_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEDATA_MCP_EXPERIMENTAL", raising=False)
    assert experimental.experimental_enabled() is False


# --- surface: off is byte-identical (26), on adds exactly the 2 tools (28) --


@pytest.mark.asyncio
async def test_flag_off_registers_26_validated_tools() -> None:
    tools = {t.name for t in await _build(False).list_tools()}
    assert len(tools) == 26
    assert "set_file_xattrs" not in tools
    assert "list_space_datasets" not in tools


@pytest.mark.asyncio
async def test_flag_on_registers_28_tools() -> None:
    off = {t.name for t in await _build(False).list_tools()}
    on = {t.name for t in await _build(True).list_tools()}
    assert len(on) == 28
    assert on - off == {"set_file_xattrs", "list_space_datasets"}


# --- main.py wiring: instructions + counts select on the flag ---------------


@pytest.mark.asyncio
async def test_main_build_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEDATA_MCP_EXPERIMENTAL", raising=False)
    monkeypatch.setattr("onedata_mcp.main.resolve_register_write_tools_sync", lambda: True)
    from onedata_mcp import main

    srv = main._create_onedata_mcp_server()
    assert srv.instructions == main._BASE_INSTRUCTIONS
    assert len({t.name for t in await srv.list_tools()}) == 26


@pytest.mark.asyncio
async def test_main_build_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_MCP_EXPERIMENTAL", "1")
    monkeypatch.setattr("onedata_mcp.main.resolve_register_write_tools_sync", lambda: True)
    from onedata_mcp import main

    srv = main._create_onedata_mcp_server()
    assert srv.instructions == main._EXPERIMENTAL_INSTRUCTIONS
    assert len({t.name for t in await srv.list_tools()}) == 28


# --- candidate behaviour ---------------------------------------------------


@pytest.mark.asyncio
async def test_set_file_xattrs_delegates_to_metadata_xattrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onedata_mcp.api import files as api_files

    calls: list[tuple] = []

    async def fake(fid: str, mtype: str, meta: str) -> None:
        calls.append((fid, mtype, meta))

    monkeypatch.setattr(api_files, "set_file_metadata", fake)
    await api_files.set_file_xattrs("/s/f", {"license": "CC-0"})
    await api_files.set_file_xattrs("/s/f", '{"a": "b"}')
    assert calls == [
        ("/s/f", "xattrs", json.dumps({"license": "CC-0"})),
        ("/s/f", "xattrs", '{"a": "b"}'),
    ]


@pytest.mark.asyncio
async def test_list_space_datasets_hits_oneprovider_endpoint(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://oneprovider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    from onedata_mcp.api import spaces as api_spaces

    # hex-shaped id → resolver returns it as-is (no extra list-spaces call)
    sid = "9742830720c0ef94496dad1d96595736ch776e"
    httpx_mock.add_response(
        method="GET",
        json={"datasets": [{"datasetId": "d1"}], "isLast": True},
    )
    result = await api_spaces.list_space_datasets(sid, state="attached", limit=50)
    assert result["datasets"][0]["datasetId"] == "d1"

    req = httpx_mock.get_requests()[-1]
    assert req.url.path == f"/api/v3/oneprovider/spaces/{sid}/datasets"
    assert req.url.params["state"] == "attached"
    assert req.url.params["limit"] == "50"


def test_list_space_datasets_rejects_bad_state() -> None:
    import asyncio

    from onedata_mcp.api import spaces as api_spaces

    with pytest.raises(ValueError, match="attached.*detached|state"):
        asyncio.run(api_spaces.list_space_datasets("x", state="bogus"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_harvester_query_coerces_json_string() -> None:
    from onedata_mcp.api import harvesters as api_h

    assert api_h.coerce_harvesters_index_query({"method": "get"}) == {"method": "get"}
    assert api_h.coerce_harvesters_index_query('{"method": "get"}') == {"method": "get"}
    with pytest.raises(ValueError, match="not valid JSON"):
        api_h.coerce_harvesters_index_query("{not json}")
