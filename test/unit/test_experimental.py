"""Tests for the opt-in experimental hardening candidates (onedata-mcp#1)."""

from __future__ import annotations

import json

import pytest
from fastmcp import FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp import experimental
from onedata_mcp import tool_maturity as tm
from onedata_mcp.modules import files, harvesters, metadata, qos, readiness, spaces, transfers


def _build(exp: bool) -> FastMCP:
    """Replicate main.py's module registration without telemetry / token prune."""
    m = FastMCP(name="test")
    files.register_module(m, experimental=exp)
    harvesters.register_module(m, experimental=exp)
    metadata.register_module(m)
    qos.register_module(m)
    readiness.register_module(m)
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


# --- surface: off is the base surface (29), on adds exactly the 2 tools (31) --


@pytest.mark.asyncio
async def test_flag_off_registers_the_29_base_tools() -> None:
    tools = {t.name for t in await _build(False).list_tools()}
    assert len(tools) == 29
    assert "set_file_xattrs" not in tools
    assert "list_space_datasets" not in tools


@pytest.mark.asyncio
async def test_flag_on_registers_31_tools() -> None:
    off = {t.name for t in await _build(False).list_tools()}
    on = {t.name for t in await _build(True).list_tools()}
    assert len(on) == 31
    assert on - off == {"set_file_xattrs", "list_space_datasets"}


# --- the harvester query tool: flag off is exactly the base definition ------

_BASE_QUERY_SCHEMA = {
    "additionalProperties": True,
    "description": (
        "Plugin-specific query payload for the index "
        '(for example: {"method": "get", "path": "resource_id"})'
    ),
    "type": "object",
}
_BASE_QUERY_DESCRIPTION = "Execute a query against a specific harvester index."


async def _query_tool(exp: bool):
    tools = {t.name: t for t in await _build(exp).list_tools()}
    return tools["query_harvester_index"]


@pytest.mark.asyncio
async def test_flag_off_query_harvester_index_is_the_base_definition() -> None:
    tool = await _query_tool(False)
    assert tool.description == _BASE_QUERY_DESCRIPTION
    assert tool.parameters["properties"]["query"] == _BASE_QUERY_SCHEMA


@pytest.mark.asyncio
async def test_flag_on_query_harvester_index_accepts_a_json_string() -> None:
    tool = await _query_tool(True)
    query = tool.parameters["properties"]["query"]
    assert {"type": "string"} in query["anyOf"]
    assert tool.description.startswith(_BASE_QUERY_DESCRIPTION)
    assert "get_harvester_index_schema" in tool.description


@pytest.mark.asyncio
async def test_query_harvester_index_forwards_a_json_string_as_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The api layer coerces a string payload, with the flag off or on."""
    from onedata_mcp.api import harvesters as api_h

    seen: list[dict] = []

    async def fake_request(config, method, path, json_body=None, **kwargs):  # noqa: ANN001
        seen.append(json_body)
        return {"body": {"hits": []}}

    monkeypatch.setattr(api_h, "request", fake_request)
    monkeypatch.setattr(api_h, "get_onezone_config", lambda: object())
    await api_h.query_harvester_index("h", "i", '{"method": "get"}')
    await api_h.query_harvester_index("h", "i", {"method": "get"})
    assert seen == [{"method": "get"}, {"method": "get"}]


# --- main.py wiring: instructions + counts select on the flag ---------------


@pytest.fixture(autouse=True)
def _no_launch_time_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The main.py tests count tools; keep the caller's tool selection out of them."""
    monkeypatch.delenv("ONEDATA_MCP_TOOLS", raising=False)
    monkeypatch.delenv("ONEDATA_MCP_MATURITY", raising=False)


