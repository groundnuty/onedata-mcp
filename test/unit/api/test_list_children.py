"""list_children, gate 1 of the promotion protocol (design/14): every error path a model can
trigger, paging, and the surface, through the API and through an MCP client.

The Onedata error bodies here are plausible shapes, not the 25.0 bodies: the swagger lists
only the statuses (400, 403, 404). The live test (test/live/test_files_live.py) prints the real
ones.
"""

from __future__ import annotations

import json
import re
import sys
from urllib.parse import quote

import httpx
import pytest
from fastmcp import Client, FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp.api import files
from onedata_mcp.modules import files as files_module
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED
from onedata_mcp.utils import OnedataApiError

API = "https://provider.example/api/v3/oneprovider"
SPACE_ID = "9JO5__9nL1b01YEvwnft1r20gwgwlkIp19ND_n8hgxs"


def whole_id(uuid: str = "0cb444f249735948f20eaf93a2dfe54bch3e33", space_id: str = SPACE_ID) -> str:
    """A file id of the 25.0 shape: an 8-byte header whose sixth byte is the payload's length,
    then `guid#<uuid>#<space id>`, all in hex."""
    payload = f"guid#{uuid}#{space_id}".encode()
    return (bytes([0, 0, 0, 0, 0, len(payload), 0x52, 0x73]) + payload).hex().upper()


DIR = whole_id()
PAGE = {"children": [], "isLast": True, "nextPageToken": None}
TOKEN_SPACES: list[dict] = []  # what `GET /spaces` answers: the token's spaces; a test sets it


def _token_spaces(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=TOKEN_SPACES)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    httpx_mock.add_callback(
        _token_spaces, method="GET", url=f"{API}/spaces", is_reusable=True, is_optional=True
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


@pytest.mark.parametrize(
    ("value", "encoded"), [("ab?c", "ab%3Fc"), ("ab#c", "ab%23c"), ("ab/x", "ab%2Fx")]
)
def test_an_id_is_percent_encoded_into_its_path_segment(value: str, encoded: str) -> None:
    """The last defence: `?` and `#` would cut the request path short and `/` would add
    segments. No such value reaches it through the tool, which refuses it first (below)."""
    assert files._id_in_path(value, value) == encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["ab?c", "ab#c", "ab/x"])
async def test_an_id_with_url_characters_is_refused_before_any_request(
    httpx_mock: HTTPXMock, value: str
) -> None:
    with pytest.raises(ValueError, match=r"is not a complete file id|Paths must start with"):
        await files.list_children(value, limit=10, offset=0)

    assert httpx_mock.get_requests() == []


def mis_copies() -> dict[str, str]:
    """What a model does to a copied id (gate 4): drops, changes and doubles characters."""
    return {
        "truncated, a digit short": DIR[:-1],
        "truncated, a byte short": DIR[:-2],
        "a digit dropped in the middle": DIR[:90] + DIR[91:],
        "a byte dropped in the middle": DIR[:90] + DIR[92:],
        "a character changed to a non-hex one": DIR[:60] + "g" + DIR[61:],
        "doubled": DIR + DIR,
        "a digit added": DIR[:90] + "a" + DIR[90:],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(mis_copies()))
async def test_an_id_that_cannot_be_whole_is_refused_before_any_request(
    httpx_mock: HTTPXMock, mcp: FastMCP, name: str
) -> None:
    copied = mis_copies()[name]

    with pytest.raises(ValueError) as caught:
        await files.list_children(copied, limit=10, offset=0)
    is_error, text = await call(mcp, {"parent_id_or_path": copied})

    for shown in (str(caught.value), text):
        assert files._shown(copied) in shown
        assert "is not a complete file id; copy it exactly from the earlier answer" in shown
        assert "or give the path" in shown
    assert is_error is True
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_a_path_is_not_taken_for_an_id_and_a_whole_id_is_sent(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/d"), json={"fileId": DIR})
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=PAGE)
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=PAGE)

    await files.list_children("/Alpha/d", limit=10, offset=0)
    await files.list_children(DIR, limit=10, offset=0)

    assert len(listings(httpx_mock)) == 2


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


