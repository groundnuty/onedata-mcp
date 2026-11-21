"""Live test of `get_file_attributes`, `list_children` and `create_directory` against the LOCAL
Compose federation (design/13, design/14, design/15).

Skipped unless ONEDATA_MCP_LIVE_FILES=1. With it set, the test REFUSES to run (it fails) unless
the Oneprovider host, and the Onezone host when one is set, is `127.0.0.1`, `localhost` or ends
in `.datahub.test` AND resolves only to loopback addresses (a `*.datahub.test` name mapped to any
other address is refused): it creates and deletes files and directories, and must never touch
production. Once the check passes, name resolution for those hosts is pinned to the addresses
that were checked and proxy variables are ignored, so the connection cannot go anywhere else
(the guard is shared with the other live tests: test/live/local_federation.py).

    make test-files-live      the usual ONEDATA_* variables, set by hand
    make valfed-files-live    against the project's own federation, in its space omcp-live

Environment (the usual four, plus the space to work in):

    ONEDATA_ONEPROVIDER_HOST, ONEDATA_ONEPROVIDER_TOKEN
    ONEDATA_ONEZONE_HOST, ONEDATA_ONEZONE_TOKEN
    ONEDATA_MCP_LIVE_FILES_SPACE   name of a space the local provider supports

Everything is made under one probe directory, `/<space>/files-live-<time>-<hex>`, which the test
creates exclusively (a taken name is retried, never adopted) and deletes by its id at the end; a
failed deletion is reported. The tools are called through an in-process MCP client, so what is
checked is what a model gets: the answer on success and the error text on failure. Each test
PRINTS what the unit tests cannot pin (they use fakes; the swagger gives only statuses): the real
error bodies, the form of `posixPermissions`, the unit of the times, the order of a listing. The
failure contracts expected of 25.0 are asserted and marked `TODO(first live run)`: pin them, or
correct them, from what the first run prints.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import time
from urllib.parse import quote

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP

from onedata_mcp.api.files import create_file, delete_file, get_file_id, list_children
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.modules import files as files_module
from onedata_mcp.utils import OnedataApiError, request
from test.live.local_federation import enforce_local_federation

ENABLE = "ONEDATA_MCP_LIVE_FILES"
SPACE_ENV = "ONEDATA_MCP_LIVE_FILES_SPACE"
PROBE_TRIES = 5
WHAT = "the live file-tools test writes files and directories"
# What a made-up pagination token must answer. The swagger lists 400 ("Invalid request") for the
# listing and gives no error id. TODO(first live run): confirm the status, and set the id from
# what the first run prints; an unrelated server error then cannot pass as "a bad token".
BAD_TOKEN_STATUS = 400
BAD_TOKEN_ERROR_ID: str | None = None

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE) != "1", reason=f"live test: set {ENABLE}=1 (local Compose only)"
)


def federation_urls() -> dict[str, str]:
    urls = {"ONEDATA_ONEPROVIDER_HOST": os.environ.get("ONEDATA_ONEPROVIDER_HOST", "")}
    if os.environ.get("ONEDATA_ONEZONE_HOST"):
        urls["ONEDATA_ONEZONE_HOST"] = os.environ["ONEDATA_ONEZONE_HOST"]
    return urls


def enforce_local(monkeypatch: pytest.MonkeyPatch, resolve=socket.getaddrinfo) -> None:  # noqa: ANN001
    """The shared guard over every host this test uses (`resolve` is for the unit tests)."""
    enforce_local_federation(monkeypatch, federation_urls(), resolve, what=WHAT)


@pytest.fixture(autouse=True)
def _local_federation_only(monkeypatch: pytest.MonkeyPatch) -> None:
    enforce_local(monkeypatch)


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


async def _new_probe_directory(space: str) -> tuple[str, str]:
    """A directory made EXCLUSIVELY under the space root, and its id. `POST /data/{root}/children`
    with `type=DIR` fails when the name is taken, so a directory that was already there is never
    adopted (and later deleted): a taken name is retried with a fresh one."""
    config = get_oneprovider_config()
    root_id = await get_file_id(f"/{space}")
    for _ in range(PROBE_TRIES):
        name = f"files-live-{int(time.time())}-{secrets.token_hex(4)}"
        try:
            made = await request(
                config,
                "POST",
                f"/data/{quote(root_id, safe='')}/children",
                params={"name": name, "type": "DIR"},
            )
        except OnedataApiError as e:
            status = (e.response or {}).get("status_code")
            if e.errno == "eexist" or status == 409:
                continue  # taken: never adopt it
            raise
        return f"/{space}/{name}", made["body"]["fileId"]
    pytest.fail(f"no free probe directory name in {space} after {PROBE_TRIES} tries")


@pytest_asyncio.fixture
async def probe(space: str):
    """A probe directory this test made exclusively, with `seed.txt` in it. It is deleted
    afterwards by the id it was made with, never by path, and a failed deletion fails the test's
    teardown: it is reported, not hidden."""
    path, probe_id = await _new_probe_directory(space)
    try:
        await create_file(f"{path}/seed.txt", "seed\n")
        yield path
    finally:
        await delete_file(probe_id)


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
    # TODO(first live run): pin 25.0's status and error id, printed above. Expected: a
    # structured not-found ('Path "..." not found'); a bare 404 gives the possible causes.
    assert result.is_error is True and ("not found" in text or "answered 404" in text)

    made_up = secrets.token_hex(16)
    result, text = await _call(files_mcp, "get_file_attributes", {"file_id_or_path": made_up})
    print(f"made-up id answered: {text}")
    # TODO(first live run): pin 25.0's status and error id, printed above (a structured
    # not-found, badValueIdentifier, or spaceNotSupportedBy for an id it cannot decode).
    assert result.is_error is True
    assert "Give a path, /<space_name>/<path>, or a file id." in text or "space" in text.lower()

    result, text = await _call(
        files_mcp,
        "get_file_attributes",
        {"file_id_or_path": f"{probe}/seed.txt", "attributes": ["owner"]},
    )
    print(f"unknown attribute name answered: {text}")
    # TODO(first live run): pin 25.0's status and error id, printed above.
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


async def _all_pages(mcp: FastMCP, path: str, **arguments) -> tuple[list[dict], int]:
    """Every entry, page by page with the token; and how many pages it took."""
    entries: list[dict] = []
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
        entries += page["children"]
        if page["isLast"]:
            return entries, pages
        assert page["nextPageToken"] and page["nextPageToken"] != token, page
        token = page["nextPageToken"]
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

    entries, pages = await _all_pages(files_mcp, base)
    names = [e["name"] for e in entries]
    print(f"all entries in order, {pages} pages: {names}")
    assert sorted(names) == sorted(
        [f"log_202001{d:02d}.dat" for d in range(1, 12)]
        + ["calib_runs", "raw_frames", "spare_slots"]
    )
    assert names[:10] == [c["name"] for c in first["children"]]  # the token walk starts there
    # LC expects a model to tell a subdirectory by its type: the three must carry DIR
    types = {e["name"]: e["type"] for e in entries}
    assert {d: types[d] for d in ("calib_runs", "raw_frames", "spare_slots")} == dict.fromkeys(
        ("calib_runs", "raw_frames", "spare_slots"), "DIR"
    )
    assert {types[n] for n in names if n.startswith("log_")} == {"REG"}

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
    assert result.is_error is False, text
    by_offset = [c["name"] for c in _payload(result, text)["children"]]
    print(f"offset 10: {by_offset}")
    assert by_offset == names[10:20]  # the same entries as the token walk's second page


@pytest.mark.asyncio
async def test_listing_errors_and_an_empty_directory_as_the_model_reads_them(
    files_mcp: FastMCP, probe: str
) -> None:
    result, text = await _call(
        files_mcp, "list_children", {"parent_id_or_path": f"{probe}/seed.txt"}
    )
    print(f"listing a regular file answered: is_error={result.is_error} {text}")
    # 25.0 does not refuse: it lists the file as its own only child (first live run, 2026-11-21).
    # The tool detects that and refuses, so a model never reads "the directory holds seed.txt".
    assert result.is_error is True, text
    assert f"'{probe}/seed.txt' is a regular file, not a directory; it has no children" in text

    result, text = await _call(
        files_mcp, "list_children", {"parent_id_or_path": f"{probe}/no-such-dir"}
    )
    print(f"listing a missing path answered: {text}")
    # TODO(first live run): pin 25.0's status and error id, printed above (as for attributes).
    assert result.is_error is True and ("not found" in text or "answered 404" in text)

    result, text = await _call(files_mcp, "create_directory", {"path": f"{probe}/empty"})
    assert result.is_error is False, text
    result, text = await _call(files_mcp, "list_children", {"parent_id_or_path": f"{probe}/empty"})
    page = _payload(result, text)
    print(f"an empty directory: {page}")
    assert page["children"] == [] and page["isLast"] is True

    result, text = await _call(
        files_mcp, "list_children", {"parent_id_or_path": probe, "token": "not-a-token"}
    )
    print(f"a made-up token answered: is_error={result.is_error} {text}")
    assert result.is_error is True, text
    assert "rejected" not in text and "reports nothing" not in text  # no id hint
    # The status and the error id are pinned, so that an unrelated failure (a server error, a
    # gateway page) cannot pass as "a bad token". The status is the swagger's; the id is
    # TODO(first live run): see BAD_TOKEN_ERROR_ID.
    with pytest.raises(OnedataApiError) as caught:
        await list_children(probe, limit=10, offset=0, token="not-a-token")
    status = (caught.value.response or {}).get("status_code")
    print(f"a made-up token: status {status}, error id {caught.value.error_id!r}")
    assert status == BAD_TOKEN_STATUS, f"expected {BAD_TOKEN_STATUS}, got {status}: {caught.value}"
    assert caught.value.error_id is not None, "not a structured Onedata error"
    if BAD_TOKEN_ERROR_ID is not None:
        assert caught.value.error_id == BAD_TOKEN_ERROR_ID


# =============================================================================================
# create_directory (design/15)
# =============================================================================================


async def _type_of(mcp: FastMCP, path: str) -> str:
    result, text = await _call(
        mcp, "get_file_attributes", {"file_id_or_path": path, "attributes": ["type"]}
    )
    assert result.is_error is False, text
    return _payload(result, text)["type"]


@pytest.mark.asyncio
async def test_a_directory_is_made_and_a_second_creation_says_it_exists(
    files_mcp: FastMCP, probe: str
) -> None:
    path = f"{probe}/archive"
    result, text = await _call(files_mcp, "create_directory", {"path": path})

    assert result.is_error is False, text
    made = _payload(result, text)
    assert made["path"] == path and made["fileId"] == await get_file_id(path)
    assert await _type_of(files_mcp, path) == "DIR"

    result, text = await _call(files_mcp, "create_directory", {"path": path})
    print(f"creating it again answered: {text}")
    assert result.is_error is True
    assert "already exists as a directory" in text


@pytest.mark.asyncio
async def test_a_regular_file_in_the_way_is_named_as_one(files_mcp: FastMCP, probe: str) -> None:
    """CDn's state: the error must say a regular file is there, not a directory."""
    path = f"{probe}/occupied"
    await create_file(path, "not a dir\n")

    result, text = await _call(files_mcp, "create_directory", {"path": path})

    print(f"a file in the way answered: {text}")
    assert result.is_error is True
    assert "already exists as a regular file, so no directory was created" in text
    assert await _type_of(files_mcp, path) == "REG"


