"""Tests for `onedata_mcp/modules/files.py` MCP wrapper layer.

Most behavioural coverage lives in `test/unit/api/test_files.py` against
the API layer. The tests here exercise the wrapper-specific contracts
introduced for M-10 / M-11 / M-12:

- M-10: `download_file` MCP tool returns a dict envelope with
  `size_bytes` reflecting UTF-8 byte length (not character length).
- M-11: `create_file` raises a defensive ValueError on the
  empty-content + no-extension shape (the V3/GLM A4 mkdir-trap), and
  `create_directory` is registered as a separate tool.
- M-12: `list_files_recursively`'s `prefix` parameter docstring
  documents the relative-only contract.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from fastmcp import Client, FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp.modules import files as files_module
from test.unit.object_ids import file_id


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


def _lookup_url(path: str) -> str:
    return f"https://provider.example/api/v3/oneprovider/lookup-file-id/{quote(path, safe='')}"


def _mock_available_spaces(httpx_mock: HTTPXMock, names: list[str]) -> None:
    space_ids = [f"s{i}" for i in range(len(names))]
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        json=[
            {"name": name, "spaceId": space_id}
            for space_id, name in zip(space_ids, names, strict=True)
        ],
        is_reusable=True,
        is_optional=True,
    )
    for space_id, name in zip(space_ids, names, strict=True):
        httpx_mock.add_response(
            method="GET",
            url=f"https://onezone.example/api/v3/onezone/spaces/{space_id}",
            json={"name": name},
            is_reusable=True,
            is_optional=True,
        )


@pytest.fixture(autouse=True)
def _mock_spaces(httpx_mock: HTTPXMock) -> None:
    _mock_available_spaces(httpx_mock, ["space"])


@pytest.fixture
def files_mcp() -> FastMCP:
    """A FastMCP instance with only the files module registered."""
    mcp = FastMCP(name="onedata-mcp-test")
    files_module.register_module(mcp)
    return mcp


# ---------------------------------------------------------------------------
# M-10: download_file returns {content, size_bytes, content_type}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_wrapper_returns_envelope_for_ascii(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    files_mcp: FastMCP,
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/note.txt"),
        json={"fileId": "fid-ascii"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-ascii",
        json={"type": "REG", "size": 5},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-ascii/content",
        text="hello",
        headers={"Content-Type": "text/plain"},
    )

    async with Client(files_mcp) as client:
        result = await client.call_tool("download_file", {"file_id_or_path": "/space/note.txt"})

    payload = result.data
    assert isinstance(payload, dict)
    assert payload["content"] == "hello"
    assert payload["size_bytes"] == 5
    assert payload["size_bytes"] == len(b"hello")
    assert payload["content_type"] == "text/plain"


@pytest.mark.asyncio
async def test_download_file_wrapper_size_bytes_for_multibyte_utf8(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    files_mcp: FastMCP,
) -> None:
    """size_bytes counts BYTES not characters — the M-10 contract.

    Use UTF-8 multi-byte content: 'héllo' is 6 bytes (é = 2 bytes) but 5
    characters. The agent that does ``len(content)`` would report 5 bytes
    incorrectly; ``size_bytes`` must report 6.
    """
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/utf.txt"),
        json={"fileId": "fid-utf"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-utf",
        json={"type": "REG", "size": 6},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-utf/content",
        content="héllo".encode(),
    )

    async with Client(files_mcp) as client:
        result = await client.call_tool("download_file", {"file_id_or_path": "/space/utf.txt"})

    payload = result.data
    assert isinstance(payload, dict)
    assert payload["content"] == "héllo"
    assert len(payload["content"]) == 5  # character count
    assert payload["size_bytes"] == 6  # byte count — the canonical answer
    assert payload["size_bytes"] == len("héllo".encode())


# ---------------------------------------------------------------------------
# M-11: create_parents=True default + defensive mkdir-trap error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_file_defaults_create_parents_true(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    files_mcp: FastMCP,
) -> None:
    """Calling create_file WITHOUT specifying create_parents should default
    to the path-create flow (PUT /data/{root}/path/{rel}?create_parents=true).
    """
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space"),
        json={"fileId": "root-id"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=re.compile(
            r"https://provider\.example/api/v3/oneprovider/data/root-id/path/d/note\.txt\?.*"
        ),
        json={"fileId": "auto-fid"},
    )

    async with Client(files_mcp) as client:
        # NOTE: no create_parents kwarg passed.
        result = await client.call_tool(
            "create_file", {"path": "/space/d/note.txt", "content": "x"}
        )

    assert result.data == "auto-fid"
    put = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.host == "provider.example" and "/path/" in r.url.path
    )
    assert put.url.params["create_parents"] == "true"


@pytest.mark.asyncio
async def test_create_file_rejects_empty_content_no_extension(
    monkeypatch: pytest.MonkeyPatch,
    files_mcp: FastMCP,
) -> None:
    """The V3/GLM A4 trap: create_file('archive', '') would silently make
    a regular file at 'archive', then archive/<x> ops fail with enotdir.

    The wrapper must refuse this shape with a ValueError pointing at
    create_directory.
    """
    _set_env(monkeypatch)

    async with Client(files_mcp) as client:
        result = await client.call_tool(
            "create_file",
            {"path": "/space/archive", "content": ""},
            raise_on_error=False,
        )

    # FastMCP surfaces tool-side ValueErrors as is_error=True with the
    # message in the content text.
    assert result.is_error is True
    text_blocks = [c.text for c in result.content if hasattr(c, "text")]
    joined = "\n".join(text_blocks)
    assert "create_directory" in joined
    assert "enotdir" in joined


@pytest.mark.asyncio
async def test_create_file_allows_explicit_extension_with_empty_content(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    files_mcp: FastMCP,
) -> None:
    """Empty file with an extension is a legitimate use — must NOT raise."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space"),
        json={"fileId": "root-id"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=re.compile(
            r"https://provider\.example/api/v3/oneprovider/data/root-id/path/empty\.txt\?.*"
        ),
        json={"fileId": "empty-fid"},
    )

    async with Client(files_mcp) as client:
        result = await client.call_tool("create_file", {"path": "/space/empty.txt", "content": ""})

    assert result.is_error is False
    assert result.data == "empty-fid"


