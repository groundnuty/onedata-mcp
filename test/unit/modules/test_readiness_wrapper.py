"""Tests for `onedata_mcp/modules/readiness.py`: what the model sees and how a call runs.

The logic itself is tested in `test/unit/api/test_readiness.py`. Here: the two tool
definitions (names, descriptions, annotations, input schemas) and one call of each tool
through the MCP client.
"""

from __future__ import annotations

import json
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
    assert "about 2.5 minutes" in ensure
    # holds are said to be QoS requirements that stay until removed, by the ids the answer lists
    assert "held by a QoS requirement" in ensure and "removed by its id" in ensure
    assert "the answer lists the ids" in ensure
    assert "for your run" not in ensure
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
async def test_descriptions_name_no_tool_and_cite_no_document(readiness_mcp: FastMCP) -> None:
    """Gate 2 (design/10): either tool may be offered without the other (a read-only token
    prunes ensure_readiness; a scenario may offer one alone), so neither names the other."""
    tools = await _tools(readiness_mcp)
    for tool in tools.values():
        schema_text = json.dumps(tool.inputSchema)
        for text in (tool.description, schema_text):
            assert not re.search(r"\b\w+_readiness\b", text), tool.name
            assert not re.search(r"design/|research/|\.md\b|M-\d|swagger|GET |POST ", text)
        # what it takes and what it returns, in the model's terms
        assert "`extent`" in tool.description and "YYYYMMDD" in tool.description
        assert "a directory path inside it" in tool.description
        assert "The answer is ready" in tool.description


@pytest.mark.asyncio
async def test_the_extent_field_carries_an_example_that_its_own_schema_accepts(
    readiness_mcp: FastMCP,
) -> None:
    """A concrete example, so a model need not guess the shape (one guessed
    `{space, paths, date_from, date_to}`); it shows both forms of `dataset`, `files` and
    `date_ranges`, and validates against the schema it describes."""
    from pydantic import TypeAdapter

    tools = await _tools(readiness_mcp)
    for name in ("check_readiness", "ensure_readiness"):
        text = tools[name].inputSchema["properties"]["extent"]["description"]
        assert text.startswith("The data you need: one item per dataset. Example: ")
        example = json.loads(text.split("Example: ", 1)[1])
        assert example == json.loads(module.EXTENT_EXAMPLE)
        items = TypeAdapter(list[module.ExtentItem]).validate_python(example)
        assert {i.dataset for i in items} == {"my-dataset", "/my-dataset/sub/dir"}
        assert any(i.files for i in items) and any(i.date_ranges for i in items)
        # the example names no field the schema lacks, and fits the item's caps
        allowed = set(tools[name].inputSchema["properties"]["extent"]["items"]["properties"])
        assert all(set(item) <= allowed for item in example)


@pytest.mark.asyncio
async def test_an_extent_item_or_a_date_range_takes_no_other_field(readiness_mcp: FastMCP) -> None:
    """An unknown field used to be dropped: `{dataset, paths}` became the whole dataset."""
    tools = await _tools(readiness_mcp)
    for name in ("check_readiness", "ensure_readiness"):
        item = tools[name].inputSchema["properties"]["extent"]["items"]
        assert item["additionalProperties"] is False
        date_range = next(
            branch["items"]
            for branch in item["properties"]["date_ranges"]["anyOf"]
            if branch.get("type") == "array"
        )
        assert date_range["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item", "needle"),
    [
        ({"dataset": "x", "paths": ["a.nc"], "date_from": "2018-06-01"}, "paths"),
        ({"dataset": "x", "file": ["a.nc"]}, "file"),
        ({"dataset": "x", "date_ranges": [{"from": "2018-06-01", "to": "2018-06-02"}]}, "from"),
    ],
)
async def test_an_unknown_field_is_refused_by_name_before_anything_is_read(
    readiness_mcp: FastMCP, item: dict, needle: str
) -> None:
    for tool, extra in (("check_readiness", {}), ("ensure_readiness", {"deadline_s": 60})):
        async with Client(readiness_mcp) as client:
            with pytest.raises(ToolError, match=needle):
                await client.call_tool(tool, {"extent": [item], **extra})


@pytest.mark.asyncio
async def test_a_dataset_above_its_space_is_refused_with_a_hint_the_model_sees(
    readiness_mcp: FastMCP,
) -> None:
    async with Client(readiness_mcp) as client:
        with pytest.raises(ToolError, match=r"empty, '\.' or '\.\.' component: give a space"):
            await client.call_tool("check_readiness", {"extent": [{"dataset": "/my-dataset/.."}]})


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
        "ready_files": 1,
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
        "held": {
            "files": 1,
            "rule": "providerId=pLocal & anyStorage",
            "qos_requirement_ids": ["q1"],
        },
    }
    posted = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/qos_requirements")]
    assert len(posted) == 1


