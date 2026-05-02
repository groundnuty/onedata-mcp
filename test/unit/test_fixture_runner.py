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
async def test_check_convergence_returns_true_when_data_replicated(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Strategy 3a: convergence inspects DATA placement, not rule status.
    File metadata matches, distribution shows the file is fully present
    on the provider matched by the QoS expression — converged."""
    _set_env(monkeypatch)
    fixture_runner._reset_space_id_cache_for_tests()
    sc = _scenario(
        "D99",
        files=(
            FileFixture(
                path="/ppam_2026_mcp_tests/d99/x.txt",
                content="x",
                json_metadata={"k": "v"},
                qos_expressions=(("providerId=p1", 1),),
            ),
        ),
    )
    # Lookup file id
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99/x.txt"),
        json={"fileId": "x-id"},
    )
    # Metadata matches the fixture
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/metadata/json",
        json={"k": "v"},
    )
    # Distribution: provider p1 fully holds the file
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/distribution",
        json={
            "type": "REG",
            "distributionPerProvider": {
                "p1": {
                    "success": True,
                    "virtualSize": 1,
                    "distributionPerStorageBackend": {
                        "s1": {"success": True, "physicalSize": 1, "blocks": [[0, 1]]}
                    },
                }
            },
        },
    )
    # Cache the space id so _resolve_space_id_async returns immediately
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces",
        json={"spaces": ["sp1"]},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/sp1",
        json={"name": "ppam_2026_mcp_tests", "spaceId": "sp1"},
    )
    # evaluate_qos_expression matches p1's storage
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/spaces/sp1/evaluate_qos_expression",
        json={"matchingStorageBackends": [{"id": "s1", "name": "posix-local", "providerId": "p1"}]},
    )
    # qos_summary still polled for the "rules attached" sanity
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/qos/summary",
        json={"requirements": {"q1": "fulfilled"}, "status": "fulfilled"},
    )

    assert await fixture_runner._check_convergence(sc, "ppam_2026_mcp_tests") is True


@pytest.mark.asyncio
async def test_check_convergence_returns_true_even_when_rule_status_pending(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Strategy 3a (research/empirical-findings #16): a QoS rule may
    stay 'pending' indefinitely on Onedata 25.0 even when the data IS
    fully replicated. Convergence MUST treat data presence as authoritative,
    not rule status — otherwise legitimate fixtures spuriously time out."""
    _set_env(monkeypatch)
    fixture_runner._reset_space_id_cache_for_tests()
    sc = _scenario(
        "D99",
        files=(
            FileFixture(
                path="/ppam_2026_mcp_tests/d99/x.txt",
                content="x",
                qos_expressions=(("providerId=p1", 1),),
            ),
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99/x.txt"),
        json={"fileId": "x-id"},
    )
    # Data IS fully replicated on p1
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/distribution",
        json={
            "type": "REG",
            "distributionPerProvider": {
                "p1": {
                    "success": True,
                    "virtualSize": 1,
                    "distributionPerStorageBackend": {
                        "s1": {"success": True, "physicalSize": 1, "blocks": [[0, 1]]}
                    },
                }
            },
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces",
        json={"spaces": ["sp1"]},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/sp1",
        json={"name": "ppam_2026_mcp_tests", "spaceId": "sp1"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/spaces/sp1/evaluate_qos_expression",
        json={"matchingStorageBackends": [{"id": "s1", "name": "posix-local", "providerId": "p1"}]},
    )
    # The rule is STILL 'pending' even though data is fully present.
    # Pre-strategy-3a, this would have blocked convergence.
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/qos/summary",
        json={"requirements": {"q1": "pending"}, "status": "pending"},
    )

    assert await fixture_runner._check_convergence(sc, "ppam_2026_mcp_tests") is True


@pytest.mark.asyncio
async def test_check_convergence_returns_false_when_data_not_yet_replicated(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Inverse: if the QoS-matching providers don't yet hold the data
    fully (physicalSize < virtualSize), convergence must say no."""
    _set_env(monkeypatch)
    fixture_runner._reset_space_id_cache_for_tests()
    sc = _scenario(
        "D99",
        files=(
            FileFixture(
                path="/ppam_2026_mcp_tests/d99/x.txt",
                content="xxxx",
                qos_expressions=(("providerId=p1", 1),),
            ),
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99/x.txt"),
        json={"fileId": "x-id"},
    )
    # Rule IS attached but data only PARTIALLY replicated.
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/qos/summary",
        json={"requirements": {"q1": "pending"}, "status": "pending"},
    )
    # Data only PARTIALLY replicated on p1: virtualSize=4, physicalSize=2
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/x-id/distribution",
        json={
            "type": "REG",
            "distributionPerProvider": {
                "p1": {
                    "success": True,
                    "virtualSize": 4,
                    "distributionPerStorageBackend": {
                        "s1": {"success": True, "physicalSize": 2, "blocks": [[0, 2]]}
                    },
                }
            },
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces",
        json={"spaces": ["sp1"]},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/sp1",
        json={"name": "ppam_2026_mcp_tests", "spaceId": "sp1"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/spaces/sp1/evaluate_qos_expression",
        json={"matchingStorageBackends": [{"id": "s1", "name": "posix-local", "providerId": "p1"}]},
    )

    assert await fixture_runner._check_convergence(sc, "ppam_2026_mcp_tests") is False


@pytest.mark.asyncio
async def test_check_convergence_returns_true_when_all_rules_terminal_impossible(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Strategy 3a special case: a fixture file with intentionally
    unfulfillable rules (e.g. P2 violator.bin) is terminal-converged
    even though no data placement satisfies the rules. All rules must
    be in 'impossible' status."""
    _set_env(monkeypatch)
    fixture_runner._reset_space_id_cache_for_tests()
    sc = _scenario(
        "D99",
        files=(
            FileFixture(
                path="/ppam_2026_mcp_tests/d99/violator.bin",
                content="x",
                qos_expressions=(("providerId=fakedoesnotexist000000000000000000ch0000", 1),),
            ),
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_lookup("/ppam_2026_mcp_tests/d99/violator.bin"),
        json={"fileId": "v-id"},
    )
    # All rules are terminally 'impossible' — accept as converged.
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/v-id/qos/summary",
        json={"requirements": {"q1": "impossible"}, "status": "impossible"},
    )

    assert await fixture_runner._check_convergence(sc, "ppam_2026_mcp_tests") is True


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
    assert await fixture_runner._check_convergence(sc, "ppam_2026_mcp_tests") is False


@pytest.mark.asyncio
async def test_check_convergence_returns_true_when_no_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenarios without fixture files (D1, D4, D6) trivially converge."""
    _set_env(monkeypatch)
    sc = _scenario("D6")
    assert await fixture_runner._check_convergence(sc, "ppam_2026_mcp_tests") is True