@pytest.mark.asyncio
async def test_create_directory_tool_creates_directory(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    files_mcp: FastMCP,
) -> None:
    """The new create_directory MCP tool — invokes PUT
    /data/{space}/path/{rel}?type=DIR&create_parents=true."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space"),
        json={"fileId": "root-id"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=re.compile(
            r"https://provider\.example/api/v3/oneprovider/data/root-id/path/archive\?.*"
        ),
        json={"fileId": "dir-fid"},
    )

    async with Client(files_mcp) as client:
        result = await client.call_tool("create_directory", {"path": "/space/archive"})

    assert result.is_error is False
    assert result.data == {"fileId": "dir-fid", "path": "/space/archive"}
    put = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.host == "provider.example" and "/path/" in r.url.path
    )
    assert put.url.params["type"] == "DIR"
    assert put.url.params["create_parents"] == "true"


@pytest.mark.asyncio
async def test_create_directory_then_create_file_under_it(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    files_mcp: FastMCP,
) -> None:
    """End-to-end shape: after create_directory('archive'), a subsequent
    create_file('archive/x.txt') should work."""
    _set_env(monkeypatch)
    # Two phases: mkdir, then file-create-under-it.
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space"),
        json={"fileId": "root-id"},
        is_reusable=True,
    )
    # mkdir PUT
    httpx_mock.add_response(
        method="PUT",
        url=re.compile(
            r"https://provider\.example/api/v3/oneprovider/data/root-id/path/archive\?.*"
        ),
        json={"fileId": "dir-fid"},
    )
    # subsequent file PUT under the new directory
    httpx_mock.add_response(
        method="PUT",
        url=re.compile(
            r"https://provider\.example/api/v3/oneprovider/data/root-id/path/archive/x\.txt\?.*"
        ),
        json={"fileId": "file-fid"},
    )

    async with Client(files_mcp) as client:
        await client.call_tool("create_directory", {"path": "/space/archive"})
        file_result = await client.call_tool(
            "create_file", {"path": "/space/archive/x.txt", "content": "data"}
        )

    assert file_result.is_error is False
    assert file_result.data == "file-fid"


# ---------------------------------------------------------------------------
# M-12: list_files_recursively prefix-param documentation contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_recursively_prefix_param_doc_relative_only(
    files_mcp: FastMCP,
) -> None:
    """The prefix param docstring must explicitly state RELATIVE-only —
    M-12 surfaced V3 silently getting `{files: []}` from passing an
    absolute prefix like '/space/d2/datasets/alpha'."""
    async with Client(files_mcp) as client:
        tools = await client.list_tools()

    list_tool = next(t for t in tools if t.name == "list_files_recursively")
    schema = list_tool.inputSchema
    properties = schema.get("properties", {})
    prefix_field = properties.get("prefix", {})
    description = prefix_field.get("description", "")

    # The contract: must mention "relative" and "Absolute paths are NOT
    # supported" so an LLM reading the schema knows not to pass /space/...
    assert "relative" in description.lower()
    assert "absolute" in description.lower()


@pytest.mark.asyncio
async def test_list_files_recursively_with_relative_prefix_filters(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    files_mcp: FastMCP,
) -> None:
    """Functional check: passing a relative prefix forwards correctly to
    the underlying API request body."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/d2/datasets"),
        json={"fileId": "parent-fid"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/parent-fid/files",
        json={"files": [{"path": "alpha.txt", "name": "alpha.txt", "type": "REG", "size": 1}]},
    )

    async with Client(files_mcp) as client:
        result = await client.call_tool(
            "list_files_recursively",
            {"parent_id_or_path": "/space/d2/datasets", "prefix": "alpha"},
        )

    files = result.data["files"]
    assert len(files) == 1
    # M-6 absolute-path normalization (independent fix, but we ride it):
    assert files[0]["path"] == "/space/d2/datasets/alpha.txt"