# --- 25.0 lists a regular file as its own only entry (it does not answer enotdir) ----------------

A_FILE = "/Alpha/seed.txt"
FILE_ENTRY = {"type": "REG", "size": 5, "path": A_FILE, "name": "seed.txt"}
FILE_IS_NOT_A_DIRECTORY = (
    "'/Alpha/seed.txt' is a regular file, not a directory; it has no children."
)


def children_page(*entries: dict, last: bool = True, token: str | None = None) -> dict:
    return {"json": {"children": list(entries), "isLast": last, "nextPageToken": token}}


def type_reads(httpx_mock: HTTPXMock) -> list:
    """The reads of the target's own type: `GET /data/{id}`, not `/children`."""
    return [
        r
        for r in httpx_mock.get_requests()
        if r.method == "GET" and r.url.path.endswith(f"/data/{DIR}")
    ]


@pytest.mark.asyncio
async def test_a_regular_file_listed_as_its_own_only_child_is_refused(
    httpx_mock: HTTPXMock,
) -> None:
    """The entry's `path` is the requested path: a directory never lists itself, so no further
    request is needed."""
    httpx_mock.add_response(method="POST", url=lookup_url(A_FILE), json={"fileId": DIR})
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **children_page(FILE_ENTRY)
    )

    with pytest.raises(NotADirectoryError) as caught:
        await files.list_children(A_FILE, limit=10, offset=0)

    assert str(caught.value) == FILE_IS_NOT_A_DIRECTORY
    assert type_reads(httpx_mock) == []


@pytest.mark.asyncio
async def test_a_file_given_by_id_is_told_from_a_one_file_directory_by_its_type(
    httpx_mock: HTTPXMock,
) -> None:
    """No requested path to compare with: the target's own type decides."""
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **children_page(FILE_ENTRY)
    )
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}", json={"type": "REG"})

    with pytest.raises(NotADirectoryError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    assert str(caught.value) == (
        f"{files._shown(DIR)} is a regular file, not a directory; it has no children."
    )


@pytest.mark.asyncio
async def test_a_file_is_found_without_the_path_attribute_too(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url(A_FILE), json={"fileId": DIR})
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **children_page({"name": "seed.txt"})
    )
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}", json={"type": "REG"})

    with pytest.raises(NotADirectoryError) as caught:
        await files.list_children(A_FILE, attributes=["name"], limit=10, offset=0)

    assert str(caught.value) == FILE_IS_NOT_A_DIRECTORY


@pytest.mark.asyncio
async def test_a_directory_with_one_file_is_listed_without_a_type_read_when_the_path_differs(
    httpx_mock: HTTPXMock,
) -> None:
    """A directory never lists itself: an entry whose path is not the requested one is its
    child, and nothing more is read."""
    entry = {"type": "REG", "path": "/Alpha/d/seed.txt", "name": "seed.txt"}
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/d"), json={"fileId": DIR})
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", **children_page(entry))

    page = await files.list_children("/Alpha/d", limit=10, offset=0)

    assert page["children"] == [entry]
    assert type_reads(httpx_mock) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", ["/Alpha/seed.txt/", "/Alpha//seed.txt", "/Alpha/./seed.txt"])
async def test_the_requested_path_is_compared_in_its_canonical_form(
    httpx_mock: HTTPXMock, asked: str
) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url(asked), json={"fileId": DIR})
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **children_page(FILE_ENTRY)
    )

    with pytest.raises(NotADirectoryError) as caught:
        await files.list_children(asked, limit=10, offset=0)

    assert str(caught.value) == f"{asked!r} is a regular file, not a directory; it has no children."
    assert type_reads(httpx_mock) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["DIR", "REG"])
