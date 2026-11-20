"""create_directory, gate 1 of the promotion protocol (design/15): every error path a model can
trigger, the path's encoding, and the surface, through the API and through an MCP client.

The Onedata error bodies here are plausible shapes, not the 25.0 bodies: the swagger lists
only the statuses (400, 403, 404). The live test (test/live/test_files_live.py) prints the real
ones.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from fastmcp import Client, FastMCP
from pytest_httpx import HTTPXMock

from onedata_mcp.api import files
from onedata_mcp.modules import files as files_module
from onedata_mcp.token_policy import WRITE_TOOL_NAMES
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED
from onedata_mcp.utils import OnedataApiError

API = "https://provider.example/api/v3/oneprovider"
ROOT = "0000000000RoOtOfAlpha00000"


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


def root_lookup(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha"), json={"fileId": ROOT})


def puts(httpx_mock: HTTPXMock) -> list:
    return [r for r in httpx_mock.get_requests() if r.method == "PUT"]


def onedata_error(status: int, error_id: str, errno: str | None = None) -> dict:
    body: dict = {"error": {"id": error_id, "description": f"{error_id} (Onedata's own text)"}}
    if errno:
        body["error"]["details"] = {"errno": errno}
    return {"status_code": status, "json": body}


async def call(mcp: FastMCP, arguments: dict) -> tuple[bool, str]:
    async with Client(mcp) as client:
        result = await client.call_tool("create_directory", arguments, raise_on_error=False)
    return result.is_error, "\n".join(c.text for c in result.content if hasattr(c, "text"))


# --- the path ------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "raw"),
    [
        ("/Alpha/archive", "archive"),
        ("/Alpha/2020/01/", "2020/01"),
        ("/Alpha/a b/c?d#e", "a%20b/c%3Fd%23e"),
        ("/Alpha/100%", "100%25"),
    ],
)
async def test_each_component_is_percent_encoded(
    httpx_mock: HTTPXMock, path: str, raw: str
) -> None:
    """A `?` or `#` in a name is part of the name, never a query or a fragment."""
    root_lookup(httpx_mock)
    httpx_mock.add_response(method="PUT", status_code=201, json={"fileId": "d1"})

    assert await files.create_directory(path) == {"fileId": "d1", "path": path}

    (sent,) = puts(httpx_mock)
    assert sent.url.raw_path.decode().split("?")[0] == f"/api/v3/oneprovider/data/{ROOT}/path/{raw}"
    assert dict(sent.url.params) == {"type": "DIR", "create_parents": "true"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "says"),
    [
        ("/Alpha", "is the root directory of a space, which always exists"),
        ("/", "The path is empty"),
        ("/Alpha/a/../b", "has an empty, '.' or '..' component"),
        ("/Alpha/a/..", "has an empty, '.' or '..' component"),
        ("/Alpha/./b", "has an empty, '.' or '..' component"),
        ("/Alpha/a//b", "has an empty, '.' or '..' component"),
    ],
)
async def test_a_path_that_cannot_name_a_new_directory_is_refused_before_any_request(
    httpx_mock: HTTPXMock, path: str, says: str
) -> None:
    """Sent as is, `/Alpha/a/../b` became `.../path/b`: a directory made at another path."""
    with pytest.raises(ValueError, match=re.escape(says)) as caught:
        await files.create_directory(path)

    assert "/<space_name>/<path_to_directory>" in str(caught.value)
    assert [
        r for r in httpx_mock.get_requests() if r.url.path != "/api/v3/oneprovider/spaces"
    ] == []


# --- what is in the way ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing", "says"),
    [
        ("DIR", "'/Alpha/archive' already exists as a directory, so nothing was created."),
        (
            "REG",
            "'/Alpha/archive' already exists as a regular file, so no directory was created.",
        ),
        (
            None,
            "Something already exists at '/Alpha/archive' (eexist), so no directory was created.",
        ),
    ],
)
async def test_already_exists_says_what_is_there(
    httpx_mock: HTTPXMock, existing: str | None, says: str
) -> None:
    """Before, it said "Directory ... already exists" even when a regular file was there."""
    root_lookup(httpx_mock)
    httpx_mock.add_response(method="PUT", **onedata_error(400, "posix", "eexist"))
    if existing:
        httpx_mock.add_response(
            method="POST", url=lookup_url("/Alpha/archive"), json={"fileId": "x1"}
        )
        httpx_mock.add_response(method="GET", url=f"{API}/data/x1", json={"type": existing})
    else:
        httpx_mock.add_response(
            method="POST", url=lookup_url("/Alpha/archive"), **onedata_error(500, "internal")
        )

    with pytest.raises(FileExistsError) as caught:
        await files.create_directory("/Alpha/archive")

    assert str(caught.value) == says
    assert "Choose" not in str(caught.value)  # it must not invite a directory elsewhere


@pytest.mark.asyncio
async def test_a_409_that_the_lookup_explains_says_what_is_there(httpx_mock: HTTPXMock) -> None:
    root_lookup(httpx_mock)
    httpx_mock.add_response(method="PUT", **onedata_error(409, "conflict"))
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/archive"), json={"fileId": "x1"})
    httpx_mock.add_response(method="GET", url=f"{API}/data/x1", json={"type": "REG"})

    with pytest.raises(FileExistsError, match=r"already exists as a regular file"):
        await files.create_directory("/Alpha/archive")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup",
    [onedata_error(400, "posix", "enoent"), onedata_error(500, "internal")],
    ids=["path-not-found", "lookup-fails"],
)
async def test_a_409_that_nothing_explains_is_a_conflict_not_an_existing_entry(
    httpx_mock: HTTPXMock, lookup: dict
) -> None:
    """Without eexist, and with no entry found at the path, nothing establishes that something
    exists there: the error says conflict, keeps Onedata's message, and is not FileExistsError."""
    root_lookup(httpx_mock)
    httpx_mock.add_response(method="PUT", **onedata_error(409, "conflict"))
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/archive"), **lookup)

    with pytest.raises(OnedataApiError) as caught:
        await files.create_directory("/Alpha/archive")

    text = str(caught.value)
    assert text.startswith(
        "This provider refused to create '/Alpha/archive' with a conflict (409) and did not "
        "say why, so no directory was created."
    )
    assert "already exists" not in text and "status=409" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "create_parents", "starts"),
    [
        (
            onedata_error(400, "posix", "enotdir"),
            True,
            "A component of '/Alpha/a.dat/sub' above the new directory is not a directory, so "
            "nothing was created.",
        ),
        (
            {"status_code": 404, "json": {"error": {}}},
            False,
            "This provider answered 404 for '/Alpha/a.dat/sub' without saying why: a parent may "
            "not exist, its space may not be supported by this provider",
        ),
        (
            onedata_error(400, "posix", "enoent"),
            True,
            "This provider reports part of '/Alpha/a.dat/sub' missing although create_parents is "
            "true",
        ),
        (
            onedata_error(400, "spaceNotSupportedBy"),
            True,
            "This provider does not support the space of '/Alpha/a.dat/sub', so nothing was "
            "created here; the space may still exist.",
        ),
        (
            onedata_error(400, "posix", "enoent"),
            False,
            "The parent directory of '/Alpha/a.dat/sub' does not exist, so nothing was created. "
            "Create it first, or call again with create_parents set to true.",
        ),
        (
            onedata_error(403, "forbidden"),
            True,
            "Not allowed to create '/Alpha/a.dat/sub': the token may be read-only",
        ),
        (onedata_error(400, "posix", "eacces"), True, "Not allowed to create"),
    ],
)
async def test_failures_say_what_to_do(
    httpx_mock: HTTPXMock, error: dict, create_parents: bool, starts: str
) -> None:
    root_lookup(httpx_mock)
    httpx_mock.add_response(method="PUT", **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.create_directory("/Alpha/a.dat/sub", create_parents=create_parents)

    assert str(caught.value).startswith(starts)
    assert "status=" in str(caught.value)  # the underlying error is kept
    if not starts.startswith("The parent directory"):
        assert "parent directory of" not in str(caught.value)  # no missing-parent claim


@pytest.mark.asyncio
async def test_other_failures_pass_on_unchanged(httpx_mock: HTTPXMock) -> None:
    root_lookup(httpx_mock)
    httpx_mock.add_response(method="PUT", **onedata_error(500, "internal"))

    with pytest.raises(OnedataApiError) as caught:
        await files.create_directory("/Alpha/archive")

    assert str(caught.value).startswith("Onedata API request failed: PUT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [onedata_error(400, "posix", "enoent"), onedata_error(404, "notFound")]
)
async def test_a_space_this_provider_reports_missing_lists_the_spaces(
    httpx_mock: HTTPXMock, error: dict
) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url("/Beta"), **error)

    with pytest.raises(FileNotFoundError) as caught:
        await files.create_directory("/Beta/archive")

    assert str(caught.value) == (
        "No space named 'Beta' is visible at this provider, so nothing was created. Spaces "
        'this token sees at this provider: "Alpha".'
    )
    assert puts(httpx_mock) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "starts"),
    [
        (
            {"status_code": 404, "json": {"error": {}}},
            "This provider answered 404 for path '/Beta' without saying why",
        ),
        (
            onedata_error(400, "spaceNotSupportedBy"),
            "This provider does not support the space of path '/Beta', so it cannot create a "
            "directory in it here; the space may still exist.",
        ),
    ],
)
async def test_a_space_lookup_without_a_structured_not_found_does_not_call_it_missing(
    httpx_mock: HTTPXMock, error: dict, starts: str
) -> None:
    httpx_mock.add_response(method="POST", url=lookup_url("/Beta"), **error)

    with pytest.raises(OnedataApiError) as caught:
        await files.create_directory("/Beta/archive")

    assert str(caught.value).startswith(starts)
    assert "No space named" not in str(caught.value)
    assert puts(httpx_mock) == []


