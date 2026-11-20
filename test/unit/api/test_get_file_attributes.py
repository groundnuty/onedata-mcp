"""get_file_attributes, gate 1 of the promotion protocol (design/13): every error path a model
can trigger, through the API and through an MCP client, as the model sees it.

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
from onedata_mcp.utils import OnedataApiError, OnedataInvalidSpaceError

API = "https://provider.example/api/v3/oneprovider"
FID = "0000000000AbCdEf0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    httpx_mock.add_response(
        method="GET",
        url=f"{API}/spaces",
        json=[{"name": "Alpha", "spaceId": "s0"}],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/s0",
        json={"name": "Alpha", "spaceId": "s0"},
        is_reusable=True,
        is_optional=True,
    )


@pytest.fixture
def mcp() -> FastMCP:
    server = FastMCP(name="onedata-mcp-test")
    files_module.register_module(server)
    return server


def lookup_url(path: str) -> str:
    return f"{API}/lookup-file-id/{quote(path, safe='')}"


def provider_requests(httpx_mock: HTTPXMock) -> list:
    return [r for r in httpx_mock.get_requests() if r.url.host == "provider.example"]


def attrs_requests(httpx_mock: HTTPXMock) -> list:
    return [r for r in provider_requests(httpx_mock) if "/data/" in r.url.path]


def onedata_error(status: int, error_id: str, errno: str | None = None, **details) -> dict:
    body: dict = {"error": {"id": error_id, "description": f"{error_id} (Onedata's own text)"}}
    if errno:
        details["errno"] = errno
    if details:
        body["error"]["details"] = details
    return {"status_code": status, "json": body}


async def call(mcp: FastMCP, arguments: dict) -> tuple[bool, str]:
    async with Client(mcp) as client:
        result = await client.call_tool("get_file_attributes", arguments, raise_on_error=False)
    return result.is_error, "\n".join(c.text for c in result.content if hasattr(c, "text"))


# --- success ---------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("attributes", [None, []])
async def test_without_attributes_the_default_set_is_asked_for(
    httpx_mock: HTTPXMock, attributes
) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/a.dat"), json={"fileId": FID})
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", json={"name": "a.dat"})

    assert await files.get_file_attributes("/Alpha/a.dat", attributes=attributes) == {
        "name": "a.dat"
    }

    (sent,) = attrs_requests(httpx_mock)
    assert json.loads(sent.content) == {"attributes": list(files.DEFAULT_FILE_ATTRIBUTE_KEYS)}


@pytest.mark.asyncio
async def test_an_id_is_used_as_is_without_a_lookup(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", json={"posixPermissions": "644"})

    out = await files.get_file_attributes(FID, attributes=["posixPermissions"])

    assert out == {"posixPermissions": "644"}
    assert [r.method for r in provider_requests(httpx_mock)] == ["GET"]


# --- the id goes into the path percent-encoded ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "encoded"),
    [("ab?c", "ab%3Fc"), ("ab#c", "ab%23c"), ("ab/children", "ab%2Fchildren"), ("a b", "a%20b")],
)
async def test_an_id_is_percent_encoded_so_it_cannot_change_the_request(
    httpx_mock: HTTPXMock, value: str, encoded: str
) -> None:
    """Unencoded, `?` and `#` cut the path short (the attributes of another file come back) and
    `/` turns the call into another endpoint (`/data/ab/children`)."""
    httpx_mock.add_response(method="GET", json={"name": "x"})

    await files.get_file_attributes(value, attributes=["name"])

    (sent,) = attrs_requests(httpx_mock)
    assert sent.url.raw_path.decode() == f"/api/v3/oneprovider/data/{encoded}"
    assert sent.url.query == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   ", ".", ".."])
async def test_an_empty_id_or_a_dot_segment_is_refused_before_any_request(
    httpx_mock: HTTPXMock, value: str
) -> None:
    """`/data/..` would be the API root, `/data/.` the `/data` collection."""
    with pytest.raises(ValueError, match=r"Give a path, /<space_name>/<path>, or a file id"):
        await files.get_file_attributes(value)

    assert attrs_requests(httpx_mock) == []


# --- attribute names ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deprecated_attribute_name_is_refused_with_its_new_name(
    httpx_mock: HTTPXMock,
) -> None:
    with pytest.raises(ValueError, match=r"mode->posixPermissions"):
        await files.get_file_attributes(FID, attributes=["name", "mode"])

    assert provider_requests(httpx_mock) == []


@pytest.mark.asyncio
async def test_an_unknown_attribute_name_passes_onedatas_error_on_unchanged(
    httpx_mock: HTTPXMock,
) -> None:
    """Onedata's 400 names the bad value and the allowed ones; it is not about the id, so it
    gets no id hint."""
    httpx_mock.add_response(
        method="GET",
        url=f"{API}/data/{FID}",
        **onedata_error(400, "badValueListNotAllowed", key="attributes"),
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(FID, attributes=["owner"])

    text = str(caught.value)
    assert "badValueListNotAllowed" in text
    assert "Onedata rejected" not in text and "Nothing exists" not in text


# --- error paths with hints ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        onedata_error(404, "notFound"),
        onedata_error(400, "posix", "enoent"),
        onedata_error(404, "posix", "enoent"),
    ],
)
async def test_an_id_that_names_nothing_says_how_to_name_a_file(
    httpx_mock: HTTPXMock, error: dict
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(FID)

    text = str(caught.value)
    assert text.startswith(f"Nothing exists at file id '{FID}'")
    assert "Give a path, /<space_name>/<path>, or a file id." in text
    assert "(Onedata's own text)" in text  # Onedata's message follows the hint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        onedata_error(400, "badValueIdentifier", key="id"),
        onedata_error(400, "badValueString", key="id"),
    ],
)
async def test_a_malformed_id_is_rejected_with_the_same_advice(
    httpx_mock: HTTPXMock, error: dict
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/abc", **error)

    with pytest.raises(
        OnedataApiError, match=r"^Onedata rejected file id 'abc' as a file id\. Give"
    ):
        await files.get_file_attributes("abc")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [onedata_error(403, "forbidden"), onedata_error(400, "posix", "eacces")],
)
async def test_no_access_says_a_retry_will_not_help(httpx_mock: HTTPXMock, error: dict) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/a.dat"), json={"fileId": FID})
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes("/Alpha/a.dat")

    assert str(caught.value).startswith(
        "Not allowed to read the attributes of path '/Alpha/a.dat': the token has no access to "
        "it. Calling again will not help."
    )


@pytest.mark.asyncio
async def test_a_path_that_names_nothing_says_so(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=lookup_url("/Alpha/missing.dat"), **onedata_error(400, "posix", "enoent")
    )

    with pytest.raises(FileNotFoundError, match=r'Path "/Alpha/missing.dat" not found'):
        await files.get_file_attributes("/Alpha/missing.dat")


@pytest.mark.asyncio
async def test_a_lookup_refused_for_access_gets_the_access_hint(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=lookup_url("/Alpha/a.dat"), **onedata_error(403, "forbidden")
    )

    with pytest.raises(OnedataApiError, match=r"^Not allowed to read the attributes of path"):
        await files.get_file_attributes("/Alpha/a.dat")


@pytest.mark.asyncio
async def test_an_id_of_an_unsupported_space_lists_the_spaces(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{API}/data/dsadas",
        status_code=400,
        json={"error": {"id": "spaceNotSupportedBy", "details": {"spaceId": "dsadas"}}},
    )

    with pytest.raises(OnedataInvalidSpaceError, match=r'Available spaces: "Alpha"\.'):
        await files.get_file_attributes("dsadas")


@pytest.mark.asyncio
async def test_a_server_error_passes_on_unchanged(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", **onedata_error(500, "internal"))

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(FID)

    assert str(caught.value).startswith("Onedata API request failed: GET")


# --- through the MCP client, as the model sees it ----------------------------------------------


@pytest.mark.asyncio
async def test_a_list_sent_as_json_text_is_read_as_that_list(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """Qwen3-Coder sent `attributes` as '["size","mtime"]' and got pydantic's list_type error."""
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", json={"size": 3, "mtime": 1})

    is_error, text = await call(mcp, {"file_id_or_path": FID, "attributes": '["size","mtime"]'})

    assert is_error is False, text
    (sent,) = attrs_requests(httpx_mock)
    assert json.loads(sent.content) == {"attributes": ["size", "mtime"]}


