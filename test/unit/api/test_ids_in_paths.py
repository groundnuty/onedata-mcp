"""An id goes into a URL path percent-encoded and guarded, in every tool (issue #44).

Unencoded, a `?` or `#` in an id cuts the request path short, so the call acts on the object
named by the part before it (`delete_file` deletes the wrong object), a `/` adds segments, and
the HTTP client resolves `..` to the API root. `utils.id_segment` is the one helper; this file
checks it at every call site of every tool: `?`, `#` and `/` in an id, an empty id, `.` and `..`
(refused before any request), and a normal id (the request URL is the same as ever).

The tests call the API functions: through an MCP client without roots, the path resolver of the
MCP layer refuses most of these values before the API function is reached.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import files, harvesters, qos, spaces, transfers
from onedata_mcp.utils import id_segment

PROVIDER = "https://provider.example/api/v3/oneprovider"
ZONE = "https://onezone.example/api/v3/onezone"
ID = "0000000000AbCdEf0123456789"  # a normal id: its URL must not change


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


# --- the helper -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (ID, ID),
        ("9742830720c0ef94496dad1d96595736ch776e", "9742830720c0ef94496dad1d96595736ch776e"),
        ("ab?c", "ab%3Fc"),
        ("ab#c", "ab%23c"),
        ("a b", "a%20b"),
        ("a%2Fb", "a%252Fb"),  # a literal `%` is itself encoded
        ("...", "..."),  # only `.` and `..` are dot segments
        (" x ", "%20x%20"),  # sent as given, not stripped
    ],
)
def test_id_segment_encodes_one_segment(value: str, encoded: str) -> None:
    assert id_segment(value) == encoded


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("", "is empty"),
        ("  \t", "is empty"),
        (".", "cannot name an object"),
        ("..", "cannot name an object"),
        (" .. ", "cannot name an object"),
        ("a/b", "contains '/'"),
        ("/", "contains '/'"),
        ("%2e%2e/x", "contains '/'"),
    ],
)
def test_id_segment_refuses_what_cannot_be_one_segment(value: str, needle: str) -> None:
    with pytest.raises(ValueError, match=re.escape(needle)) as caught:
        id_segment(value, "transfer id", hint="Give the id.")
    assert str(caught.value).startswith("transfer id")
    assert str(caught.value).endswith("Give the id.")


def test_id_segment_cuts_a_long_value_in_its_error() -> None:
    with pytest.raises(ValueError) as caught:
        id_segment("a/" + "b" * 500)
    assert len(str(caught.value)) < 250


# --- every call site --------------------------------------------------------------------------


class Site(NamedTuple):
    """One place a tool puts an id into a request path."""

    name: str
    method: str
    base: str  # the service's API root
    template: str  # the request path; `{}` is where the id goes
    call: Callable[[str], Awaitable[Any]]
    reply: Any = {}  # noqa: RUF012 - a JSON answer the function can read
    status: int = 200
    takes_paths: bool = False  # a value starting with `/` is a path, resolved first


SITES = [
    # files: the stable tools that share `_normalize_path_to_file_id`
    Site(
        "delete_file",
        "DELETE",
        PROVIDER,
        "/data/{}",
        lambda v: files.delete_file(v),
        None,
        204,
        True,
    ),
    Site(
        "get_file_metadata",
        "GET",
        PROVIDER,
        "/data/{}/metadata/json",
        lambda v: files.get_file_metadata(v, ["json"]),
        takes_paths=True,
    ),
    Site(
        "set_file_metadata",
        "PUT",
        PROVIDER,
        "/data/{}/metadata/json",
        lambda v: files.set_file_metadata(v, "json", "{}"),
        takes_paths=True,
    ),
    Site(
        "set_file_xattrs",
        "PUT",
        PROVIDER,
        "/data/{}/metadata/xattrs",
        lambda v: files.set_file_xattrs(v, {"k": "v"}),
        takes_paths=True,
    ),
    Site(
        "get_file_distribution",
        "GET",
        PROVIDER,
        "/data/{}/distribution",
        lambda v: files.get_file_distribution(v),
        takes_paths=True,
    ),
    Site(
        "list_files_recursively",
        "GET",
        PROVIDER,
        "/data/{}/files",
        lambda v: files.list_files_recursively(v, limit=10),
        {"files": []},
        takes_paths=True,
    ),
    # qos
    Site(
        "get_file_qos_summary",
        "GET",
        PROVIDER,
        "/data/{}/qos/summary",
        lambda v: qos.get_file_qos_summary(v),
        {"requirements": {}, "status": "fulfilled"},
        takes_paths=True,
    ),
    # transfers
    Site("get_transfer", "GET", PROVIDER, "/transfers/{}", lambda v: transfers.get_transfer(v)),
    Site(
        "list_space_transfers",
        "GET",
        PROVIDER,
        "/spaces/{}/transfers",
        lambda v: transfers.list_space_transfers(v),
        {"transfers": []},
    ),
    # spaces
    Site(
        "get_space_providers",
        "GET",
        PROVIDER,
        "/spaces/{}",
        lambda v: spaces.get_space_providers(v),
    ),
    Site("get_space_details", "GET", ZONE, "/spaces/{}", lambda v: spaces.get_space_details(v)),
    Site(
        "get_marketplace_space_details",
        "GET",
        ZONE,
        "/spaces/marketplace/{}",
        lambda v: spaces.get_marketplace_space_details(v),
    ),
    # harvesters
    Site(
        "get_user_harvester",
        "GET",
        ZONE,
        "/user/harvesters/{}",
        lambda v: harvesters.get_user_harvester(v),
    ),
    Site(
        "list_harvester_indices",
        "GET",
        ZONE,
        "/harvesters/{}/indices",
        lambda v: harvesters.list_harvester_indices(v),
        {"indices": []},
    ),
    Site(
        "get_harvester_index (harvester id)",
        "GET",
        ZONE,
        "/harvesters/{}/indices/ix1",
        lambda v: harvesters.get_harvester_index(v, "ix1"),
    ),
    Site(
        "get_harvester_index (index id)",
        "GET",
        ZONE,
        "/harvesters/h1/indices/{}",
        lambda v: harvesters.get_harvester_index("h1", v),
    ),
    Site(
        "get_harvester_index_schema (harvester id)",
        "GET",
        ZONE,
        "/harvesters/{}/indices/ix1",
        lambda v: harvesters.get_harvester_index_schema(v, "ix1"),
    ),
    Site(
        "get_harvester_index_schema (index id)",
        "GET",
        ZONE,
        "/harvesters/h1/indices/{}",
        lambda v: harvesters.get_harvester_index_schema("h1", v),
    ),
    Site(
        "query_harvester_index (harvester id)",
        "POST",
        ZONE,
        "/harvesters/{}/indices/ix1/query",
        lambda v: harvesters.query_harvester_index(v, "ix1", {"method": "get"}),
    ),
    Site(
        "query_harvester_index (index id)",
        "POST",
        ZONE,
        "/harvesters/h1/indices/{}/query",
        lambda v: harvesters.query_harvester_index("h1", v, {"method": "get"}),
    ),
]
SITE_IDS = [site.name for site in SITES]


def sent_path(request: Any) -> str:
    """The path of a request as it went on the wire, query string left out."""
    return request.url.raw_path.split(b"?")[0].decode()


def reply(httpx_mock: HTTPXMock, site: Site) -> None:
    if site.status == 204:
        httpx_mock.add_response(method=site.method, status_code=204)
    else:
        httpx_mock.add_response(method=site.method, json=site.reply, status_code=site.status)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", SITES, ids=SITE_IDS)
async def test_a_normal_id_goes_into_the_path_unchanged(site: Site, httpx_mock: HTTPXMock) -> None:
    reply(httpx_mock, site)

    await site.call(ID)

    (sent,) = httpx_mock.get_requests()
    assert sent.method == site.method
    assert str(sent.url).split("?")[0] == site.base + site.template.format(ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", SITES, ids=SITE_IDS)
@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("ab?c", "ab%3Fc"),
        ("ab#c", "ab%23c"),
        ("ID?x=1#frag", "ID%3Fx%3D1%23frag"),
        ("a b", "a%20b"),
    ],
)
async def test_an_id_is_percent_encoded_so_it_cannot_change_the_request(
    site: Site, value: str, encoded: str, httpx_mock: HTTPXMock
) -> None:
    """Unencoded, the path would end at the `?` or `#` and name the object before it."""
    reply(httpx_mock, site)

    await site.call(value)

    (sent,) = httpx_mock.get_requests()
    assert sent_path(sent) == "/api/v3/" + site.base.split("/api/v3/")[1] + site.template.format(
        encoded
    )
    # the id's `?` and `#` did not turn into a query or a fragment of their own
    assert b"x=1" not in sent.url.query


@pytest.mark.asyncio
@pytest.mark.parametrize("site", SITES, ids=SITE_IDS)
@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("", "is empty"),
        ("   ", "is empty"),
        (".", "cannot name an object"),
        ("..", "cannot name an object"),
        ("a/b", "contains '/'"),
        ("ab/children", "contains '/'"),
        ("../x", "contains '/'"),
    ],
)
async def test_an_unusable_id_is_refused_before_any_request(
    site: Site, value: str, needle: str, httpx_mock: HTTPXMock
) -> None:
    """`/data/..` is the API root; `a/b` would be two segments."""
    with pytest.raises(ValueError, match=re.escape(needle)):
        await site.call(value)

    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "site", [site for site in SITES if not site.takes_paths], ids=lambda site: site.name
)
async def test_an_id_that_starts_with_a_slash_is_refused_where_there_is_no_path(
    site: Site, httpx_mock: HTTPXMock
) -> None:
    with pytest.raises(ValueError, match=re.escape("contains '/'")):
        await site.call("/a")

    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "site", [site for site in SITES if site.takes_paths], ids=lambda site: site.name
)
async def test_a_path_is_resolved_first_and_the_id_it_gives_is_used(
    site: Site, httpx_mock: HTTPXMock
) -> None:
    """Paths keep their handling: one lookup, the path encoded whole, then the call by id."""
    httpx_mock.add_response(
        method="POST",
        url=f"{PROVIDER}/lookup-file-id/%2FAlpha%2Fa%3Fb%23c.dat",
        json={"fileId": ID},
    )
    reply(httpx_mock, site)

    await site.call("/Alpha/a?b#c.dat")

    lookup, sent = httpx_mock.get_requests()
    assert sent_path(lookup) == "/api/v3/oneprovider/lookup-file-id/%2FAlpha%2Fa%3Fb%23c.dat"
    assert sent_path(sent) == "/api/v3/oneprovider" + site.template.format(ID)


# --- delete_file: the call that could delete the wrong object ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "encoded"), [("abc?x", "abc%3Fx"), ("abc#frag", "abc%23frag"), ("abc?", "abc%3F")]
)
async def test_delete_file_never_deletes_the_object_before_the_question_mark(
    value: str, encoded: str, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="DELETE", status_code=204)

    await files.delete_file(value)

    (sent,) = httpx_mock.get_requests()
    assert sent.method == "DELETE"
    assert sent_path(sent) == f"/api/v3/oneprovider/data/{encoded}"
    assert sent_path(sent) != "/api/v3/oneprovider/data/abc"
    assert sent.url.query == b"" and sent.url.fragment == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["..", ".", "", " ", "a/b", "abc/../x"])
async def test_delete_file_refuses_an_unusable_id_without_a_request(
    value: str, httpx_mock: HTTPXMock
) -> None:
    with pytest.raises(ValueError):
        await files.delete_file(value)

    assert httpx_mock.get_requests() == []


# --- the metadata type is in the path too -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_type", ["json", "rdf", "xattrs"])
async def test_a_valid_metadata_type_is_unchanged(
    metadata_type: str, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="PUT", json={})

    await files.set_file_metadata(ID, metadata_type, "{}")

    (sent,) = httpx_mock.get_requests()
    assert sent_path(sent) == f"/api/v3/oneprovider/data/{ID}/metadata/{metadata_type}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata_type", "encoded"), [("json?x", "json%3Fx"), ("json#x", "json%23x")]
)
async def test_set_file_metadata_encodes_the_metadata_type(
    metadata_type: str, encoded: str, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="PUT", json={})

    await files.set_file_metadata(ID, metadata_type, "{}")

    (sent,) = httpx_mock.get_requests()
    assert sent_path(sent) == f"/api/v3/oneprovider/data/{ID}/metadata/{encoded}"


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_type", ["", " ", ".", "..", "json/x", "../json"])
async def test_set_file_metadata_refuses_an_unusable_metadata_type(
    metadata_type: str, httpx_mock: HTTPXMock
) -> None:
    with pytest.raises(ValueError, match="metadata type"):
        await files.set_file_metadata(ID, metadata_type, "{}")

    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_get_file_metadata_asks_each_type_with_the_encoded_id(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(method="GET", json={"a": 1}, is_reusable=True)

    out = await files.get_file_metadata("ab?c", ["json", "xattrs"])

    assert out == {"json": {"a": 1}, "xattrs": {"a": 1}}
    assert [sent_path(r) for r in httpx_mock.get_requests()] == [
        "/api/v3/oneprovider/data/ab%3Fc/metadata/json",
        "/api/v3/oneprovider/data/ab%3Fc/metadata/xattrs",
    ]


# --- download_file reads the attributes first, then the content -------------------------------


@pytest.mark.asyncio
async def test_download_file_uses_the_normal_id_unchanged(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{PROVIDER}/data/{ID}", json={"type": "REG", "size": 3}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{PROVIDER}/data/{ID}/content",
        content=b"abc",
        headers={"Content-Type": "text/plain"},
    )

    assert await files.download_file(ID) == b"abc"

    assert [str(r.url) for r in httpx_mock.get_requests()] == [
        f"{PROVIDER}/data/{ID}",
        f"{PROVIDER}/data/{ID}/content",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "encoded"), [("ab?c", "ab%3Fc"), ("ab#c", "ab%23c")])
async def test_download_file_encodes_the_id_in_both_requests(
    value: str, encoded: str, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{PROVIDER}/data/{encoded}", json={"type": "REG", "size": 3}
    )
    httpx_mock.add_response(method="GET", url=f"{PROVIDER}/data/{encoded}/content", content=b"abc")

    assert await files.download_file(value) == b"abc"

    assert [sent_path(r) for r in httpx_mock.get_requests()] == [
        f"/api/v3/oneprovider/data/{encoded}",
        f"/api/v3/oneprovider/data/{encoded}/content",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   ", ".", "..", "a/b", "ab/content", "../x"])
async def test_download_file_refuses_an_unusable_id_before_the_attributes_request(
    value: str, httpx_mock: HTTPXMock
) -> None:
    with pytest.raises(ValueError):
        await files.download_file(value)

    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_grep_file_content_goes_through_the_same_guard(httpx_mock: HTTPXMock) -> None:
    with pytest.raises(ValueError, match="contains '/'"):
        await files.grep_file_content("a/b", "x")

    assert httpx_mock.get_requests() == []


# --- create_file: the parent is an id or a path ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("parent", "encoded"), [("ab?c", "ab%3Fc"), ("ab#c", "ab%23c"), (ID, ID)])
async def test_create_file_encodes_the_parent_id(
    parent: str, encoded: str, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="POST", json={"fileId": "new"})

    assert await files.create_file(f"{parent}/a.txt", "x") == "new"

    (sent,) = httpx_mock.get_requests()
    assert sent_path(sent) == f"/api/v3/oneprovider/data/{encoded}/children"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "needle"),
    [("/a.txt", "is empty"), ("../a.txt", "cannot name"), (" /a.txt", "is empty")],
)
async def test_create_file_refuses_an_unusable_parent_id_before_any_request(
    path: str, needle: str, httpx_mock: HTTPXMock
) -> None:
    with pytest.raises(ValueError, match=needle):
        await files.create_file(path, "x")

    assert httpx_mock.get_requests() == []


# --- space ids that a tool resolved first ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "encoded"), [(ID, ID), ("ab?c", "ab%3Fc"), ("ab#c", "ab%23c")])
async def test_list_space_datasets_encodes_the_resolved_space_id(
    value: str, encoded: str, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolved(_: str) -> str:
        return value

    monkeypatch.setattr(spaces, "resolve_space_id_or_name", resolved)
    httpx_mock.add_response(method="GET", json={"datasets": []})

    await spaces.list_space_datasets("a space name")

    (sent,) = httpx_mock.get_requests()
    assert sent_path(sent) == f"/api/v3/oneprovider/spaces/{encoded}/datasets"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "..", "a/b"])
async def test_list_space_datasets_refuses_an_unusable_resolved_space_id(
    value: str, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolved(_: str) -> str:
        return value

    monkeypatch.setattr(spaces, "resolve_space_id_or_name", resolved)

    with pytest.raises(ValueError):
        await spaces.list_space_datasets("a space name")

    assert httpx_mock.get_requests() == []
