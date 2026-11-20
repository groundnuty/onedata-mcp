"""Live test of `get_file_attributes`, `list_children` and `create_directory` against the LOCAL
Compose federation (design/13, design/14, design/15).

Skipped unless ONEDATA_MCP_LIVE_FILES=1. With it set, the test REFUSES to run (it fails) unless
the Oneprovider host, and the Onezone host when one is set, is `127.0.0.1`, `localhost` or ends
in `.datahub.test`: it creates and deletes files and directories, and must never touch
production.

    make test-files-live      the usual ONEDATA_* variables, set by hand
    make valfed-files-live    against the project's own federation, in its space omcp-live

Environment (the usual four, plus the space to work in):

    ONEDATA_ONEPROVIDER_HOST, ONEDATA_ONEPROVIDER_TOKEN
    ONEDATA_ONEZONE_HOST, ONEDATA_ONEZONE_TOKEN
    ONEDATA_MCP_LIVE_FILES_SPACE   name of a space the local provider supports

Everything is made under one probe directory, `/<space>/files-live-<time>-<hex>`, deleted at the
end. The tools are called through an in-process MCP client, so what is checked is what a model
gets: the answer on success and the error text on failure. Each test PRINTS what the unit tests
cannot pin (they use fakes; the swagger gives only statuses): the real error bodies, the form of
`posixPermissions`, the unit of the times, the order of a listing.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP

from onedata_mcp.api.files import create_file, delete_file, get_file_id
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.modules import files as files_module
from onedata_mcp.utils import request

ENABLE = "ONEDATA_MCP_LIVE_FILES"
SPACE_ENV = "ONEDATA_MCP_LIVE_FILES_SPACE"

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE) != "1", reason=f"live test: set {ENABLE}=1 (local Compose only)"
)


def is_local_host(host: str | None) -> bool:
    return bool(host) and (host in {"127.0.0.1", "localhost"} or host.endswith(".datahub.test"))


@pytest.fixture(autouse=True)
def _local_federation_only() -> None:
    hosts = {"ONEDATA_ONEPROVIDER_HOST": os.environ.get("ONEDATA_ONEPROVIDER_HOST", "")}
    if os.environ.get("ONEDATA_ONEZONE_HOST"):
        hosts["ONEDATA_ONEZONE_HOST"] = os.environ["ONEDATA_ONEZONE_HOST"]
    for name, url in hosts.items():
        host = urlparse(url).hostname
        if not is_local_host(host):
            pytest.fail(
                "refusing to run: the live file-tools test writes files and directories, and runs "
                f"only against 127.0.0.1, localhost or *.datahub.test ({name} host: {host!r})",
                pytrace=False,
            )


@pytest.fixture
def space() -> str:
    name = os.environ.get(SPACE_ENV, "").strip()
    if not name:
        pytest.fail(f"{SPACE_ENV} must name a space the local provider supports", pytrace=False)
    return name


@pytest.fixture
def files_mcp() -> FastMCP:
    mcp = FastMCP(name="onedata-mcp-live-files")
    files_module.register_module(mcp)
    return mcp


@pytest_asyncio.fixture
async def probe(space: str):
    """A fresh probe directory, made through the stable create_file (a file inside it, with its
    parents); deleted afterwards with everything in it."""
    path = f"/{space}/files-live-{int(time.time())}-{secrets.token_hex(3)}"
    await create_file(f"{path}/seed.txt", "seed\n", create_parents=True)
    yield path
    with contextlib.suppress(Exception):  # cleanup must not hide the test's result
        await delete_file(path)


async def _call(mcp: FastMCP, tool: str, arguments: dict):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    return result, text


def _payload(result, text: str):
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(text)


async def set_mode(path: str, mode: str) -> None:
    """`PUT /data/{id}` with `posixPermissions` (swagger `set_attr`): the panel's FA fixtures
    set modes this way, at the owner provider."""
    file_id = await get_file_id(path)
    await request(
        get_oneprovider_config(), "PUT", f"/data/{file_id}", json_body={"posixPermissions": mode}
    )


# =============================================================================================
# get_file_attributes (design/13)
# =============================================================================================


@pytest.mark.asyncio
async def test_attributes_by_path_and_by_id_after_a_mode_is_set(
    files_mcp: FastMCP, probe: str
) -> None:
    """The FA scenarios set modes 644 and 600 on small files and ask which file differs: the mode
    must read back, by path and by id, at the provider the agent uses."""
    path = f"{probe}/mode-probe.dat"
    file_id = await create_file(path, "0123456789")
    await set_mode(path, "600")

    result, text = await _call(files_mcp, "get_file_attributes", {"file_id_or_path": path})

    assert result.is_error is False, text
    by_path = _payload(result, text)
    print(f"default attributes of a file: {by_path}")
    assert by_path["type"] == "REG" and by_path["size"] == 10
    print(f"posixPermissions after setting '600': {by_path['posixPermissions']!r}")
    assert by_path["posixPermissions"] in {"600", "0600"}, by_path

    result, text = await _call(
        files_mcp,
        "get_file_attributes",
        {"file_id_or_path": file_id, "attributes": ["name", "posixPermissions", "creationTime"]},
    )
    assert result.is_error is False, text
    by_id = _payload(result, text)
    assert by_id["name"] == "mode-probe.dat"
    assert by_id["posixPermissions"] == by_path["posixPermissions"]
    print(f"creationTime {by_id['creationTime']} (seconds if ~1.7e9, milliseconds if ~1.7e12)")


@pytest.mark.asyncio
async def test_attributes_of_a_directory(files_mcp: FastMCP, probe: str) -> None:
    result, text = await _call(files_mcp, "get_file_attributes", {"file_id_or_path": probe})

    assert result.is_error is False, text
    attrs = _payload(result, text)
    print(f"default attributes of a directory: {attrs}")
    assert attrs["type"] == "DIR"


@pytest.mark.asyncio
async def test_attributes_given_as_json_text_reach_onedata_as_a_list(
    files_mcp: FastMCP, probe: str
) -> None:
    result, text = await _call(
        files_mcp,
        "get_file_attributes",
        {"file_id_or_path": f"{probe}/seed.txt", "attributes": '["size", "mtime"]'},
    )

    assert result.is_error is False, text
    assert set(_payload(result, text)) == {"size", "mtime"}


@pytest.mark.asyncio
async def test_attribute_errors_as_the_model_reads_them(files_mcp: FastMCP, probe: str) -> None:
    """Prints each error text; asserts the hint where this server adds one."""
    missing = f"{probe}/no-such-file.dat"
    result, text = await _call(files_mcp, "get_file_attributes", {"file_id_or_path": missing})
    print(f"missing path answered: {text}")
    assert result.is_error is True and "not found" in text

    made_up = secrets.token_hex(16)
    result, text = await _call(files_mcp, "get_file_attributes", {"file_id_or_path": made_up})
    print(f"made-up id answered: {text}")
    assert result.is_error is True
    assert "Give a path, /<space_name>/<path>, or a file id." in text or "space" in text.lower()

    result, text = await _call(
        files_mcp,
        "get_file_attributes",
        {"file_id_or_path": f"{probe}/seed.txt", "attributes": ["owner"]},
    )
    print(f"unknown attribute name answered: {text}")
    assert result.is_error is True and "owner" in text

    result, text = await _call(
        files_mcp,
        "get_file_attributes",
        {"file_id_or_path": f"{probe}/seed.txt", "attributes": ["mode"]},
    )
    assert result.is_error is True and "mode->posixPermissions" in text


# =============================================================================================
# list_children (design/14)
# =============================================================================================


async def _all_pages(mcp: FastMCP, path: str, **arguments) -> tuple[list[str], int]:
    """Every entry name, page by page with the token; and how many pages it took."""
    names: list[str] = []
    token = None
    pages = 0
    while True:
        args = {"parent_id_or_path": path, **arguments}
        if token:
            args["token"] = token
        result, text = await _call(mcp, "list_children", args)
        assert result.is_error is False, text
        page = _payload(result, text)
        pages += 1
        names += [c["name"] for c in page["children"]]
        if page["isLast"]:
            return names, pages
        token = page["nextPageToken"]
        assert token, page
        assert pages < 10, "no end to the listing"


@pytest.mark.asyncio
async def test_a_directory_longer_than_one_page_is_listed_with_the_token(
    files_mcp: FastMCP, probe: str
) -> None:
    """LCp's shape: 11 files and 3 subdirectories (two empty), 14 entries for a page of 10. It
    settles that a token page is accepted (the tool no longer sends offset with it), and prints
    the order of the entries (LCp assumes name order puts the empty directories last)."""
    base = f"{probe}/listing"
    for day in range(1, 12):
        await create_file(f"{base}/log_202001{day:02d}.dat", f"log {day}\n", create_parents=True)
    await create_file(f"{base}/calib_runs/log_20200112.dat", "log 12\n", create_parents=True)
    for name in ("raw_frames", "spare_slots"):
        result, text = await _call(files_mcp, "create_directory", {"path": f"{base}/{name}"})
        assert result.is_error is False, text

    result, text = await _call(files_mcp, "list_children", {"parent_id_or_path": base})
    first = _payload(result, text)
    print(f"first page (default limit): {[c['name'] for c in first['children']]}")
    print(f"isLast {first['isLast']}, nextPageToken given: {bool(first['nextPageToken'])}")
    assert len(first["children"]) == 10 and first["isLast"] is False

    names, pages = await _all_pages(files_mcp, base)
    print(f"all entries in order, {pages} pages: {names}")
    assert sorted(names) == sorted(
        [f"log_202001{d:02d}.dat" for d in range(1, 12)]
        + ["calib_runs", "raw_frames", "spare_slots"]
    )

    result, text = await _call(
        files_mcp, "list_files_recursively", {"parent_id_or_path": base, "limit": 100}
    )
    listed = [f["path"] for f in _payload(result, text)["files"]]
    print(f"recursive file listing: {listed}")
    # LC and LCp "require" list_children because this listing shows no empty directory
    assert not any(p.rstrip("/").endswith(("raw_frames", "spare_slots")) for p in listed)

    result, text = await _call(
        files_mcp, "list_children", {"parent_id_or_path": base, "offset": 10, "limit": 10}
    )
    print(f"offset 10: {[c['name'] for c in _payload(result, text)['children']]}")


@pytest.mark.asyncio
async def test_listing_errors_and_an_empty_directory_as_the_model_reads_them(
    files_mcp: FastMCP, probe: str
) -> None:
    result, text = await _call(
        files_mcp, "list_children", {"parent_id_or_path": f"{probe}/seed.txt"}
    )
    print(f"listing a regular file answered: is_error={result.is_error} {text}")
    if result.is_error:
        assert "regular file, not a directory" in text or "not found" not in text

    result, text = await _call(
        files_mcp, "list_children", {"parent_id_or_path": f"{probe}/no-such-dir"}
    )
    print(f"listing a missing path answered: {text}")
    assert result.is_error is True and "not found" in text

    result, text = await _call(files_mcp, "create_directory", {"path": f"{probe}/empty"})
    assert result.is_error is False, text
    result, text = await _call(files_mcp, "list_children", {"parent_id_or_path": f"{probe}/empty"})
    page = _payload(result, text)
    print(f"an empty directory: {page}")
    assert page["children"] == [] and page["isLast"] is True

    result, text = await _call(
        files_mcp, "list_children", {"parent_id_or_path": probe, "token": "not-a-token"}
    )
    print(f"a made-up token answered: {text}")