# --- through the MCP client ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_model_reads_what_is_in_the_way(mcp: FastMCP, httpx_mock: HTTPXMock) -> None:
    root_lookup(httpx_mock)
    httpx_mock.add_response(method="PUT", **onedata_error(400, "posix", "eexist"))
    httpx_mock.add_response(method="POST", url=lookup_url("/Alpha/archive"), json={"fileId": "x1"})
    httpx_mock.add_response(method="GET", url=f"{API}/data/x1", json={"type": "REG"})

    is_error, text = await call(mcp, {"path": "/Alpha/archive"})

    assert is_error is True
    assert "already exists as a regular file, so no directory was created" in text
    assert "provider.example" not in text


@pytest.mark.asyncio
async def test_create_parents_false_reaches_onedata(mcp: FastMCP, httpx_mock: HTTPXMock) -> None:
    root_lookup(httpx_mock)
    httpx_mock.add_response(method="PUT", status_code=201, json={"fileId": "d1"})

    is_error, _ = await call(mcp, {"path": "/Alpha/d/sub", "create_parents": False})

    assert is_error is False
    (sent,) = puts(httpx_mock)
    assert sent.url.params["create_parents"] == "false"


# --- the surface (gate 2) ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_description_follows_the_surface_rules(mcp: FastMCP) -> None:
    """design/10, gate 2: it says what it returns and what an existing path does, names no
    other tool (create_file may not be offered with it), and cites no document."""
    async with Client(mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == "create_directory")
    text = f"{tool.description} " + " ".join(
        p["description"] for p in tool.inputSchema["properties"].values()
    )

    assert "Returns `{fileId, path}`" in tool.description
    assert "already exists" in tool.description
    others = (ALL_KNOWN | FLAG_GATED) - {"create_directory"}
    assert [name for name in others if re.search(rf"\b{name}\b", text)] == []
    assert not re.search(r"\.md\b|design/|research/|https?://|§|\bM-\d|FileExistsError|PUT ", text)
    assert tool.annotations.destructiveHint is True
    assert "create_directory" in WRITE_TOOL_NAMES
    assert set(tool.inputSchema["properties"]) == {"path", "create_parents"}  # no list to cap
