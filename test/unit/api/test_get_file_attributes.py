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
from onedata_mcp.utils import OnedataApiError

API = "https://provider.example/api/v3/oneprovider"
FID = "0000000000AbCdEf0123456789"  # opaque: the API function takes an id as given
SEEN_SPACE = "s0"  # the one space the token sees (the fixture below)
OTHER_SPACE = "9JO5__9nL1b01YEvwnft1r20gwgwlkIp19ND_n8hgxs"


def whole_id(
    space_id: str = SEEN_SPACE, uuid: str = "0cb444f249735948f20eaf93a2dfe54bch3e33"
) -> str:
    """A file id of the 25.0 shape: an 8-byte header whose sixth byte is the payload's length,
    then `guid#<uuid>#<space id>`, all in hex. What the tool takes from a model."""
    payload = f"guid#{uuid}#{space_id}".encode()
    return (bytes([0, 0, 0, 0, 0, len(payload), 0x52, 0x73]) + payload).hex().upper()


WHOLE = whole_id()


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
async def test_a_structured_not_found_says_the_provider_reports_nothing(
    httpx_mock: HTTPXMock, error: dict
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(FID)

    text = str(caught.value)
    assert text.startswith(f"This provider reports nothing at file id '{FID}'")
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
        OnedataApiError, match=r"^This provider rejected file id 'abc' as a file id\. Give"
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
@pytest.mark.parametrize("via", ["id", "path"])
async def test_an_unsupported_space_is_not_called_missing(httpx_mock: HTTPXMock, via: str) -> None:
    """`spaceNotSupportedBy` says this provider does not support the space; the space may still
    exist (the shared resolver of the stable tools reads it as "does not exist")."""
    body = {"error": {"id": "spaceNotSupportedBy", "details": {"spaceId": "s9"}}}
    if via == "id":
        target, shown = "dsadas", "file id 'dsadas'"
        httpx_mock.add_response(method="GET", url=f"{API}/data/dsadas", status_code=400, json=body)
    else:
        target, shown = "/Beta/a.dat", "path '/Beta/a.dat'"
        httpx_mock.add_response(method="POST", url=lookup_url(target), status_code=400, json=body)

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(target)

    text = str(caught.value)
    assert text.startswith(
        f"This provider does not support the space of {shown}, so it cannot read the "
        "attributes of it here; the space may still exist. Spaces this token sees at this "
        'provider: "Alpha".'
    )
    assert "does not exist" not in text and "spaceNotSupportedBy" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["id", "path"])
async def test_a_bare_404_gives_the_possible_causes_not_one(
    httpx_mock: HTTPXMock, via: str
) -> None:
    """A 404 without a structured body: the target may be missing, its space unsupported, or
    hidden from the token. Nothing more is claimed."""
    if via == "id":
        target, shown = FID, f"file id '{FID}'"
        httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", status_code=404, text="no")
    else:
        target, shown = "/Alpha/a.dat", "path '/Alpha/a.dat'"
        httpx_mock.add_response(
            method="POST", url=lookup_url(target), status_code=404, json={"error": {}}
        )

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(target)

    text = str(caught.value)
    assert text.startswith(f"This provider answered 404 for {shown} without saying why")
    assert "space may not be supported by this provider" in text and "token's access" in text
    assert "reports nothing" not in text and "not found" not in text.split("[")[0]
    assert "status=404" in text  # the underlying error is kept


@pytest.mark.asyncio
async def test_a_file_on_the_way_names_a_component_not_the_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=lookup_url("/Alpha/a.dat/x"),
        **onedata_error(400, "posix", "enotdir"),
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes("/Alpha/a.dat/x")

    assert str(caught.value).startswith(
        "A component of path '/Alpha/a.dat/x' before its last part is not a directory"
    )