async def test_a_single_untyped_entry_without_a_path_is_told_by_the_targets_type(
    httpx_mock: HTTPXMock, kind: str
) -> None:
    entry = {"name": "seed.txt"}
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/d"), json={"fileId": DIR})
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", **children_page(entry))
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}", json={"type": kind})

    if kind == "REG":
        with pytest.raises(NotADirectoryError):
            await files.list_children("/Alpha/d", limit=10, offset=0)
    else:
        assert (await files.list_children("/Alpha/d", limit=10, offset=0))["children"] == [entry]
    assert len(type_reads(httpx_mock)) == 1


@pytest.mark.asyncio
async def test_a_directory_with_one_subdirectory_is_listed_without_a_type_read(
    httpx_mock: HTTPXMock,
) -> None:
    """A regular file's own entry is of type REG: another type cannot be one."""
    entry = {"type": "DIR", "path": "/Alpha/d/sub", "name": "sub"}
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/d"), json={"fileId": DIR})
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", **children_page(entry))

    page = await files.list_children("/Alpha/d", limit=10, offset=0)

    assert page["children"] == [entry]
    assert type_reads(httpx_mock) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        children_page(),
        children_page({"type": "REG", "name": "a"}, {"type": "REG", "name": "b"}),
    ],
    ids=["empty", "two-entries"],
)
async def test_a_page_without_exactly_one_entry_is_not_a_file(
    httpx_mock: HTTPXMock, page: dict
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", **page)

    result = await files.list_children(DIR, limit=10, offset=0)

    assert result["children"] == page["json"]["children"]
    assert type_reads(httpx_mock) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("paging", [{"offset": 10}, {"offset": 0, "token": "t1"}])
async def test_a_later_page_with_one_entry_is_not_taken_for_a_file(
    httpx_mock: HTTPXMock, paging: dict
) -> None:
    """The last page of a long listing may hold one entry; a file only lists itself first."""
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **children_page(FILE_ENTRY)
    )

    result = await files.list_children(DIR, limit=10, **paging)

    assert result["children"] == [FILE_ENTRY]
    assert type_reads(httpx_mock) == []


@pytest.mark.asyncio
async def test_a_type_that_cannot_be_read_leaves_the_listing_as_it_is(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **children_page({"name": "seed.txt"})
    )
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}", **onedata_error(500, "internal"))

    result = await files.list_children(DIR, limit=10, offset=0)

    assert result["children"] == [{"name": "seed.txt"}]


@pytest.mark.asyncio
async def test_the_model_reads_that_a_regular_file_has_no_children(
    mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url(A_FILE), json={"fileId": DIR})
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **children_page(FILE_ENTRY)
    )

    is_error, text = await call(mcp, {"parent_id_or_path": A_FILE})

    assert is_error is True
    assert FILE_IS_NOT_A_DIRECTORY in text
    assert "provider.example" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502])
@pytest.mark.parametrize("error", [("notFound", None), ("posix", "enoent")], ids=["id", "errno"])
async def test_a_5xx_that_mentions_a_missing_item_keeps_the_uncertainty(
    httpx_mock: HTTPXMock, error: tuple, status: int
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **onedata_error(status, *error)
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    hint = str(caught.value).split(" [", 1)[0]
    assert hint.startswith(
        f"This provider answered {status} and mentioned a missing item: the provider failed, "
        f"and file id {files._shown(DIR)} may also not exist."
    )
    assert "reports nothing" not in hint


@pytest.mark.asyncio
async def test_a_5xx_with_enotdir_does_not_say_the_target_is_not_a_directory(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **onedata_error(500, "posix", "enotdir")
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    assert str(caught.value).startswith("Onedata API request failed: GET")


@pytest.mark.asyncio
async def test_a_5xx_with_enoent_on_the_path_lookup_is_not_a_missing_path(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="POST", url=lookup_url("/Alpha/d"), **onedata_error(500, "posix", "enoent")
    )

    with pytest.raises(OnedataApiError) as caught:  # not FileNotFoundError
        await files.list_children("/Alpha/d", limit=10, offset=0)

    assert str(caught.value).startswith(
        "This provider answered 500 and mentioned a missing item: the provider failed, and "
        "path '/Alpha/d' may also not exist."
    )
    assert listings(httpx_mock) == []


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
        (
            onedata_error(404, "notFound"),
            f"This provider reports nothing at file id {files._shown(DIR)}",
        ),
        (
            onedata_error(400, "posix", "enoent"),
            f"This provider reports nothing at file id {files._shown(DIR)}",
        ),
        (
            onedata_error(400, "badValueIdentifier", key="id"),
            f"This provider rejected file id {files._shown(DIR)}",
        ),
        (
            onedata_error(400, "spaceNotSupportedBy"),
            f"Could not list file id {files._shown(DIR)}: this provider reports that it does "
            "not support the space in the id. The id may be mis-copied",
        ),
        (
            {"status_code": 404, "json": {"error": {"description": "(Onedata's own text)"}}},
            f"This provider answered 404 for file id {files._shown(DIR)} without saying why",
        ),
        (
            onedata_error(403, "forbidden"),
            f"Could not list file id {files._shown(DIR)}: the id may be mis-copied",
        ),
        (
            onedata_error(400, "posix", "eacces"),
            f"Could not list file id {files._shown(DIR)}: the id may be mis-copied",
        ),
    ],
)
async def test_errors_say_what_to_do(httpx_mock: HTTPXMock, error: dict, starts: str) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    assert str(caught.value).startswith(starts)
    assert "(Onedata's own text)" in str(caught.value)


