"""`schedule_file_replication` as a model sees it: through the MCP client, errors included.

The API-level error paths are in `test/unit/api/test_transfers.py`; these check that the text a
model receives tells it how to fix the call and holds no host, and what a success answers.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp.modules import transfers as transfers_module
from onedata_mcp.token_policy import WRITE_TOOL_NAMES

HOST = "provider.example"
BASE = f"https://{HOST}/api/v3/oneprovider"
TOOL = "schedule_file_replication"
FILE_ID = "0000000000521f5567756964236600a3ch3f2e"
TARGET = "pLocal0123456789abcdef"


@pytest.fixture
def transfers_mcp(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", f"https://{HOST}")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    mcp = FastMCP(name="onedata-mcp-test")
    transfers_module.register_module(mcp)
    return mcp


async def _call(mcp: FastMCP, file_arg: str, target: str = TARGET):
    async with Client(mcp) as client:
        result = await client.call_tool(
            TOOL, {"file_id_or_path": file_arg, "target_provider_id": target}, raise_on_error=False
        )
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    return result, text


@pytest.mark.asyncio
async def test_it_is_a_write_tool_annotated_as_such_with_no_list_input(
    transfers_mcp: FastMCP,
) -> None:
    async with Client(transfers_mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == TOOL)
    assert tool.annotations is not None
    assert tool.annotations.destructiveHint is True and not tool.annotations.readOnlyHint
    assert TOOL in WRITE_TOOL_NAMES
    props = tool.inputSchema["properties"]
    assert sorted(props) == ["file_id_or_path", "target_provider_id"]
    assert all(p["type"] == "string" for p in props.values())  # no list input to cap


@pytest.mark.asyncio
async def test_a_schedule_answers_the_transfer_id(
    transfers_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="POST", url=f"{BASE}/transfers", json={"transferId": "t1"})

    result, text = await _call(transfers_mcp, FILE_ID)

    assert result.is_error is False
    assert result.structured_content == {"transferId": "t1"}


@pytest.mark.asyncio
async def test_a_relative_path_is_refused_without_a_request(
    transfers_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    result, text = await _call(transfers_mcp, "k2val-x/logs/log_20200105.dat")

    assert result.is_error is True
    assert "Paths must start with '/'" in text and "/<space_name>/<path_to_file>" in text
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "needle"),
    [
        (404, {"error": {"id": "notFound", "description": "not found"}}, "No file is known"),
        (403, {"error": {"id": "forbidden", "description": "no"}}, "will not help"),
        (400, {"error": {"id": "spaceNotSupportedBy"}}, "not a provider that supports"),
        (500, {"error": {"id": "internalServerError"}}, "status=500"),
    ],
)
async def test_an_error_reaches_the_model_with_its_hint_and_no_host(
    transfers_mcp: FastMCP, httpx_mock: HTTPXMock, status: int, body: dict, needle: str
) -> None:
    httpx_mock.add_response(method="POST", url=f"{BASE}/transfers", status_code=status, json=body)

    result, text = await _call(transfers_mcp, FILE_ID)

    assert result.is_error is True
    assert needle in text
    assert HOST not in text
