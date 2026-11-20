"""list_children, gate 1 of the promotion protocol (design/14): every error path a model can
trigger, paging, and the surface, through the API and through an MCP client.

The Onedata error bodies here are plausible shapes, not the 25.0 bodies: the swagger lists
only the statuses (400, 403, 404). The live test (test/live/test_files_live.py) prints the real
ones.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

import pytest
from fastmcp import Client, FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp.api import files
from onedata_mcp.modules import files as files_module
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED
from onedata_mcp.utils import OnedataApiError

API = "https://provider.example/api/v3/oneprovider"
DIR = "0000000000DiReCt0ry0123456"
PAGE = {"children": [], "isLast": True, "nextPageToken": None}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    httpx_mock.add_response(
        method="GET", url=f"{API}/spaces", json=[], is_reusable=True, is_optional=True
    )


@pytest.fixture
def mcp() -> FastMCP:
    server = FastMCP(name="onedata-mcp-test")
    files_module.register_module(server)
    return server


def lookup_url(path: str) -> str:
    return f"{API}/lookup-file-id/{quote(path, safe='')}"


def listings(httpx_mock: HTTPXMock) -> list:
    return [r for r in httpx_mock.get_requests() if r.url.path.endswith("/children")]


def onedata_error(status: int, error_id: str, errno: str | None = None, **details) -> dict:
    body: dict = {"error": {"id": error_id, "description": f"{error_id} (Onedata's own text)"}}
    if errno:
        details["errno"] = errno
    if details:
        body["error"]["details"] = details
    return {"status_code": status, "json": body}


async def call(mcp: FastMCP, arguments: dict) -> tuple[bool, str]:
    async with Client(mcp) as client:
        result = await client.call_tool("list_children", arguments, raise_on_error=False)
    return result.is_error, "\n".join(c.text for c in result.content if hasattr(c, "text"))


# --- paging: token and offset are exclusive ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_page_sends_offset_and_no_token(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=PAGE)

    await files.list_children(DIR, attributes=["name"], limit=10, offset=3)

    (sent,) = listings(httpx_mock)
    assert json.loads(sent.content) == {"limit": 10, "attributes": ["name"], "offset": 3}


@pytest.mark.asyncio
async def test_a_next_page_sends_the_token_and_no_offset(httpx_mock: HTTPXMock) -> None:
    """The swagger: offset "cannot be provided alongside the `token` parameter". Before, the
    tool always sent `offset: 0` with the token."""
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=PAGE)

    await files.list_children(DIR, attributes=["name"], limit=10, offset=0, token="tok-2")

    (sent,) = listings(httpx_mock)
    assert json.loads(sent.content) == {"limit": 10, "attributes": ["name"], "token": "tok-2"}


@pytest.mark.asyncio
async def test_a_token_with_a_nonzero_offset_is_refused_before_any_request(
    httpx_mock: HTTPXMock,
) -> None:
    with pytest.raises(ValueError, match=r"^Pass token or offset, not both"):
        await files.list_children(DIR, limit=10, offset=5, token="tok-2")

    assert httpx_mock.get_requests() == []


# --- the id goes into the path percent-encoded -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "encoded"), [("ab?c", "ab%3Fc"), ("ab#c", "ab%23c"), ("ab/x", "ab%2Fx")]
)
async def test_an_id_is_percent_encoded(httpx_mock: HTTPXMock, value: str, encoded: str) -> None:
    httpx_mock.add_response(method="GET", json=PAGE)

    await files.list_children(value, limit=10, offset=0)

    (sent,) = listings(httpx_mock)
    assert sent.url.raw_path.decode() == f"/api/v3/oneprovider/data/{encoded}/children"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", ".", ".."])
async def test_an_empty_id_or_a_dot_segment_is_refused(httpx_mock: HTTPXMock, value: str) -> None:
    """`/data/../children` would be `/children` at the API root."""
    with pytest.raises(ValueError, match=r"Give a path, /<space_name>/<path>, or a file id"):
        await files.list_children(value, limit=10, offset=0)

    assert listings(httpx_mock) == []


@pytest.mark.asyncio
async def test_a_deprecated_attribute_name_is_refused_before_any_request(
    httpx_mock: HTTPXMock,
) -> None:
    with pytest.raises(ValueError, match=r"file_id->fileId"):
        await files.list_children("/Alpha/d", attributes=["file_id"], limit=10, offset=0)

    assert httpx_mock.get_requests() == []


# --- error paths with hints ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_a_regular_file_says_only_a_directory_has_entries(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/a.dat"), json={"fileId": DIR})
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **onedata_error(400, "posix", "enotdir")
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children("/Alpha/a.dat", limit=10, offset=0)

    assert str(caught.value).startswith(
        "The path '/Alpha/a.dat' is not a directory: only a directory has entries to list."
    )


@pytest.mark.asyncio
async def test_a_file_on_the_way_of_the_path_names_a_component(httpx_mock: HTTPXMock) -> None:
    """`enotdir` while resolving `/Alpha/a.dat/sub`: a component is not a directory, which says
    nothing of `sub` itself."""
    httpx_mock.add_response(
        method="POST",
        url=lookup_url("/Alpha/a.dat/sub"),
        **onedata_error(400, "posix", "enotdir"),
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children("/Alpha/a.dat/sub", limit=10, offset=0)

    assert str(caught.value).startswith(
        "A component of path '/Alpha/a.dat/sub' before its last part is not a directory, so "
        "this provider cannot resolve the path."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "starts"),
    [
        (onedata_error(404, "notFound"), f"This provider reports nothing at file id '{DIR}'"),
        (
            onedata_error(400, "posix", "enoent"),
            f"This provider reports nothing at file id '{DIR}'",
        ),
        (
            onedata_error(400, "badValueIdentifier", key="id"),
            f"This provider rejected file id '{DIR}'",
        ),
        (
            onedata_error(400, "spaceNotSupportedBy"),
            f"This provider does not support the space of file id '{DIR}', so it cannot list",
        ),
        (
            {"status_code": 404, "json": {"error": {"description": "(Onedata's own text)"}}},
            f"This provider answered 404 for file id '{DIR}' without saying why",
        ),
        (onedata_error(403, "forbidden"), f"Not allowed to list file id '{DIR}'"),
        (onedata_error(400, "posix", "eacces"), f"Not allowed to list file id '{DIR}'"),
    ],
)
async def test_errors_say_what_to_do(httpx_mock: HTTPXMock, error: dict, starts: str) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    assert str(caught.value).startswith(starts)
    assert "(Onedata's own text)" in str(caught.value)


@pytest.mark.asyncio
async def test_a_path_that_names_nothing_says_so(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=lookup_url("/Alpha/archive"), **onedata_error(400, "posix", "enoent")
    )

    with pytest.raises(FileNotFoundError, match=r'Path "/Alpha/archive" not found'):
        await files.list_children("/Alpha/archive", limit=10, offset=0)


@pytest.mark.asyncio
async def test_a_bad_token_passes_onedatas_error_on(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{API}/data/{DIR}/children",
        **onedata_error(400, "badValueToken", key="token"),
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0, token="garbage")

    assert "badValueToken" in str(caught.value) and "rejected" not in str(caught.value)


@pytest.mark.asyncio
async def test_an_empty_directory_is_an_empty_last_page(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=PAGE)

    assert await files.list_children(DIR, limit=10, offset=0) == PAGE


# --- through the MCP client ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attributes_as_json_text_are_read_as_a_list(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=PAGE)

    is_error, text = await call(mcp, {"parent_id_or_path": DIR, "attributes": '["name", "type"]'})

    assert is_error is False, text
    (sent,) = listings(httpx_mock)
    assert json.loads(sent.content)["attributes"] == ["name", "type"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        ({"attributes": "name,type"}, "attributes must be a list of attribute names"),
        ({"offset": -1}, "greater than or equal to 0"),
        ({"offset": 501}, "less than or equal to 500"),
        ({"limit": 0}, "greater than or equal to 1"),
        ({"limit": 101}, "less than or equal to 100"),
        ({"offset": 10, "token": "tok-1"}, "Pass token or offset, not both"),
    ],
)
async def test_bad_arguments_are_refused_with_what_to_send(
    mcp: FastMCP, httpx_mock: HTTPXMock, arguments: dict, needle: str
) -> None:
    is_error, text = await call(mcp, {"parent_id_or_path": DIR, **arguments})

    assert is_error is True and needle in text
    assert listings(httpx_mock) == []


@pytest.mark.asyncio
async def test_a_second_page_through_the_client_sends_no_offset(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=PAGE)

    is_error, text = await call(mcp, {"parent_id_or_path": DIR, "token": "tok-10"})

    assert is_error is False, text
    (sent,) = listings(httpx_mock)
    assert "offset" not in json.loads(sent.content)


# --- the surface (gate 2) ----------------------------------------------------------------------


async def _tool(mcp: FastMCP):
    async with Client(mcp) as client:
        return next(t for t in await client.list_tools() if t.name == "list_children")


@pytest.mark.asyncio
async def test_the_description_follows_the_surface_rules(mcp: FastMCP) -> None:
    """design/10, gate 2: it says what it returns and how to page, lists the attribute names
    itself (it names no other tool, since the three file tools may be offered apart), and
    cites no document."""
    tool = await _tool(mcp)
    props = tool.inputSchema["properties"]
    text = f"{tool.description} " + " ".join(p["description"] for p in props.values())

    for needle in ("Returns one page", "isLast", "nextPageToken", "DIR for a directory"):
        assert needle in tool.description
    assert "posixPermissions" in props["attributes"]["description"]
    others = (ALL_KNOWN | FLAG_GATED) - {"list_children"}
    assert [name for name in others if re.search(rf"\b{name}\b", text)] == []
    assert not re.search(r"\.md\b|design/|research/|https?://|§|\bM-\d", text)
    assert (props["offset"]["minimum"], props["offset"]["maximum"]) == (0, 500)
    assert props["attributes"]["anyOf"][0]["maxItems"] == 64
    assert tool.annotations.readOnlyHint is True