@pytest.mark.asyncio
async def test_nested_paths_and_their_failures(files_mcp: FastMCP, probe: str) -> None:
    result, text = await _call(files_mcp, "create_directory", {"path": f"{probe}/2020/01"})
    assert result.is_error is False, text
    assert await _type_of(files_mcp, f"{probe}/2020") == "DIR"
    assert await _type_of(files_mcp, f"{probe}/2020/01") == "DIR"

    result, text = await _call(
        files_mcp,
        "create_directory",
        {"path": f"{probe}/missing/child", "create_parents": False},
    )
    print(f"a missing parent with create_parents false answered: {text}")
    assert result.is_error is True
    # TODO(first live run): pin 25.0's answer, printed above. A structured not-found gives
    # "The parent directory of ..."; a bare 404 gives the possible causes, "a parent may not
    # exist" among them.
    assert "parent" in text

    result, text = await _call(files_mcp, "create_directory", {"path": f"{probe}/seed.txt/sub"})
    print(f"a regular file on the path answered: {text}")
    # 25.0 answers 400 posix `eacces`, not `enotdir` (first live run, 2026-11-21); the tool reads
    # the components of the path and names the regular file instead of hinting at permissions.
    assert result.is_error is True
    assert (
        f"A component of the path, '{probe}/seed.txt', is a regular file, so nothing can be "
        "created under it"
    ) in text
    assert "read-only" not in text


@pytest.mark.asyncio
async def test_a_question_mark_and_a_hash_are_part_of_the_name(
    files_mcp: FastMCP, probe: str
) -> None:
    """Percent-encoded: before, `?` started a query and `#` a fragment."""
    path = f"{probe}/a?b#c"
    result, text = await _call(files_mcp, "create_directory", {"path": path})
    print(f"creating {path!r} answered: is_error={result.is_error} {text}")
    if result.is_error:
        return  # Onedata may refuse such a name; the error is printed above
    result, text = await _call(files_mcp, "list_children", {"parent_id_or_path": probe})
    names = [c["name"] for c in _payload(result, text)["children"]]
    assert "a?b#c" in names and "a" not in names