# --- an id the caller copied: a refusal may only mean a bad copy ------------------------------


def _the_token_sees(monkeypatch: pytest.MonkeyPatch, *space_ids: str) -> None:
    spaces = [{"name": f"Space{i}", "spaceId": sid} for i, sid in enumerate(space_ids)]
    monkeypatch.setattr(sys.modules[__name__], "TOKEN_SPACES", spaces)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [onedata_error(403, "forbidden"), onedata_error(400, "posix", "eacces")],
    ids=["403", "eacces"],
)
async def test_a_permission_refusal_of_an_id_says_it_may_be_mis_copied(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, error: dict
) -> None:
    _the_token_sees(monkeypatch, SPACE_ID)  # the id's space is one of them
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    text = str(caught.value)
    assert text.startswith(f"Could not list file id {files._shown(DIR)}: the id may be mis-copied")
    assert "or the token may have no access to the file" in text  # the other reading stays
    assert "Copy the id exactly from the earlier answer, or give the path" in text
    assert "will not help" not in text and "Not allowed" not in text
    assert "not one this token sees" not in text
    assert "(Onedata's own text)" in text


@pytest.mark.asyncio
async def test_an_id_whose_space_the_token_does_not_see_says_so(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _the_token_sees(monkeypatch, "someOtherSpace0123456789")
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **onedata_error(403, "forbidden")
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    assert "the id may be mis-copied" in str(caught.value)
    assert "The space in the id is not one this token sees at this provider." in str(caught.value)


@pytest.mark.asyncio
async def test_a_space_not_supported_for_an_id_says_it_may_be_mis_copied_and_lists_the_spaces(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _the_token_sees(monkeypatch, "s0")
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/s0",
        json={"name": "Alpha", "spaceId": "s0"},
    )
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **onedata_error(400, "spaceNotSupportedBy")
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    text = str(caught.value)
    assert text.startswith(f"Could not list file id {files._shown(DIR)}: this provider reports")
    assert "The id may be mis-copied" in text and "or give the path" in text
    assert "The space in the id is not one this token sees at this provider." in text
    assert 'Spaces this token sees at this provider: "Alpha".' in text
    assert "so it cannot list it here" not in text  # the old claim about the provider


@pytest.mark.asyncio
async def test_the_refusals_of_a_path_keep_their_hints_and_read_no_spaces(
    httpx_mock: HTTPXMock,
) -> None:
    """A path that resolved is not a bad copy: pinned as they always were."""
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/d"), json={"fileId": DIR})
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **onedata_error(403, "forbidden")
    )
    with pytest.raises(OnedataApiError) as caught:
        await files.list_children("/Alpha/d", limit=10, offset=0)
    assert str(caught.value).startswith(
        "Not allowed to list path '/Alpha/d': the token has no access to it. Calling again "
        "will not help."
    )

    httpx_mock.add_response(method="POST", url=lookup_url("/Beta/d"), json={"fileId": DIR})
    httpx_mock.add_response(
        method="GET",
        url=f"{API}/data/{DIR}/children",
        **onedata_error(400, "spaceNotSupportedBy"),
    )
    with pytest.raises(OnedataApiError) as caught:
        await files.list_children("/Beta/d", limit=10, offset=0)
    assert str(caught.value).startswith(
        "This provider does not support the space of path '/Beta/d', so it cannot list it here; "
        "the space may still exist."
    )
    assert "mis-copied" not in str(caught.value)


