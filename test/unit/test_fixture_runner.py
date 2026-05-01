"""Mocked-HTTP unit tests for the fixture runner.

Covers wipe, materialise, and convergence phases. Pre-stage (P4 only) is
not unit-tested here — its behaviour depends on real federation timing
that's hard to mock cleanly; it gets exercised in the live --write smoke.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from pytest_httpx import HTTPXMock

from benchmark import fixture_runner
from benchmark._scenario_types import FileFixture, Fixture, Scenario


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token-z")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    fixture_runner._reset_space_id_cache_for_tests()


def _lookup(path: str) -> str:
    return f"https://provider.example/api/v3/oneprovider/lookup-file-id/{quote(path, safe='')}"


def _scenario(scenario_id: str, files: tuple[FileFixture, ...] = ()) -> Scenario:
    return Scenario(
        id=scenario_id,
        band="discovery",
        brief="brief",
        oracle_tier="format",
        required_tools=frozenset(),
        allowed_tools_minimal=frozenset(),
        fixture=Fixture(files=files),
        oracle_check="check",
    )


# ---------------------------------------------------------------------------
# Phase 1 — Wipe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wipe_subtree_handles_missing_path_silently(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99"),
        status_code=400,
        json={"error": {"details": {"errno": "enoent"}}},
    )

    # Should not raise.
    await fixture_runner._wipe_subtree("/ppam_2026_mcp_tests/d99")


@pytest.mark.asyncio
async def test_wipe_subtree_calls_delete_for_existing_path(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99"),
        json={"fileId": "subtree-id"},
    )
    httpx_mock.add_response(
        method="DELETE",
        url="https://provider.example/api/v3/oneprovider/data/subtree-id",
        status_code=204,
    )

    await fixture_runner._wipe_subtree("/ppam_2026_mcp_tests/d99")


# ---------------------------------------------------------------------------
# Phase 2 — Materialise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialise_files_creates_with_metadata_and_qos(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    f = FileFixture(
        path="/ppam_2026_mcp_tests/d99/x.txt",
        content="hello",
        json_metadata={"k": "v"},
        qos_expressions=(("country=PL", 1),),
    )
    # PUT for create_file with create_parents=True
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests"),
        json={"fileId": "space-root-id"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=(
            "https://provider.example/api/v3/oneprovider/data/space-root-id"
            "/path/d99/x.txt?create_parents=true"
        ),
        json={"fileId": "x-id"},
        status_code=201,
    )
    # set_file_metadata
    httpx_mock.add_response(
        method="PUT",
        url="https://provider.example/api/v3/oneprovider/data/x-id/metadata/json",
        status_code=204,
    )
    # add_qos_requirement
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/qos_requirements",
        json={"qosRequirementId": "qos-1"},
        status_code=201,
    )

    paths = await fixture_runner._materialise_files((f,))

    assert paths == {"/ppam_2026_mcp_tests/d99/x.txt": "x-id"}

    # Confirm metadata body shape
    md_request = next(r for r in httpx_mock.get_requests() if r.url.path.endswith("/metadata/json"))
    assert json.loads(md_request.content) == {"k": "v"}

    # Confirm QoS body shape (top-level POST, fileId in body)
    qos_request = next(
        r for r in httpx_mock.get_requests() if r.url.path.endswith("/qos_requirements")
    )
    body = json.loads(qos_request.content)
    assert body == {"expression": "country=PL", "replicasNum": 1, "fileId": "x-id"}


# ---------------------------------------------------------------------------
# Phase 4 — Convergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_convergence_returns_true_when_all_files_present_and_qos_settled(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    sc = _scenario(
        "D99",
        files=(
            FileFixture(
                path="/ppam_2026_mcp_tests/d99/x.txt",
                content="x",
                json_metadata={"k": "v"},
                qos_expressions=(("country=PL", 1),),
            ),
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99/x.txt"),
        json={"fileId": "x-id"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/metadata/json",
        json={"k": "v"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/qos/summary",
        json={"requirements": {"q1": "fulfilled"}, "status": "fulfilled"},
    )

    assert await fixture_runner._check_convergence(sc) is True


@pytest.mark.asyncio
async def test_check_convergence_returns_false_when_qos_pending(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    sc = _scenario(
        "D99",
        files=(
            FileFixture(
                path="/ppam_2026_mcp_tests/d99/x.txt",
                content="x",
                qos_expressions=(("country=PL", 1),),
            ),
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99/x.txt"),
        json={"fileId": "x-id"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/qos/summary",
        json={"requirements": {"q1": "pending"}, "status": "pending"},
    )

    assert await fixture_runner._check_convergence(sc) is False


@pytest.mark.asyncio
async def test_check_convergence_returns_false_on_missing_file(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    sc = _scenario(
        "D99",
        files=(FileFixture(path="/ppam_2026_mcp_tests/d99/missing.txt", content=""),),
    )
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99/missing.txt"),
        status_code=400,
        json={"error": {"details": {"errno": "enoent"}}},
    )
    assert await fixture_runner._check_convergence(sc) is False


@pytest.mark.asyncio
async def test_check_convergence_returns_true_when_no_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenarios without fixture files (D1, D4, D6) trivially converge."""
    _set_env(monkeypatch)
    sc = _scenario("D6")
    assert await fixture_runner._check_convergence(sc) is True