# ---------------------------------------------------------------------------
# A relative path with a client that offers no MCP roots gets a clear error
# (was: "List roots not supported", which says nothing about the path).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_file_attributes", "get_file_distribution"])
async def test_relative_path_without_client_roots_says_how_to_fix(
    monkeypatch: pytest.MonkeyPatch, files_mcp: FastMCP, tool: str
) -> None:
    _set_env(monkeypatch)
    async with Client(files_mcp) as client:
        result = await client.call_tool(
            tool, {"file_id_or_path": "space/a.nc"}, raise_on_error=False
        )
    assert result.is_error
    text = result.content[0].text
    assert "Paths must start with '/'" in text
    assert "/<space_name>/<path_to_file>" in text
    assert "'space/a.nc'" in text
    assert "List roots not supported" not in text


@pytest.mark.asyncio
async def test_relative_path_with_client_root_still_resolves(
    monkeypatch: pytest.MonkeyPatch, files_mcp: FastMCP, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST", url=_lookup_url("/space/a.nc"), json={"fileId": "0000abcd"}
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"https://provider\.example/api/v3/oneprovider/data/0000abcd(\?.*)?$"),
        json={"name": "a.nc", "path": "/space/a.nc", "type": "REG", "size": 3},
    )
    async with Client(files_mcp, roots=["file:///space"]) as client:
        result = await client.call_tool(
            "get_file_attributes", {"file_id_or_path": "a.nc"}, raise_on_error=False
        )
    assert not result.is_error, result.content[0].text


# ---------------------------------------------------------------------------
# With an MCP root mounted, get_file_attributes and list_children tell a name from a copied id
# by the id's own start (its header, or the hex of `guid#`), not by length: a long plain name is
# resolved under the root, and a copied id, damaged or wrapped in punctuation, is checked as an
# id instead of being looked up as a path (design/13-14).
# ---------------------------------------------------------------------------

ROOT = "file:///Alpha"
COPIED_ID = file_id("0cb444f249735948f20eaf93a2dfe54bch3e33", "s0")
DATA = r"https://provider\.example/api/v3/oneprovider/data/"
ID_TOOLS = [
    ("get_file_attributes", "file_id_or_path", "", {"name": "d", "type": "DIR"}),
    ("list_children", "parent_id_or_path", "/children", {"children": [], "isLast": True}),
]


def _lookups(httpx_mock: HTTPXMock) -> list:
    return [r for r in httpx_mock.get_requests() if "/lookup-file-id/" in r.url.path]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arg", "suffix", "body"), ID_TOOLS)