@pytest.mark.asyncio
async def test_main_build_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEDATA_MCP_EXPERIMENTAL", raising=False)
    monkeypatch.setattr("onedata_mcp.main.resolve_register_write_tools_sync", lambda: True)
    from onedata_mcp import main

    srv = main._create_onedata_mcp_server()
    assert srv.instructions == main._BASE_INSTRUCTIONS
    assert len({t.name for t in await srv.list_tools()}) == 29


@pytest.mark.asyncio
async def test_main_build_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_MCP_EXPERIMENTAL", "1")
    monkeypatch.setattr("onedata_mcp.main.resolve_register_write_tools_sync", lambda: True)
    from onedata_mcp import main

    srv = main._create_onedata_mcp_server()
    assert srv.instructions == main._EXPERIMENTAL_INSTRUCTIONS
    assert len({t.name for t in await srv.list_tools()}) == 31


# --- classification: every tool registered with the flag on is classified ---


@pytest.mark.asyncio
async def test_registered_surface_matches_the_classification_in_both_modes() -> None:
    for exp in (False, True):
        registered = {t.name for t in await _build(exp).list_tools()}
        assert registered == tm.registered_surface(exp), f"experimental={exp}"
    assert tm.registered_surface(True) == tm.ALL_KNOWN | tm.FLAG_GATED


def test_flag_gated_tools_are_experimental_and_upstream_adapted() -> None:
    """Never panel-validated; adapted from later upstream commits (not in UPSTREAM)."""
    assert {"list_space_datasets", "set_file_xattrs"} == tm.FLAG_GATED
    assert not (tm.FLAG_GATED & tm.ALL_KNOWN)  # absent from the default surface
    for name in tm.FLAG_GATED:
        assert tm.maturity_of(name) == "experimental"
        assert tm.origin_of(name) == "upstream"
        assert name not in tm.STABLE | tm.EXPERIMENTAL | tm.UPSTREAM


# --- launch-time selection and the read-only token prune see the new tools ---


def _server(monkeypatch: pytest.MonkeyPatch, *, flag: str | None, writable: bool = True):
    if flag is None:
        monkeypatch.delenv("ONEDATA_MCP_EXPERIMENTAL", raising=False)
    else:
        monkeypatch.setenv("ONEDATA_MCP_EXPERIMENTAL", flag)
    monkeypatch.setattr("onedata_mcp.main.resolve_register_write_tools_sync", lambda: writable)
    from onedata_mcp import main

    return main._create_onedata_mcp_server()


@pytest.mark.asyncio
async def test_flag_on_stable_maturity_prunes_the_flag_gated_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEDATA_MCP_MATURITY", "stable")
    srv = _server(monkeypatch, flag="1")
    assert {t.name for t in await srv.list_tools()} == set(tm.STABLE)


@pytest.mark.asyncio
async def test_flag_on_explicit_tools_can_select_a_flag_gated_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEDATA_MCP_TOOLS", "set_file_xattrs, list_user_spaces")
    srv = _server(monkeypatch, flag="1")
    assert {t.name for t in await srv.list_tools()} == {"set_file_xattrs", "list_user_spaces"}


@pytest.mark.asyncio
async def test_flag_off_explicit_tools_ignore_a_flag_gated_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEDATA_MCP_TOOLS", "set_file_xattrs, list_user_spaces")
    srv = _server(monkeypatch, flag=None)
    assert {t.name for t in await srv.list_tools()} == {"list_user_spaces"}


@pytest.mark.asyncio
async def test_read_only_token_prunes_set_file_xattrs_but_keeps_list_space_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onedata_mcp.token_policy import WRITE_TOOL_NAMES

    writable = {t.name for t in await _server(monkeypatch, flag="1").list_tools()}
    read_only = {t.name for t in await _server(monkeypatch, flag="1", writable=False).list_tools()}
    assert {"set_file_xattrs", "list_space_datasets"} <= writable
    assert read_only == writable - WRITE_TOOL_NAMES - {"set_file_xattrs"}
    assert "list_space_datasets" in read_only


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
