import re
from urllib.parse import quote

import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import files
from onedata_mcp.utils import OnedataInvalidSpaceError


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


@pytest.fixture
def available_spaces(request: pytest.FixtureRequest) -> list[str]:
    return getattr(request, "param", ["space"])


@pytest.fixture(autouse=True)
def _mock_spaces_for_tests(httpx_mock: HTTPXMock, available_spaces: list[str]) -> None:
    _mock_available_spaces(httpx_mock, available_spaces)


@pytest.mark.asyncio
async def test_get_file_id_encodes_path_and_returns_id(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/my dir"),
        json={"fileId": "fid-123"},
    )

    result = await files.get_file_id("/space/my dir")

    assert result == "fid-123"


@pytest.mark.asyncio
async def test_get_file_id_maps_enoent_to_file_not_found(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/missing"),
        status_code=400,
        json={"error": {"details": {"errno": "enoent"}}},
    )

    with pytest.raises(FileNotFoundError):
        await files.get_file_id("/space/missing")


@pytest.mark.asyncio
async def test_get_file_attributes_sends_selected_attributes(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/path"),
        json={"fileId": "file-id"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/file-id",
        json={"name": "x"},
    )

    result = await files.get_file_attributes("/space/path", attributes=["name", "size"])

    assert result == {"name": "x"}
    provider_requests = [r for r in httpx_mock.get_requests() if r.url.host == "provider.example"]
    assert provider_requests[1].method == "GET"
    assert provider_requests[1].url.path == "/api/v3/oneprovider/data/file-id"
    assert provider_requests[1].content == b'{"attributes":["name","size"]}'


@pytest.mark.asyncio
async def test_list_children_applies_default_attributes_when_none(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/path"),
        json={"fileId": "parent-id"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/parent-id/children",
        json={"children": [], "isLast": True, "nextPageToken": None},
    )

    await files.list_children("/space/path", attributes=None, limit=10, offset=0)

    request = next(
        r for r in httpx_mock.get_requests() if r.url.path.endswith("/data/parent-id/children")
    )
    assert b'"attributes":' in request.content


@pytest.mark.asyncio
async def test_list_files_recursively_applies_default_attributes_when_none(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/path"),
        json={"fileId": "parent-id"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/parent-id/files",
        json={"files": [], "isLast": True, "nextPageToken": None},
    )

    await files.list_files_recursively("/space/path", attributes=None, limit=10)

    request = next(
        r for r in httpx_mock.get_requests() if r.url.path.endswith("/data/parent-id/files")
    )
    assert b'"attributes":' in request.content


@pytest.mark.asyncio
async def test_list_children_rejects_deprecated_attribute_names(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/path"),
        json={"fileId": "parent-id"},
    )

    with pytest.raises(ValueError, match="Deprecated attribute names"):
        await files.list_children("/space/path", attributes=["file_id"], limit=10, offset=0)


@pytest.mark.asyncio
async def test_list_files_recursively_rejects_deprecated_attribute_names(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/path"),
        json={"fileId": "parent-id"},
    )

    with pytest.raises(ValueError, match="Deprecated attribute names"):
        await files.list_files_recursively("/space/path", attributes=["mode"], limit=10)


@pytest.mark.asyncio
async def test_list_children_filters_deprecated_response_fields(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/path"),
        json={"fileId": "parent-id"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/parent-id/children",
        json={
            "children": [
                {"name": "a.txt", "file_id": "old-id", "fileId": "new-id"},
                {"name": "b.txt", "mode": "0777", "posixPermissions": "0777"},
            ],
            "isLast": True,
            "nextPageToken": None,
        },
    )

    result = await files.list_children("/space/path", limit=10, offset=0)

    assert result["children"][0]["fileId"] == "new-id"
    assert "file_id" not in result["children"][0]
    assert "mode" not in result["children"][1]
    assert result["children"][1]["posixPermissions"] == "0777"


@pytest.mark.asyncio
async def test_list_files_recursively_filters_deprecated_response_fields(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/path"),
        json={"fileId": "parent-id"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/parent-id/files",
        json={
            "files": [
                {"name": "a.txt", "file_id": "old-id", "fileId": "new-id"},
                {"name": "b.txt", "owner_id": "123", "ownerUserId": "123"},
            ],
            "isLast": True,
            "nextPageToken": None,
        },
    )

    result = await files.list_files_recursively("/space/path", limit=10)

    assert result["files"][0]["fileId"] == "new-id"
    assert "file_id" not in result["files"][0]
    assert "owner_id" not in result["files"][1]
    assert result["files"][1]["ownerUserId"] == "123"


@pytest.mark.asyncio
@pytest.mark.parametrize("available_spaces", [["Alpha", "Beta"]], indirect=True)
async def test_list_files_recursively_formats_invalid_space_error_with_hints(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/dsadas/files",
        status_code=400,
        json={
            "error": {
                "id": "spaceNotSupportedBy",
                "details": {"spaceId": "dsadas", "providerId": "provider-1"},
            }
        },
    )

    with pytest.raises(OnedataInvalidSpaceError, match='Space "dsadas" does not exist') as exc:
        await files.list_files_recursively("dsadas", limit=10)

    assert 'Available spaces: "Alpha", "Beta".' in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("available_spaces", [["Alpha"]], indirect=True)
async def test_list_children_formats_invalid_space_error_with_hints(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/dsadas/children",
        status_code=400,
        json={
            "error": {
                "id": "spaceNotSupportedBy",
                "details": {"spaceId": "dsadas", "providerId": "provider-1"},
            }
        },
    )

    with pytest.raises(OnedataInvalidSpaceError, match='Space "dsadas" does not exist') as exc:
        await files.list_children("dsadas", limit=10, offset=0)

    assert 'Available spaces: "Alpha".' in str(exc.value)


@pytest.mark.asyncio
async def test_download_file_rejects_directory(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/dir"),
        json={"fileId": "fid-dir"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-dir",
        json={"type": "DIR", "size": 10},
    )

    with pytest.raises(ValueError, match="directory"):
        await files.download_file("/space/dir")


@pytest.mark.asyncio
async def test_download_file_rejects_large_files(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/big"),
        json={"fileId": "fid-big"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-big",
        json={"type": "REG", "size": 6 * 1024 * 1024},
    )

    with pytest.raises(ValueError, match="too large"):
        await files.download_file("/space/big")


@pytest.mark.asyncio
async def test_download_file_uses_httpx_async_client(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/file.txt"),
        json={"fileId": "fid-1"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-1",
        json={"type": "REG", "size": 3},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-1/content",
        text="abc",
        headers={"Content-Type": "text/plain"},
    )

    result = await files.download_file("/space/file.txt")

    assert result == b"abc"
    content_req = httpx_mock.get_requests()[-1]
    assert content_req.url.path == "/api/v3/oneprovider/data/fid-1/content"
    assert content_req.headers["Accept"] == "*/*"
    assert "Content-Type" not in content_req.headers


@pytest.mark.asyncio
async def test_get_file_metadata_fetches_each_type_with_rdf_accept_header(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/a"),
        json={"fileId": "fid-meta"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-meta/metadata/json",
        json={"k": "v"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-meta/metadata/rdf",
        text="<rdf/>",
        headers={"Content-Type": "application/rdf+xml"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-meta/metadata/xattrs",
        json={"x": "1"},
    )

    result = await files.get_file_metadata("/space/a", ["json", "rdf", "xattrs"])

    assert result["json"] == {"k": "v"}
    assert result["rdf"] == "<rdf/>"
    assert result["xattrs"] == {"x": "1"}
    rdf_req = next(
        r for r in httpx_mock.get_requests() if r.url.path.endswith("/data/fid-meta/metadata/rdf")
    )
    assert rdf_req.url.path == "/api/v3/oneprovider/data/fid-meta/metadata/rdf"
    assert rdf_req.headers["Accept"] == "application/rdf+xml"


@pytest.mark.asyncio
async def test_get_file_metadata_maps_enodata_to_none(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/a"),
        json={"fileId": "fid-meta"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-meta/metadata/rdf",
        status_code=400,
        json={"error": {"details": {"errno": "enodata"}}},
    )

    result = await files.get_file_metadata("/space/a", ["rdf"])

    assert result == {"rdf": None}


@pytest.mark.asyncio
async def test_get_file_metadata_rejects_invalid_type(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/a"),
        json={"fileId": "fid-meta"},
    )
    with pytest.raises(ValueError, match="Unsupported metadata type"):
        await files.get_file_metadata("/space/a", ["json", "bad"])


@pytest.mark.asyncio
async def test_set_file_metadata_sets_content_type(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/a"),
        json={"fileId": "fid-set"},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://provider.example/api/v3/oneprovider/data/fid-set/metadata/rdf",
        json={},
    )
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/a"),
        json={"fileId": "fid-set"},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://provider.example/api/v3/oneprovider/data/fid-set/metadata/json",
        json={},
    )

    await files.set_file_metadata("/space/a", "rdf", "<rdf/>")
    await files.set_file_metadata("/space/a", "json", '{"a":1}')

    put_requests = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.host == "provider.example"
    ]
    assert put_requests[0].headers["Content-Type"] == "application/rdf+xml"
    assert put_requests[1].headers["Content-Type"] == "application/json"
    assert put_requests[0].content == b"<rdf/>"
    assert put_requests[1].content == b'{"a":1}'


@pytest.mark.asyncio
async def test_create_file_posts_child_when_create_parents_false(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/parent"),
        json={"fileId": "parent-id"},
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"https://provider\.example/api/v3/oneprovider/data/parent-id/children\?.*"),
        json={"fileId": "new-fid"},
    )

    fid = await files.create_file("/space/parent/note.txt", "hello")

    assert fid == "new-fid"
    post = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.host == "provider.example" and "/children" in r.url.path
    )
    assert post.url.params["name"] == "note.txt"


@pytest.mark.asyncio
async def test_create_file_put_path_when_create_parents_true(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space"),
        json={"fileId": "root-id"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=re.compile(
            r"https://provider\.example/api/v3/oneprovider/data/root-id/path/results/d\.csv\?.*"
        ),
        json={"fileId": "nested-fid"},
    )

    fid = await files.create_file("/space/results/d.csv", "x", create_parents=True)

    assert fid == "nested-fid"
    put = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.host == "provider.example" and "/path/" in r.url.path
    )
    assert put.url.params["create_parents"] == "true"
    assert put.content == b"x"


# ---------------------------------------------------------------------------
# get_file_distribution (Onedata 25.0, GET /data/{id}/distribution)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_distribution_returns_per_provider_blocks(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    file_id = "094576776E66743172323067776777"
    httpx_mock.add_response(
        method="GET",
        url=f"https://provider.example/api/v3/oneprovider/data/{file_id}/distribution",
        json={
            "type": "REG",
            "distributionPerProvider": {
                "p_de": {
                    "success": True,
                    "logicalSize": 8,
                    "locationsPerStorageBackend": {
                        "s1": {"success": True, "location": "/file/loc/de"}
                    },
                    "distributionPerStorageBackend": {
                        "s1": {"success": True, "blocks": [[0, 4], [6, 2]], "physicalSize": 6}
                    },
                },
                "p_pl": {
                    "success": True,
                    "logicalSize": 8,
                    "locationsPerStorageBackend": {"s2": {"success": True, "location": None}},
                    "distributionPerStorageBackend": {
                        "s2": {"success": True, "blocks": [], "physicalSize": 0}
                    },
                },
            },
        },
    )

    result = await files.get_file_distribution(file_id)

    assert result["type"] == "REG"
    assert "p_de" in result["distributionPerProvider"]
    assert "p_pl" in result["distributionPerProvider"]
    assert result["distributionPerProvider"]["p_de"]["distributionPerStorageBackend"]["s1"][
        "blocks"
    ] == [[0, 4], [6, 2]]
    # Zero-block providers must surface (so agents can distinguish "not replicated" from "unknown")
    assert (
        result["distributionPerProvider"]["p_pl"]["distributionPerStorageBackend"]["s2"]["blocks"]
        == []
    )


@pytest.mark.asyncio
async def test_get_file_distribution_resolves_path_via_lookup_first(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    file_id = "abcdefghijklmnopqrstuvwxyz0123456789"
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/myspace/big.bin"),
        json={"fileId": file_id},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"https://provider.example/api/v3/oneprovider/data/{file_id}/distribution",
        json={"type": "REG", "distributionPerProvider": {}},
    )

    result = await files.get_file_distribution("/myspace/big.bin")

    assert result["type"] == "REG"


# ---------------------------------------------------------------------------
# move_file (CDMI PUT against /cdmi/{space}/{path} on the oneprovider host)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_file_issues_cdmi_put_with_correct_body_and_headers(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    # CDMI PUT to the destination
    httpx_mock.add_response(
        method="PUT",
        url="https://provider.example/cdmi/myspace/newfile.txt",
        status_code=204,
    )
    # Subsequent get_file_id lookup for the destination
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/myspace/newfile.txt"),
        json={"fileId": "destFid"},
    )

    result = await files.move_file("/myspace/oldfile.txt", "/myspace/newfile.txt")

    assert result == "destFid"
    cdmi_request = next(
        r for r in httpx_mock.get_requests() if r.url.path == "/cdmi/myspace/newfile.txt"
    )
    assert cdmi_request.method == "PUT"
    assert cdmi_request.headers["X-CDMI-Specification-Version"] == "1.1.1"
    assert cdmi_request.headers["Content-Type"] == "application/cdmi-object"
    assert cdmi_request.headers["X-Auth-Token"] == "token"
    import json as _json

    assert _json.loads(cdmi_request.content) == {"move": "myspace/oldfile.txt"}


@pytest.mark.asyncio
async def test_move_file_handles_nested_paths_and_url_encodes_spaces(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="PUT",
        url="https://provider.example/cdmi/myspace/dir%20with%20spaces/file.bin",
        status_code=204,
    )
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/myspace/dir with spaces/file.bin"),
        json={"fileId": "destFid"},
    )

    result = await files.move_file(
        "/myspace/datasets/file.bin",
        "/myspace/dir with spaces/file.bin",
    )

    assert result == "destFid"


@pytest.mark.asyncio
async def test_move_file_rejects_cross_space_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(ValueError, match="Cross-space moves"):
        await files.move_file("/space_a/foo.txt", "/space_b/foo.txt")


@pytest.mark.asyncio
async def test_move_file_rejects_invalid_path_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(ValueError, match="logical path"):
        await files.move_file("relative/no_leading_slash.txt", "/space/x.txt")
    with pytest.raises(ValueError, match="logical path"):
        await files.move_file("/space/x.txt", "/no_inner_path")


@pytest.mark.asyncio
async def test_move_file_surfaces_cdmi_server_error(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="PUT",
        url="https://provider.example/cdmi/myspace/newfile.txt",
        status_code=500,
        text="oneprovider boom",
    )

    from onedata_mcp.utils import OnedataApiError

    with pytest.raises(OnedataApiError, match="CDMI move failed"):
        await files.move_file("/myspace/oldfile.txt", "/myspace/newfile.txt")


# ---------------------------------------------------------------------------
# M-10: download_file_with_meta returns (content, content_type)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_with_meta_returns_content_and_content_type(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/note.txt"),
        json={"fileId": "fid-meta"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-meta",
        json={"type": "REG", "size": 5},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-meta/content",
        text="hello",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )

    raw, ct = await files.download_file_with_meta("/space/note.txt")

    assert raw == b"hello"
    assert ct == "text/plain; charset=utf-8"


@pytest.mark.asyncio
async def test_download_file_with_meta_handles_missing_content_type(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space/raw.bin"),
        json={"fileId": "fid-raw"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-raw",
        json={"type": "REG", "size": 3},
    )
    # pytest_httpx auto-injects a Content-Type when using `text=` /  `json=`,
    # so use `content=` directly to control the response headers.
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid-raw/content",
        content=b"abc",
    )

    raw, ct = await files.download_file_with_meta("/space/raw.bin")

    # content_type may be None or whatever httpx defaults to — only assert
    # the body is correct (the wrapper-level test asserts the dict shape).
    assert raw == b"abc"
    # If pytest_httpx injects a default Content-Type, we still get a string
    # (not None); the wrapper test (test_modules_files.py) covers the shape.
    assert ct is None or isinstance(ct, str)


# ---------------------------------------------------------------------------
# M-11: create_directory wrapper for PUT /data/{space}/path/{rel}?type=DIR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_directory_puts_with_type_dir(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
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

    result = await files.create_directory("/space/archive")

    assert result == {"fileId": "dir-fid", "path": "/space/archive"}
    put = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.host == "provider.example" and "/path/" in r.url.path
    )
    assert put.url.params["type"] == "DIR"
    assert put.url.params["create_parents"] == "true"


@pytest.mark.asyncio
async def test_create_directory_propagates_create_parents_false(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup_url("/space"),
        json={"fileId": "root-id"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=re.compile(r"https://provider\.example/api/v3/oneprovider/data/root-id/path/d/sub\?.*"),
        json={"fileId": "dir-fid"},
    )

    await files.create_directory("/space/d/sub", create_parents=False)

    put = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.host == "provider.example" and "/path/" in r.url.path
    )
    assert put.url.params["type"] == "DIR"
    assert put.url.params["create_parents"] == "false"


@pytest.mark.asyncio
async def test_create_directory_rejects_space_root_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(ValueError, match="path must be /<space_name>/<path_to_directory>"):
        await files.create_directory("/space")


# ---------------------------------------------------------------------------
# M-11: _looks_like_directory_intent heuristic
# ---------------------------------------------------------------------------


def test_looks_like_directory_intent_flags_empty_no_extension() -> None:
    # The exact V3/GLM A4 trap shape.
    assert files._looks_like_directory_intent("/space/archive", "") is True
    assert files._looks_like_directory_intent("archive", "") is True


def test_looks_like_directory_intent_passes_legitimate_files() -> None:
    # Empty content with extension — legitimate empty file.
    assert files._looks_like_directory_intent("/space/empty.txt", "") is False
    # Non-empty content — not mkdir.
    assert files._looks_like_directory_intent("/space/archive", "x") is False
    # Hidden files (.gitignore) ARE allowed empty (intentional design).
    assert files._looks_like_directory_intent("/space/.gitignore", "") is False
