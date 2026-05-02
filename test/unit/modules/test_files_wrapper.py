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
        url="https://onezone.example/api/v3/onezone/spaces",
        json={"spaces": space_ids},
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