# --- F3: the answer itself says more entries follow --------------------------------------------

MORE = "More entries follow: call again with token set to nextPageToken."
TWO = [{"name": "a.dat"}, {"name": "b.dat"}]  # one entry would be read as a file listed as itself


@pytest.mark.asyncio
async def test_a_page_that_is_not_the_last_says_more_entries_follow(
    httpx_mock: HTTPXMock,
) -> None:
    page = {"children": TWO, "isLast": False, "nextPageToken": "tok-1"}
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=page)

    out = await files.list_children(DIR, attributes=["name"], limit=1, offset=0)

    assert out["more"] == MORE
    assert {k: v for k, v in out.items() if k != "more"} == page  # nothing else changed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        {"children": [], "isLast": True, "nextPageToken": None},
        {"children": TWO, "isLast": True},
        {"children": TWO},  # an answer that does not say: nothing is claimed
    ],
    ids=["last", "last-no-token-key", "no-isLast"],
)
async def test_the_last_page_and_an_answer_that_does_not_say_get_no_note(
    httpx_mock: HTTPXMock, page: dict
) -> None:
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=page)

    out = await files.list_children(DIR, attributes=["name"], limit=10, offset=0)

    assert "more" not in out
    assert out == page


@pytest.mark.asyncio
async def test_a_page_that_is_not_the_last_but_has_no_token_does_not_send_the_caller_to_one(
    httpx_mock: HTTPXMock,
) -> None:
    page = {"children": TWO, "isLast": False, "nextPageToken": None}
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=page)

    out = await files.list_children(DIR, attributes=["name"], limit=1, offset=0)

    assert out["more"] == "More entries follow."


@pytest.mark.asyncio
async def test_the_note_reaches_the_model_and_agrees_with_the_description(
    httpx_mock: HTTPXMock, mcp: FastMCP
) -> None:
    page = {"children": TWO, "isLast": False, "nextPageToken": "tok-1"}
    httpx_mock.add_response(method="GET", url=f"{API}/data/{DIR}/children", json=page)

    async with Client(mcp) as client:
        result = await client.call_tool("list_children", {"parent_id_or_path": DIR})
        tool = next(t for t in await client.list_tools() if t.name == "list_children")

    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    assert result.structured_content["more"] == MORE
    assert MORE in text  # in the text a client shows, as well
    # the description says the same, in the same words about the same two fields
    description = " ".join(tool.description.split())
    assert "call again with `token` set to `nextPageToken`" in description
    assert "nextPageToken" in MORE and "token set to" in MORE
    assert [name for name in (ALL_KNOWN | FLAG_GATED) if name in MORE] == []


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
async def test_a_server_error_on_a_page_token_says_how_to_start_over(httpx_mock: HTTPXMock) -> None:
    """Onedata 25.0 answers a made-up or stale token with 500 internalServerError (seen live)."""
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **onedata_error(500, "internalServerError")
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0, token="garbage")

    text = str(caught.value)
    assert "valid only as returned in the previous page's nextPageToken" in text
    assert "without a token" in text and "internalServerError" in text


@pytest.mark.asyncio
async def test_a_server_error_without_a_token_gets_no_token_hint(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{API}/data/{DIR}/children", **onedata_error(500, "internalServerError")
    )

    with pytest.raises(OnedataApiError) as caught:
        await files.list_children(DIR, limit=10, offset=0)

    assert "nextPageToken" not in str(caught.value)


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
