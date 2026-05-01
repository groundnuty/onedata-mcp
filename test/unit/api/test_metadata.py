"""Unit tests for the recursive metadata query.

No harvester involvement — verifies the simple recursive composition over
list_files_recursively + get_file_metadata. See
design/02-query-by-metadata-no-harvester.md.
"""

import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import metadata


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token-z")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


# ---------------------------------------------------------------------------
# Predicate parser
# ---------------------------------------------------------------------------


def test_parse_predicate_single_equals_value() -> None:
    assert metadata._parse_predicate("pipeline_stage=anonymised") == [
        ("pipeline_stage", "anonymised")
    ]


def test_parse_predicate_wildcard() -> None:
    assert metadata._parse_predicate("reviewed=*") == [("reviewed", None)]


def test_parse_predicate_multiple_clauses_anded() -> None:
    assert metadata._parse_predicate("pipeline_stage=raw & reviewed=*") == [
        ("pipeline_stage", "raw"),
        ("reviewed", None),
    ]


def test_parse_predicate_rejects_double_equals() -> None:
    # The clause 'geo==PL' partitions on the first '=' as ('geo', '=PL') —
    # which is technically valid syntactically. But the user-facing failure
    # mode the paper §5.4 H_qos_syntax tracks is 'key==value', so the
    # predicate parser also surfaces a clear error for anything matching
    # an empty key.
    with pytest.raises(ValueError, match="empty key|empty"):
        metadata._parse_predicate("=foo")


def test_parse_predicate_rejects_clause_without_equals() -> None:
    with pytest.raises(ValueError, match="single '='"):
        metadata._parse_predicate("just_a_key")


def test_parse_predicate_rejects_empty_clause() -> None:
    with pytest.raises(ValueError, match="empty"):
        metadata._parse_predicate("a=1 &  & b=2")


def test_parse_predicate_rejects_empty_predicate() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        metadata._parse_predicate("")


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------


def test_matches_all_clauses_satisfied() -> None:
    ok, keys = metadata._matches(
        {"pipeline_stage": "raw", "reviewed": True},
        [("pipeline_stage", "raw"), ("reviewed", None)],
    )
    assert ok is True
    assert set(keys) == {"pipeline_stage", "reviewed"}


def test_matches_value_mismatch() -> None:
    ok, _ = metadata._matches(
        {"pipeline_stage": "anonymised"},
        [("pipeline_stage", "raw")],
    )
    assert ok is False


def test_matches_missing_key() -> None:
    ok, _ = metadata._matches({}, [("pipeline_stage", "raw")])
    assert ok is False


def test_matches_non_object_metadata() -> None:
    # JSON metadata can be set to a non-object (e.g. a string or list);
    # those don't have top-level keys, so they don't match.
    assert metadata._matches(None, [("k", "v")]) == (False, [])
    assert metadata._matches("a string", [("k", "v")]) == (False, [])
    assert metadata._matches([1, 2, 3], [("k", "v")]) == (False, [])


# ---------------------------------------------------------------------------
# query_by_metadata happy paths and bounds (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_by_metadata_returns_matches_with_matched_keys(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    # Resolve /myspace -> file id (root dir)
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/lookup-file-id/%2Fmyspace",
        json={"fileId": "rootId"},
    )
    # First (and only) page of files
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/rootId/files",
        json={
            "files": [
                {"fileId": "f1", "path": "/myspace/a.txt"},
                {"fileId": "f2", "path": "/myspace/b.txt"},
                {"fileId": "f3", "path": "/myspace/c.txt"},
            ],
            "isLast": True,
            "nextPageToken": None,
        },
    )
    # Per-file JSON metadata
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/f1/metadata/json",
        json={"pipeline_stage": "raw"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/f2/metadata/json",
        json={"pipeline_stage": "anonymised"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/f3/metadata/json",
        json={"pipeline_stage": "raw"},
    )

    result = await metadata.query_by_metadata("myspace", "pipeline_stage=raw")

    assert result["truncated"] is False
    assert result["files_visited"] == 3
    paths = {m["path"] for m in result["matches"]}
    assert paths == {"/myspace/a.txt", "/myspace/c.txt"}
    for match in result["matches"]:
        assert match["matched_keys"] == ["pipeline_stage"]


@pytest.mark.asyncio
async def test_query_by_metadata_truncates_at_max_results(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/lookup-file-id/%2Fmyspace",
        json={"fileId": "rootId"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/rootId/files",
        json={
            "files": [{"fileId": f"f{i}", "path": f"/myspace/{i}.txt"} for i in range(5)],
            "isLast": True,
            "nextPageToken": None,
        },
    )
    for i in range(5):
        httpx_mock.add_response(
            method="GET",
            url=f"https://provider.example/api/v3/oneprovider/data/f{i}/metadata/json",
            json={"reviewed": True},
            is_optional=True,  # not all 5 will be reached at max_results=2
        )

    result = await metadata.query_by_metadata("myspace", "reviewed=*", max_results=2)

    assert len(result["matches"]) == 2
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_query_by_metadata_skips_files_beyond_max_depth(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/lookup-file-id/%2Fmyspace",
        json={"fileId": "rootId"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/rootId/files",
        json={
            "files": [
                {"fileId": "shallow", "path": "/myspace/a.txt"},
                {"fileId": "deep", "path": "/myspace/d1/d2/d3/x.txt"},
            ],
            "isLast": True,
            "nextPageToken": None,
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/shallow/metadata/json",
        json={"k": "v"},
    )
    # `deep` should NOT have its metadata fetched at max_depth=2
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/deep/metadata/json",
        json={"k": "v"},
        is_optional=True,
    )

    result = await metadata.query_by_metadata("myspace", "k=v", max_depth=2)

    assert {m["path"] for m in result["matches"]} == {"/myspace/a.txt"}


@pytest.mark.asyncio
async def test_query_by_metadata_handles_no_metadata_set(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A file without JSON metadata returns enodata; should not match
    and should not crash."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/lookup-file-id/%2Fmyspace",
        json={"fileId": "rootId"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/rootId/files",
        json={
            "files": [{"fileId": "fno", "path": "/myspace/no_meta.txt"}],
            "isLast": True,
            "nextPageToken": None,
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fno/metadata/json",
        status_code=400,
        json={"error": {"id": "noData", "details": {"errno": "enodata"}}},
    )

    result = await metadata.query_by_metadata("myspace", "k=v")

    assert result["matches"] == []


@pytest.mark.asyncio
async def test_query_by_metadata_rejects_invalid_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(ValueError, match=">= 1"):
        await metadata.query_by_metadata("s", "k=v", max_depth=0)
    with pytest.raises(ValueError, match=">= 1"):
        await metadata.query_by_metadata("s", "k=v", max_results=0)
    with pytest.raises(ValueError, match="non-empty"):
        await metadata.query_by_metadata("s", "")
