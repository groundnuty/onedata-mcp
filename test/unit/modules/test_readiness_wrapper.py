"""Tests for `onedata_mcp/modules/readiness.py`: what the model sees and how a call runs.

The logic itself is tested in `test/unit/api/test_readiness.py`. Here: the two tool
definitions (names, descriptions, annotations, input schemas) and one call of each tool
through the MCP client.
"""

from __future__ import annotations

import re

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pytest_httpx import HTTPXMock

from onedata_mcp.api import readiness as api
from onedata_mcp.modules import readiness as module

PROVIDER = "https://provider.example/api/v3/oneprovider"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token-z")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    api.reset_state()
    yield
    api.reset_state()


@pytest.fixture
def readiness_mcp() -> FastMCP:
    mcp = FastMCP(name="probe")
    module.register_module(mcp)
    return mcp


async def _tools(mcp: FastMCP) -> dict:
    async with Client(mcp) as client:
        return {t.name: t for t in await client.list_tools()}


@pytest.mark.asyncio
async def test_both_tools_are_registered_with_the_specified_annotations(
    readiness_mcp: FastMCP,
) -> None:
    tools = await _tools(readiness_mcp)

    assert set(tools) == {"check_readiness", "ensure_readiness"}
    assert tools["check_readiness"].annotations.readOnlyHint is True
    assert tools["ensure_readiness"].annotations.destructiveHint is False
    assert not tools["ensure_readiness"].annotations.readOnlyHint


@pytest.mark.asyncio
async def test_descriptions_say_what_the_answer_means_and_advise_nothing_on_the_analysis(
    readiness_mcp: FastMCP,
) -> None:
    tools = await _tools(readiness_mcp)
    check = tools["check_readiness"].description
    ensure = tools["ensure_readiness"].description

    for reason in ("being_written", "at_other_site", "in_transfer", "unreachable"):
        assert reason in check
    assert check.endswith("Read-only.")
    assert "YYYYMMDD" in check
    assert (
        "ready (the data are complete and readable at your provider now, and held there)" in ensure
    )
    assert "call it again" in ensure
    assert "about 4 minutes" in ensure
    for text in (check, ensure):
        assert not re.search(
            r"comput|analy[sz]e|should|must|do not", text.replace("your analysis", ""), re.I
        )
    assert "Compute" not in ensure