@pytest.mark.asyncio
async def test_the_panels_direct_call_path_reads_json_text_too(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """The panel calls `app.call_tool` directly (benchmark/k2val/recording.py), not via a client."""
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", json={"size": 3})

    result = await mcp.call_tool(
        "get_file_attributes", {"file_id_or_path": FID, "attributes": '["size"]'}
    )

    assert result.structured_content == {"size": 3}
    (sent,) = attrs_requests(httpx_mock)
    assert json.loads(sent.content) == {"attributes": ["size"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["size", "size,mtime", "[size, mtime]", "[1, 2]", '{"size": true}', '"size"']
)
async def test_a_string_that_is_not_a_json_list_of_names_is_refused_with_what_to_send(
    mcp: FastMCP, httpx_mock: HTTPXMock, value: str
) -> None:
    is_error, text = await call(mcp, {"file_id_or_path": FID, "attributes": value})

    assert is_error is True
    assert "attributes must be a list of attribute names" in text
    assert '["size", "mtime"]' in text
    assert attrs_requests(httpx_mock) == []


@pytest.mark.asyncio
async def test_more_than_64_names_are_refused(mcp: FastMCP, httpx_mock: HTTPXMock) -> None:
    names = [f"xattr.k{i}" for i in range(65)]

    is_error, text = await call(mcp, {"file_id_or_path": FID, "attributes": names})

    assert is_error is True and "at most 64" in text
    assert attrs_requests(httpx_mock) == []


@pytest.mark.asyncio
async def test_the_error_reaches_the_model_with_the_hint(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", **onedata_error(404, "notFound"))

    is_error, text = await call(mcp, {"file_id_or_path": FID})

    assert is_error is True
    assert f"Nothing exists at file id '{FID}'" in text
    assert "provider.example" not in text  # host-free


# --- the surface (gate 2) ----------------------------------------------------------------------


async def _tool(mcp: FastMCP):
    async with Client(mcp) as client:
        return next(t for t in await client.list_tools() if t.name == "get_file_attributes")


@pytest.mark.asyncio
async def test_the_published_schema_is_still_a_list_of_strings(mcp: FastMCP) -> None:
    """The JSON-text reading is a tolerance, not a second accepted type."""
    schema = (await _tool(mcp)).inputSchema["properties"]["attributes"]

    assert schema["anyOf"] == [
        {"items": {"type": "string"}, "maxItems": 64, "type": "array"},
        {"type": "null"},
    ]


@pytest.mark.asyncio
async def test_the_description_follows_the_surface_rules(mcp: FastMCP) -> None:
    """design/10, gate 2: it says what it returns, names no other tool and cites no document."""
    tool = await _tool(mcp)
    fields = " ".join(p["description"] for p in tool.inputSchema["properties"].values())
    text = f"{tool.description} {fields}"

    assert "Returns an object that maps each requested attribute name to its value" in text
    assert "posixPermissions" in text and "directory" in text
    others = (ALL_KNOWN | FLAG_GATED) - {"get_file_attributes"}
    assert [name for name in others if re.search(rf"\b{name}\b", text)] == []
    assert not re.search(r"\.md\b|design/|research/|https?://|§|\bM-\d", text)
    assert tool.annotations.readOnlyHint is True