@pytest.mark.asyncio
async def test_a_server_error_passes_on_unchanged(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", **onedata_error(500, "internal"))

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(FID)

    assert str(caught.value).startswith("Onedata API request failed: GET")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
@pytest.mark.parametrize("error", [("notFound", None), ("posix", "enoent")], ids=["id", "errno"])
@pytest.mark.parametrize("via", ["id", "path"])
async def test_a_5xx_that_mentions_a_missing_item_keeps_the_uncertainty(
    httpx_mock: HTTPXMock, via: str, error: tuple, status: int
) -> None:
    """The same body on a 5xx says the provider failed, not that the file is missing: never
    "not found" (for a path, never `Path ... not found`), never "reports nothing"."""
    failure = onedata_error(status, *error)
    if via == "id":
        target, shown = FID, f"file id '{FID}'"
        httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", **failure)
    else:
        target, shown = "/Alpha/a.dat", "path '/Alpha/a.dat'"
        httpx_mock.add_response(method="POST", url=lookup_url(target), **failure)

    with pytest.raises(OnedataApiError) as caught:  # not FileNotFoundError, which is not one
        await files.get_file_attributes(target)

    text = str(caught.value)
    hint = text.split(" [", 1)[0]
    assert hint.startswith(
        f"This provider answered {status} and mentioned a missing item: the provider failed, "
        f"and {shown} may also not exist."
    )
    assert "reports nothing" not in hint and "not found" not in hint
    assert f"status={status}" in text  # the underlying error is kept


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        onedata_error(500, "forbidden"),
        onedata_error(500, "posix", "eacces"),
        onedata_error(503, "spaceNotSupportedBy"),
        onedata_error(500, "posix", "enotdir"),
    ],
    ids=["forbidden", "eacces", "unsupported-space", "enotdir"],
)
async def test_a_5xx_never_gets_a_specific_diagnosis(httpx_mock: HTTPXMock, error: dict) -> None:
    """A permission, a missing space and a path component that is not a directory are claims
    about the caller's target. On a 5xx the provider only said that it failed."""
    httpx_mock.add_response(method="GET", url=f"{API}/data/{FID}", **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(FID)

    assert str(caught.value).startswith("Onedata API request failed: GET")


# --- through the MCP client, as the model sees it ----------------------------------------------


@pytest.mark.asyncio
async def test_a_list_sent_as_json_text_is_read_as_that_list(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """Qwen3-Coder sent `attributes` as '["size","mtime"]' and got pydantic's list_type error."""
    httpx_mock.add_response(method="GET", url=f"{API}/data/{WHOLE}", json={"size": 3, "mtime": 1})

    is_error, text = await call(mcp, {"file_id_or_path": WHOLE, "attributes": '["size","mtime"]'})

    assert is_error is False, text
    (sent,) = attrs_requests(httpx_mock)
    assert json.loads(sent.content) == {"attributes": ["size", "mtime"]}


@pytest.mark.asyncio
async def test_the_panels_direct_call_path_reads_json_text_too(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """The panel calls `app.call_tool` directly (benchmark/k2val/recording.py), not via a client."""
    httpx_mock.add_response(method="GET", url=f"{API}/data/{WHOLE}", json={"size": 3})

    result = await mcp.call_tool(
        "get_file_attributes", {"file_id_or_path": WHOLE, "attributes": '["size"]'}
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
async def test_a_long_text_is_refused_before_it_is_decoded(
    mcp: FastMCP, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_decoding(text):
        raise AssertionError("decoded a text longer than the bound")

    monkeypatch.setattr(files_module.json, "loads", no_decoding)
    text = "[" * (files_module.MAX_ATTRIBUTES_TEXT + 1)

    is_error, answer = await call(mcp, {"file_id_or_path": FID, "attributes": text})

    assert is_error is True and "attributes must be a list of attribute names" in answer
    assert attrs_requests(httpx_mock) == []


@pytest.mark.asyncio
async def test_a_text_nested_too_deeply_to_decode_gets_the_same_hint(
    mcp: FastMCP, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound keeps the nesting below the decoder's limit here; another interpreter's limit
    may be lower, so a RecursionError is read as a bad value too."""

    def too_deep(text):
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")

    monkeypatch.setattr(files_module.json, "loads", too_deep)

    is_error, answer = await call(mcp, {"file_id_or_path": FID, "attributes": "[[[[]]]]"})

    assert is_error is True and "attributes must be a list of attribute names" in answer
    assert "RecursionError" not in answer and attrs_requests(httpx_mock) == []


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
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{WHOLE}", **onedata_error(404, "notFound")
    )

    is_error, text = await call(mcp, {"file_id_or_path": WHOLE})

    assert is_error is True
    assert f"This provider reports nothing at file id {files._shown(WHOLE)}" in text
    assert "provider.example" not in text  # host-free


# --- an id the model copied: refused when it cannot be whole, a refusal may be a bad copy -----


def mis_copies() -> dict[str, str]:
    """What a model does to a copied id (gate 4): drops, changes and doubles characters."""
    return {
        "truncated, a digit short": WHOLE[:-1],
        "truncated, a byte short": WHOLE[:-2],
        "a digit dropped in the middle": WHOLE[:90] + WHOLE[91:],
        "a byte dropped in the middle": WHOLE[:90] + WHOLE[92:],
        "a character changed to a non-hex one": WHOLE[:60] + "g" + WHOLE[61:],
        "the marker changed": WHOLE[:16] + "7" + WHOLE[17:],
        "doubled": WHOLE + WHOLE,
        "a digit added": WHOLE[:90] + "a" + WHOLE[90:],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(mis_copies()))
async def test_an_id_that_cannot_be_whole_is_refused_before_any_request(
    mcp: FastMCP, httpx_mock: HTTPXMock, name: str
) -> None:
    copied = mis_copies()[name]

    is_error, text = await call(mcp, {"file_id_or_path": copied})
    with pytest.raises(ValueError) as caught:
        await files.get_file_attributes(copied, copied_id=True)

    for shown in (text, str(caught.value)):
        assert files._shown(copied) in shown
        assert "is not a complete file id; copy it exactly from the earlier answer" in shown
        assert "or give the path" in shown
    assert is_error is True
    assert provider_requests(httpx_mock) == []  # no lookup, no read


@pytest.mark.asyncio
async def test_a_whole_id_and_a_path_are_read_as_before(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/a.dat"), json={"fileId": WHOLE})
    httpx_mock.add_response(method="GET", url=f"{API}/data/{WHOLE}", json={"name": "a"})
    httpx_mock.add_response(method="GET", url=f"{API}/data/{WHOLE}", json={"name": "a"})

    by_path, _ = await call(mcp, {"file_id_or_path": "/Alpha/a.dat", "attributes": ["name"]})
    by_id, _ = await call(mcp, {"file_id_or_path": WHOLE, "attributes": ["name"]})

    assert (by_path, by_id) == (False, False)


@pytest.mark.asyncio
async def test_the_callers_that_pass_ids_from_onedata_keep_their_handling(
    httpx_mock: HTTPXMock,
) -> None:
    """Readiness and the stable download tool call the API function with an id Onedata gave
    them, and not `copied_id`: nothing is refused for its shape, and a refusal keeps its hint."""
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{FID}", **onedata_error(403, "forbidden")
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.get_file_attributes(FID)  # `FID` is not even a whole id

    assert str(caught.value).startswith(
        f"Not allowed to read the attributes of file id '{FID}': the token has no access to it. "
        "Calling again will not help."
    )
    assert "mis-copied" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [onedata_error(403, "forbidden"), onedata_error(400, "posix", "eacces")],
    ids=["403", "eacces"],
)
async def test_a_permission_refusal_of_a_copied_id_says_it_may_be_mis_copied(
    mcp: FastMCP, httpx_mock: HTTPXMock, error: dict
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{WHOLE}", **error)

    is_error, text = await call(mcp, {"file_id_or_path": WHOLE})

    assert is_error is True
    assert (
        f"Could not read the attributes of file id {files._shown(WHOLE)}: the id may be "
        "mis-copied (an id with a missing or changed character is refused like this), or the "
        "token may have no access to the file. Copy the id exactly from the earlier answer, "
        "or give the path, /<space_name>/<path>."
    ) in text
    assert "will not help" not in text and "Not allowed" not in text
    assert "not one this token sees" not in text  # its space is the one the token sees
    assert "(Onedata's own text)" in text


@pytest.mark.asyncio
async def test_a_copied_id_whose_space_the_token_does_not_see_says_so(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    unseen = whole_id(OTHER_SPACE)
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{unseen}", **onedata_error(403, "forbidden")
    )

    is_error, text = await call(mcp, {"file_id_or_path": unseen})

    assert "the id may be mis-copied" in text
    assert "The space in the id is not one this token sees at this provider." in text


@pytest.mark.asyncio
async def test_an_unsupported_space_of_a_copied_id_says_it_may_be_mis_copied(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    unseen = whole_id(OTHER_SPACE)
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{unseen}", **onedata_error(400, "spaceNotSupportedBy")
    )

    is_error, text = await call(mcp, {"file_id_or_path": unseen})

    assert (
        f"Could not read the attributes of file id {files._shown(unseen)}: this provider "
        "reports that it does not support the space in the id. The id may be mis-copied"
    ) in text
    assert "copy it exactly from the earlier answer, or give the path" in text
    assert "The space in the id is not one this token sees at this provider." in text
    assert 'Spaces this token sees at this provider: "Alpha".' in text
    assert "so it cannot read" not in text  # the old claim about the provider


@pytest.mark.asyncio
async def test_the_refusals_of_a_path_keep_their_hints_through_the_tool_too(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    """A path that resolved is not a bad copy; the tool's id handling does not touch it."""
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/a.dat"), json={"fileId": FID})
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{FID}", **onedata_error(403, "forbidden")
    )

    is_error, text = await call(mcp, {"file_id_or_path": "/Alpha/a.dat"})

    assert (
        "Not allowed to read the attributes of path '/Alpha/a.dat': the token has no access to "
        "it. Calling again will not help."
    ) in text
    assert "mis-copied" not in text


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