@pytest.mark.asyncio
async def test_extent_schema_is_small(readiness_mcp: FastMCP) -> None:
    tools = await _tools(readiness_mcp)

    for name in ("check_readiness", "ensure_readiness"):
        extent = tools[name].inputSchema["properties"]["extent"]
        assert (extent["minItems"], extent["maxItems"]) == (1, 20)
        item = extent["items"]
        assert set(item["properties"]) == {"dataset", "files", "date_ranges"}
        assert item["required"] == ["dataset"]
        date_range = next(
            branch["items"]
            for branch in item["properties"]["date_ranges"]["anyOf"]
            if branch.get("type") == "array"
        )
        assert set(date_range["properties"]) == {"start", "end"}
        assert sorted(date_range["required"]) == ["end", "start"]
        # the caps of one item: 20000 names, 400 date ranges
        arrays = {
            name: next(b for b in item["properties"][name]["anyOf"] if b.get("type") == "array")
            for name in ("files", "date_ranges")
        }
        assert arrays["files"]["maxItems"] == 20_000
        assert arrays["date_ranges"]["maxItems"] == 400

    assert tools["check_readiness"].inputSchema["required"] == ["extent"]
    ensure_schema = tools["ensure_readiness"].inputSchema
    assert sorted(ensure_schema["required"]) == ["deadline_s", "extent"]
    deadline = ensure_schema["properties"]["deadline_s"]
    assert (deadline["type"], deadline["minimum"], deadline["maximum"]) == ("integer", 1, 86400)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"extent": []},
        {"extent": [{"dataset": "x"}] * 21},
        {"extent": [{"files": ["a.nc"]}]},
        {"extent": [{"dataset": "x", "files": ["a.nc"] * 20_001}]},
        {
            "extent": [
                {
                    "dataset": "x",
                    "date_ranges": [{"start": "2018-01-01", "end": "2018-01-02"}] * 401,
                }
            ]
        },
        {"extent": [{"dataset": "x", "date_ranges": [{"start": "June 1", "end": "2018-08-31"}]}]},
    ],
)
async def test_check_rejects_a_malformed_extent_before_doing_anything(
    readiness_mcp: FastMCP, arguments: dict
) -> None:
    async with Client(readiness_mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("check_readiness", arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline_s", [0, 86401, "soon"])
async def test_ensure_rejects_a_bad_deadline(readiness_mcp: FastMCP, deadline_s: object) -> None:
    async with Client(readiness_mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "ensure_readiness",
                {"extent": [{"dataset": "my-dataset"}], "deadline_s": deadline_s},
            )


def _mock_one_ready_file(httpx_mock: HTTPXMock) -> None:
    """A space with one file, complete at the local provider and at its owner."""
    httpx_mock.add_response(
        method="GET", url=f"{PROVIDER}/configuration", json={"providerId": "pLocal"}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{PROVIDER}/spaces",
        json=[
            {
                "name": "my-dataset",
                "spaceId": "s1",
                "providers": [{"providerId": "pLocal", "providerName": "local"}],
                "fileId": "root1",
            }
        ],
    )
    httpx_mock.add_response(
        method="POST", url=f"{PROVIDER}/lookup-file-id/%2Fmy-dataset", json={"fileId": "root1"}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{PROVIDER}/data/root1/files",
        json={
            "files": [
                {
                    "fileId": "f1",
                    "path": "data_20180801.nc",
                    "name": "data_20180801.nc",
                    "type": "REG",
                    "size": 10,
                    "isFullyReplicatedLocally": True,
                    "localReplicationRate": 1.0,
                    "originProviderId": "pOwner",
                }
            ],
            "isLast": True,
            "nextPageToken": None,
        },
    )
    full = {
        "success": True,
        "logicalSize": 10,
        "distributionPerStorageBackend": {"s": {"success": True, "blocks": [[0, 10]]}},
    }
    httpx_mock.add_response(
        method="GET",
        url=f"{PROVIDER}/data/f1/distribution",
        json={"type": "REG", "distributionPerProvider": {"pLocal": full, "pOwner": full}},
        is_reusable=True,
    )


@pytest.mark.asyncio
async def test_check_readiness_through_the_mcp_client(
    readiness_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    _mock_one_ready_file(httpx_mock)

    async with Client(readiness_mcp) as client:
        result = await client.call_tool(
            "check_readiness",
            {
                "extent": [
                    {
                        "dataset": "my-dataset",
                        "date_ranges": [{"start": "2018-06-01", "end": "2018-08-31"}],
                    }
                ]
            },
        )

    assert result.data == {
        "ready": True,
        "files": 1,
        "bytes": 10,
        "advice": "Ready: all declared files are complete and readable at your provider now.",
    }


@pytest.mark.asyncio
async def test_ensure_readiness_through_the_mcp_client(
    readiness_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    _mock_one_ready_file(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"{PROVIDER}/qos_requirements",
        status_code=201,
        json={"qosRequirementId": "q1"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{PROVIDER}/qos_requirements/q1",
        json={"qosRequirementId": "q1", "fileId": "f1", "status": "pending"},
        is_reusable=True,  # read back for the answer
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{PROVIDER}/data/f1",
        json={"isFullyReplicatedLocally": True, "size": 10},
        is_reusable=True,  # the wait confirms the file, and the final pass reads it again
    )

    async with Client(readiness_mcp) as client:
        result = await client.call_tool(
            "ensure_readiness",
            {
                "extent": [{"dataset": "my-dataset", "files": ["data_20180801.nc"]}],
                "deadline_s": 60,
            },
        )

    assert result.data == {
        "ready": True,
        "files": 1,
        "bytes": 10,
        "advice": (
            "Ready: all declared files are complete and readable at your provider now, "
            "and held there."
        ),
        "waited_s": 0,
        "held": {"files": 1, "rule": "providerId=pLocal & anyStorage"},
    }
    posted = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/qos_requirements")]
    assert len(posted) == 1
