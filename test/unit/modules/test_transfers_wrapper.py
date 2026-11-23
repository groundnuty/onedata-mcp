"""`schedule_file_replication` as a model sees it: through the MCP client, errors included.

The API-level error paths are in `test/unit/api/test_transfers.py`; these check that the text a
model receives tells it how to fix the call and holds no host, and what a success answers.
"""

from __future__ import annotations

import re

import pytest
from fastmcp import Client, FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp.api.transfers import SCHEDULED_NOTE
from onedata_mcp.modules import transfers as transfers_module
from onedata_mcp.token_policy import WRITE_TOOL_NAMES
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED

HOST = "provider.example"
BASE = f"https://{HOST}/api/v3/oneprovider"
TOOL = "schedule_file_replication"
SPACE_ID = "9JO5__9nL1b01YEvwnft1r20gwgwlkIp19ND_n8hgxs"
_PAYLOAD = f"guid#t_w-ghd1c7eDoMvTmo-gW_3aszvpHkGzy6Xw4#{SPACE_ID}".encode()
# a file id of the 25.0 shape: an 8-byte header whose sixth byte is the payload's length, then
# the payload, in hex
FILE_ID = (bytes([0, 0, 0, 0, 0, len(_PAYLOAD), 0x5C, 0xF4]) + _PAYLOAD).hex().upper()
TARGET = "pLocal0123456789abcdef"


def _the_spaces_are_read_if_asked(httpx_mock: HTTPXMock) -> None:
    """The error path of a file given as an id reads the token's spaces and the id's space."""
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/spaces",
        json=[{"name": "k2val-x", "spaceId": SPACE_ID}],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/spaces/{SPACE_ID}",
        json={"spaceId": SPACE_ID, "providers": [{"providerId": TARGET, "providerName": "site-a"}]},
        is_reusable=True,
        is_optional=True,
    )


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
async def test_the_description_follows_the_surface_rules(transfers_mcp: FastMCP) -> None:
    """design/10, gate 2: it says what it returns and what "scheduled" does not mean, names no
    other tool (one may not be offered alongside it) and cites no document the model cannot
    read."""
    async with Client(transfers_mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == TOOL)
    fields = " ".join(p["description"] for p in tool.inputSchema["properties"].values())
    text = " ".join(f"{tool.description} {fields}".split())  # one line, as a model reads it

    assert '`{"transferId": "<id>"}`' in text  # what it returns
    assert "Scheduled does not mean copied" in text and "the data arrive later" in text
    # the done-signal: the blocks at the target, not the transfer's status
    assert "can read completed before the copied blocks show" in text
    assert "re-check the target provider's blocks of the file until they cover all of it" in text
    assert "not by the transfer's status" in text
    assert "nothing keeps the data at the target afterwards" in text  # a copy, not a rule
    assert "given by its provider id, not its name" in text
    others = (ALL_KNOWN | FLAG_GATED) - {TOOL}
    assert [name for name in others if name in text] == []
    assert not re.search(r"\.md\b|design/|https?://|§|\bM-\d", text)


@pytest.mark.asyncio
async def test_a_schedule_answers_the_transfer_id(
    transfers_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    _the_spaces_are_read_if_asked(httpx_mock)
    httpx_mock.add_response(method="POST", url=f"{BASE}/transfers", json={"transferId": "t1"})

    result, text = await _call(transfers_mcp, FILE_ID)

    assert result.is_error is False
    assert result.structured_content == {"transferId": "t1", "note": SCHEDULED_NOTE}


@pytest.mark.asyncio
async def test_the_schedule_answer_says_scheduled_is_not_copied_and_names_no_tool(
    transfers_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """F2 (design/12): a model that read only the transfer's status answered "done". The result
    repeats what the description says, where the model reads it at the moment it acts, and
    names no tool, as the description does not: the one that reads the blocks may not be offered."""
    _the_spaces_are_read_if_asked(httpx_mock)
    httpx_mock.add_response(method="POST", url=f"{BASE}/transfers", json={"transferId": "t1"})

    result, text = await _call(transfers_mcp, FILE_ID)
    note = result.structured_content["note"]

    assert note.startswith("Scheduled, not yet copied")
    assert "target provider's blocks of the file" in note and "not by the transfer's status" in note
    assert "note" in text and "Scheduled, not yet copied" in text  # in the text a client shows too
    assert [name for name in (ALL_KNOWN | FLAG_GATED) if name in note] == []
    # consistent with the description: the same test of completion, in the same words
    async with Client(transfers_mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == TOOL)
    description = " ".join(tool.description.split())
    assert "judge the copy by those blocks, not by the transfer's status" in description
    assert "not by the transfer's status" in note


@pytest.mark.asyncio
async def test_a_providers_name_is_refused_before_any_transfer_and_without_onedatas_text(
    transfers_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """M-46 (design/12): the tool checks the target against the space's providers. Onedata
    answers a name with a message that says the space is not supported by that "provider",
    the opposite of the hint, and a model believed it. The refusal is made before any transfer
    is requested, and the model reads none of Onedata's text."""
    _the_spaces_are_read_if_asked(httpx_mock)

    result, text = await _call(transfers_mcp, FILE_ID, "site-a")

    assert result.is_error is True
    assert f"Pass its id '{TARGET}', not its name: 'site-a' is the name of a provider" in text
    assert f"site-a (id '{TARGET}')" in text and text.endswith("No replication was scheduled.")
    assert "not supported by provider" not in text and "Onedata" not in text
    assert HOST not in text
    assert [r for r in httpx_mock.get_requests() if r.method == "POST"] == []


@pytest.mark.asyncio
async def test_a_value_that_is_neither_a_name_nor_an_id_is_refused_before_any_transfer(
    transfers_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    _the_spaces_are_read_if_asked(httpx_mock)

    result, text = await _call(transfers_mcp, FILE_ID, "site-z")

    assert result.is_error is True
    assert "Cannot replicate to 'site-z': no provider with that id supports" in text
    assert f"site-a (id '{TARGET}')" in text and "Pass its id" not in text
    assert [r for r in httpx_mock.get_requests() if r.method == "POST"] == []


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
        (
            404,
            {"error": {"id": "notFound", "description": "not found"}},
            "did not say which argument it means",
        ),
        (
            400,
            {"error": {"id": "badValueIdentifier", "details": {"key": "fileId"}}},
            "No file is known",
        ),
        (403, {"error": {"id": "forbidden", "description": "no"}}, "may be mis-copied"),
        (400, {"error": {"id": "spaceNotSupportedBy"}}, "the file id may be mis-copied"),
        (500, {"error": {"id": "internalServerError"}}, "status=500"),
    ],
)
async def test_an_error_reaches_the_model_with_its_hint_and_no_host(
    transfers_mcp: FastMCP, httpx_mock: HTTPXMock, status: int, body: dict, needle: str
) -> None:
    _the_spaces_are_read_if_asked(httpx_mock)
    httpx_mock.add_response(method="POST", url=f"{BASE}/transfers", status_code=status, json=body)

    result, text = await _call(transfers_mcp, FILE_ID)

    assert result.is_error is True
    assert needle in text
    assert HOST not in text