async def test_a_long_plain_name_is_resolved_under_the_root(
    monkeypatch: pytest.MonkeyPatch,
    files_mcp: FastMCP,
    httpx_mock: HTTPXMock,
    tool: str,
    arg: str,
    suffix: str,
    body: dict,
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/Alpha/quarterlyreportsarchive"),
        json={"fileId": COPIED_ID},
    )
    httpx_mock.add_response(
        method="GET", url=re.compile(rf"{DATA}{COPIED_ID}{suffix}(\?.*)?$"), json=body
    )
    async with Client(files_mcp, roots=[ROOT]) as client:
        result = await client.call_tool(
            tool, {arg: "quarterlyreportsarchive"}, raise_on_error=False
        )
    assert not result.is_error, result.content[0].text
    assert len(_lookups(httpx_mock)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arg", "suffix", "body"), ID_TOOLS)
@pytest.mark.parametrize("value", [COPIED_ID, COPIED_ID.lower()])
async def test_a_copied_id_is_used_as_an_id_with_a_root_mounted(
    monkeypatch: pytest.MonkeyPatch,
    files_mcp: FastMCP,
    httpx_mock: HTTPXMock,
    tool: str,
    arg: str,
    suffix: str,
    body: dict,
    value: str,
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET", url=re.compile(rf"{DATA}{value}{suffix}(\?.*)?$"), json=body
    )
    async with Client(files_mcp, roots=[ROOT]) as client:
        result = await client.call_tool(tool, {arg: value}, raise_on_error=False)
    assert not result.is_error, result.content[0].text
    assert _lookups(httpx_mock) == []


DAMAGED_COPIES = {
    "a full stop after it": COPIED_ID + ".",
    "in backticks": f"`{COPIED_ID}`",
    "in quotes": f'"{COPIED_ID}"',
    "a space after the header's zeros (a recorded copy)": COPIED_ID[:11] + " " + COPIED_ID[11:],
    "a hyphen inside": COPIED_ID[:40] + "-" + COPIED_ID[40:],
    "a leading zero dropped (a recorded copy)": COPIED_ID[1:],
    "the payload without its header": COPIED_ID[16:],
    "a header spliced onto it": "000000000052FFFF" + COPIED_ID[16:],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arg", "suffix", "body"), ID_TOOLS)
@pytest.mark.parametrize("name", list(DAMAGED_COPIES))
async def test_a_damaged_copy_is_checked_as_an_id_not_looked_up_under_the_root(
    monkeypatch: pytest.MonkeyPatch,
    files_mcp: FastMCP,
    httpx_mock: HTTPXMock,
    tool: str,
    arg: str,
    suffix: str,
    body: dict,
    name: str,
) -> None:
    _set_env(monkeypatch)
    async with Client(files_mcp, roots=[ROOT]) as client:
        result = await client.call_tool(tool, {arg: DAMAGED_COPIES[name]}, raise_on_error=False)
    assert result.is_error
    text = result.content[0].text
    assert "is not a complete file id; copy it exactly from the earlier answer" in text, text
    assert httpx_mock.get_requests() == []  # no lookup under the root, nothing sent as an id


# ---------------------------------------------------------------------------
# get_file_distribution: `holds_whole_file` per provider (SF3, design/16)
# ---------------------------------------------------------------------------

SIZE = 262144


def _entry(
    blocks: list | None,
    size_field: str | None = "virtualSize",
    size: int | None = SIZE,
    **extra: object,
) -> dict:
    """A provider's entry for a regular file: one storage with the given blocks."""
    entry: dict = {
        "success": True,
        "distributionPerStorageBackend": {
            "s1": {"success": True, "blocks": blocks, "physicalSize": 0}
        },
        **extra,
    }
    if size_field is not None:
        entry[size_field] = size
    return entry


def _distribution(**entries: dict) -> dict:
    return {"type": "REG", "distributionPerProvider": entries}


def _marked(body: dict) -> dict[str, dict]:
    files_module._add_holds_whole_file(body)
    return body["distributionPerProvider"]


def test_blocks_covering_the_size_are_a_whole_copy() -> None:
    marked = _marked(
        _distribution(
            a=_entry([[0, SIZE]]),
            b=_entry([[0, 100000], [100000, SIZE - 100000]]),  # adjacent blocks, as one
            c=_entry([[0, SIZE], [1000, 50]]),  # a block inside another changes nothing
        )
    )
    assert [marked[p]["holds_whole_file"] for p in "abc"] == [True, True, True]
    assert all("holds_whole_file_note" not in marked[p] for p in "abc")


def test_blocks_that_leave_a_gap_are_a_partial_copy() -> None:
    marked = _marked(
        _distribution(
            a=_entry([[0, SIZE]]),
            b=_entry([[0, SIZE // 2]]),
            c=_entry([[0, 131072], [196608, 65536]]),  # the third quarter is missing
        )
    )
    assert [marked[p]["holds_whole_file"] for p in "abc"] == [True, False, False]


def test_an_entry_with_a_size_and_no_blocks_holds_nothing() -> None:
    """The entry that was read as a copy: success, the file's size, physicalSize 0, no blocks."""
    marked = _marked(_distribution(owner=_entry([[0, SIZE]]), local=_entry([])))
    assert marked["local"]["holds_whole_file"] is False
    assert marked["local"]["virtualSize"] == SIZE  # the existing fields stay
    assert marked["local"]["distributionPerStorageBackend"]["s1"]["blocks"] == []
    assert marked["owner"]["holds_whole_file"] is True


@pytest.mark.parametrize(
    "size_field, size",
    [("virtualSize", 0), ("logicalSize", 0), ("virtualSize", None), (None, None)],
)
def test_a_size_that_is_zero_or_unknown_is_said_not_guessed(
    size_field: str | None, size: int | None
) -> None:
    """Even for a provider with a block: the blocks alone do not say how long the file is."""
    marked = _marked(
        _distribution(a=_entry([[0, 10]], size_field, size), b=_entry([], size_field, size))
    )
    for provider in "ab":
        assert marked[provider]["holds_whole_file"] is None
        assert "size is 0 or not reported" in marked[provider]["holds_whole_file_note"]


def test_an_empty_file_with_no_blocks_is_not_called_whole() -> None:
    marked = _marked(_distribution(a=_entry([], size=0), b=_entry([], size=0)))
    for provider in "ab":
        assert marked[provider]["holds_whole_file"] is None
        assert "size is 0 or not reported" in marked[provider]["holds_whole_file_note"]


def test_the_largest_size_any_provider_reports_is_the_files_size() -> None:
    """A stale, smaller size at one provider must not let its partial replica look whole."""
    marked = _marked(
        _distribution(stale=_entry([[0, 1000]], size=1000), current=_entry([[0, SIZE]], size=SIZE))
    )
    assert marked["stale"]["holds_whole_file"] is False
    assert marked["current"]["holds_whole_file"] is True


def test_the_older_size_name_is_read_too() -> None:
    marked = _marked(_distribution(a=_entry([[0, SIZE]], "logicalSize")))
    assert marked["a"]["holds_whole_file"] is True


def test_an_unreadable_storage_is_unknown_unless_what_was_read_covers_the_file() -> None:
    broken = {"success": False, "error": {"id": "posix"}}

    def with_broken(blocks: list) -> dict:
        entry = _entry(blocks)
        entry["distributionPerStorageBackend"]["s2"] = broken
        return entry

    marked = _marked(_distribution(partial=with_broken([[0, 10]]), whole=with_broken([[0, SIZE]])))
    assert marked["partial"]["holds_whole_file"] is None
    assert "could not be read in full" in marked["partial"]["holds_whole_file_note"]
    assert marked["whole"]["holds_whole_file"] is True  # what was read already covers it


def test_malformed_blocks_are_unknown_not_false() -> None:
    marked = _marked(_distribution(a=_entry([[0, SIZE]]), b=_entry([[0]]), c=_entry([["x", 4]])))
    assert marked["a"]["holds_whole_file"] is True
    assert marked["b"]["holds_whole_file"] is None and marked["c"]["holds_whole_file"] is None


def test_a_failed_entry_and_a_directory_are_left_as_they_are() -> None:
    failed = {"success": False, "error": {"id": "posix"}}
    marked = _marked(_distribution(a=_entry([[0, SIZE]]), gone=dict(failed)))
    assert marked["gone"] == failed
    directory = {
        "type": "DIR",
        "distributionPerProvider": {
            "a": {
                "success": True,
                "virtualSize": 8,
                "distributionPerStorageBackend": {"s1": {"success": True, "physicalSize": 6}},
            }
        },
    }
    assert "holds_whole_file" not in _marked(directory)["a"]


def test_no_provider_mapping_is_not_an_error() -> None:
    for body in ({}, {"type": "REG"}, {"type": "REG", "distributionPerProvider": None}):
        files_module._add_holds_whole_file(body)  # nothing to mark


@pytest.mark.asyncio
async def test_the_tool_answers_with_the_field_beside_the_existing_ones(
    monkeypatch: pytest.MonkeyPatch, files_mcp: FastMCP
) -> None:
    async def distribution(_: str) -> dict:
        return _distribution(owner=_entry([[0, SIZE]], physicalSize=SIZE), local=_entry([]))

    async def no_spaces() -> list:
        return []

    monkeypatch.setattr(files_module, "get_file_distribution", distribution)
    monkeypatch.setattr("onedata_mcp.api.spaces.list_user_spaces", no_spaces)
    async with Client(files_mcp) as client:
        result = await client.call_tool("get_file_distribution", {"file_id_or_path": "/space/f"})
    per_provider = result.structured_content["distributionPerProvider"]
    assert per_provider["local"]["holds_whole_file"] is False
    assert per_provider["local"]["virtualSize"] == SIZE
    assert per_provider["owner"]["holds_whole_file"] is True