def _mock_one_remote_file(httpx_mock: HTTPXMock) -> None:
    """A space with one file, complete at its owner only: not ready, at another site."""
    httpx_mock.add_response(
        method="GET", url=f"{PROVIDER}/configuration", json={"providerId": "pLocal"}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{PROVIDER}/spaces",
        json=[{"name": "my-dataset", "spaceId": "s1", "fileId": "root1"}],
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
                    "isFullyReplicatedLocally": False,
                    "localReplicationRate": 0.0,
                    "originProviderId": "pOwner",
                }
            ],
            "isLast": True,
            "nextPageToken": None,
        },
    )
    storage = {"s": {"success": True, "blocks": [[0, 10]]}}
    empty = {"s": {"success": True, "blocks": []}}
    httpx_mock.add_response(
        method="GET",
        url=f"{PROVIDER}/data/f1/distribution",
        json={
            "type": "REG",
            "distributionPerProvider": {
                "pOwner": {
                    "success": True,
                    "logicalSize": 10,
                    "distributionPerStorageBackend": storage,
                },
                "pLocal": {
                    "success": True,
                    "logicalSize": 10,
                    "distributionPerStorageBackend": empty,
                },
            },
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/providers/pOwner",
        json={"providerId": "pOwner", "online": True},
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(PROVIDER)}/spaces/s1/transfers.*"),
        json={"transfers": [], "nextPageToken": None},
        is_optional=True,
    )


POINTER = "ensure_readiness can wait for it and place it at your provider."
EXTENT = {"extent": [{"dataset": "my-dataset"}]}


@pytest.mark.asyncio
async def test_the_answer_names_ensure_readiness_when_the_server_offers_it(
    readiness_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    _mock_one_remote_file(httpx_mock)

    async with Client(readiness_mcp) as client:
        result = (await client.call_tool("check_readiness", EXTENT)).data

    assert result["ready"] is False and result["ready_files"] == 0
    assert result["advice"].endswith(POINTER)


@pytest.mark.asyncio
async def test_the_answer_names_no_tool_when_the_server_removed_ensure_readiness(
    readiness_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """A read-only token or a selection removes it at launch (main.py)."""
    _mock_one_remote_file(httpx_mock)
    readiness_mcp.local_provider.remove_tool("ensure_readiness")

    async with Client(readiness_mcp) as client:
        result = (await client.call_tool("check_readiness", EXTENT)).data

    assert result["ready"] is False
    assert "ensure_readiness" not in result["advice"]


@pytest.mark.asyncio
async def test_a_host_that_offers_fewer_tools_than_registered_is_believed(
    readiness_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """The panel's in-process host offers a scenario's set of a server that registers all."""
    from onedata_mcp.tool_maturity import OFFERED_TOOLS

    _mock_one_remote_file(httpx_mock)
    token = OFFERED_TOOLS.set(frozenset({"check_readiness"}))
    try:
        async with Client(readiness_mcp) as client:
            result = (await client.call_tool("check_readiness", EXTENT)).data
    finally:
        OFFERED_TOOLS.reset(token)

    assert "ensure_readiness" not in result["advice"]


# --- External review (design/17, design/18): reproduced first, then fixed --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        {"dataset": "my-dataset", "files": None},
        {"dataset": "my-dataset", "date_ranges": None},
        {"dataset": "my-dataset", "files": []},
        {"dataset": "my-dataset", "date_ranges": []},
    ],
    ids=["files-null", "date_ranges-null", "files-empty", "date_ranges-empty"],
)
@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
async def test_review_an_explicitly_empty_selector_is_refused_through_the_mcp_client(
    readiness_mcp: FastMCP, item: dict
) -> None:
    """Codex 1, Gemini 4: an explicit `null` was dropped on the way in (it read as left out,
    the whole dataset), and `[]` too. Both are refused before any request, with a hint."""
    for tool, extra in (("check_readiness", {}), ("ensure_readiness", {"deadline_s": 60})):
        async with Client(readiness_mcp) as client:
            with pytest.raises(ToolError, match=r"extent\[0\]\.(?:files|date_ranges) is"):
                await client.call_tool(tool, {"extent": [item], **extra})


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
async def test_review_an_unknown_field_through_the_mcp_client_lists_the_valid_fields(
    readiness_mcp: FastMCP,
) -> None:
    """Gemini 11: the schema's own refusal ("Extra inputs are not permitted") came first and
    did not say which fields an item has."""
    item = {"dataset": "x", "paths": ["a.nc"]}
    async with Client(readiness_mcp) as client:
        with pytest.raises(ToolError, match=r"its fields are dataset, files, date_ranges"):
            await client.call_tool("check_readiness", {"extent": [item]})
