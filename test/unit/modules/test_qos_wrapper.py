"""`remove_qos_requirement` as a model sees it: through the MCP client, errors included.

The API-level error paths are in `test/unit/api/test_qos.py`; these check that the text a model
receives tells it how to fix the call, holds no host, and that a success is an empty answer.
"""

from __future__ import annotations

import re

import pytest
from fastmcp import Client, FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp.modules import qos as qos_module
from onedata_mcp.token_policy import WRITE_TOOL_NAMES
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED

HOST = "provider.example"
QOS_URL = f"https://{HOST}/api/v3/oneprovider/qos_requirements"
QID = "c84f669f9522c46976fee490d80651f0"


@pytest.fixture
def qos_mcp(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", f"https://{HOST}")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    mcp = FastMCP(name="onedata-mcp-test")
    qos_module.register_module(mcp)
    return mcp


async def _call(mcp: FastMCP, qos_id: str):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "remove_qos_requirement", {"qos_id": qos_id}, raise_on_error=False
        )
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    return result, text


@pytest.mark.asyncio
async def test_it_is_a_write_tool_annotated_destructive(qos_mcp: FastMCP) -> None:
    async with Client(qos_mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == "remove_qos_requirement")
    assert tool.annotations is not None and tool.annotations.destructiveHint is True
    assert "remove_qos_requirement" in WRITE_TOOL_NAMES
    assert list(tool.inputSchema["properties"]) == ["qos_id"]  # no list input to cap


@pytest.mark.asyncio
async def test_the_description_follows_the_surface_rules(qos_mcp: FastMCP) -> None:
    """design/10, gate 2: it says what it returns, names no other tool (one may not be offered
    alongside it) and cites no document the model cannot read."""
    async with Client(qos_mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == "remove_qos_requirement")
    field = tool.inputSchema["properties"]["qos_id"]["description"]
    text = f"{tool.description} {field}"

    assert "Returns nothing on success" in text
    assert "parent directories" in text  # an inherited rule affects every file under it
    others = (ALL_KNOWN | FLAG_GATED) - {"remove_qos_requirement"}
    assert [name for name in others if name in text] == []
    assert not re.search(r"\.md\b|design/|https?://|§|\bM-\d", text)


@pytest.mark.asyncio
async def test_a_removal_answers_nothing(qos_mcp: FastMCP, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{QID}", status_code=204)

    result, text = await _call(qos_mcp, QID)

    assert result.is_error is False
    assert result.content == [] and text == ""


@pytest.mark.asyncio
async def test_an_unknown_id_tells_the_model_where_the_ids_are(
    qos_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="DELETE",
        url=f"{QOS_URL}/{QID}",
        status_code=404,
        json={"error": {"id": "notFound", "description": "The resource could not be found."}},
    )

    result, text = await _call(qos_mcp, QID)

    assert result.is_error is True
    assert f"No QoS requirement with id '{QID}' exists" in text
    assert "get_file_qos_summary" in text
    assert HOST not in text


@pytest.mark.asyncio
async def test_a_path_is_refused_without_a_request(qos_mcp: FastMCP, httpx_mock: HTTPXMock) -> None:
    result, text = await _call(qos_mcp, "/space/logs/log_20200107.dat")

    assert result.is_error is True
    assert "is a path, not a QoS requirement id" in text
    assert "get_file_qos_summary" in text
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_a_permission_error_says_a_retry_will_not_help(
    qos_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="DELETE",
        url=f"{QOS_URL}/{QID}",
        status_code=403,
        json={"error": {"id": "forbidden", "description": "You are not authorized."}},
    )

    result, text = await _call(qos_mcp, QID)

    assert result.is_error is True
    assert "Not allowed to remove QoS requirement" in text and "will not help" in text
    assert HOST not in text
