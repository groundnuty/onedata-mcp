"""Unit tests for the QoS API client.

Mocked HTTP responses against shapes pinned to Onedata 25.0 swagger
(oneprovider-swagger tag 25.0; commit 39da981).
"""

import json

import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import qos
from onedata_mcp.utils import OnedataApiError


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token-z")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://oneprovider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


@pytest.mark.asyncio
async def test_get_file_qos_summary_returns_requirements_and_status(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    file_id = "094576776E66743172323067776777"
    httpx_mock.add_response(
        method="GET",
        url=f"https://oneprovider.example/api/v3/oneprovider/data/{file_id}/qos/summary",
        json={
            "requirements": {"c84f669f9522c46976fee490d80651f0": "fulfilled"},
            "status": "fulfilled",
        },
    )

    result = await qos.get_file_qos_summary(file_id)

    assert result["status"] == "fulfilled"
    assert "c84f669f9522c46976fee490d80651f0" in result["requirements"]


@pytest.mark.asyncio
async def test_get_file_qos_summary_resolves_path_via_lookup_first(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    file_id = "abcdefghijklmnopqrstuvwxyz0123456789"
    httpx_mock.add_response(
        method="POST",
        url="https://oneprovider.example/api/v3/oneprovider/lookup-file-id/%2Fmyspace%2Ffile.txt",
        json={"fileId": file_id},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"https://oneprovider.example/api/v3/oneprovider/data/{file_id}/qos/summary",
        json={"requirements": {}, "status": "impossible"},
    )

    result = await qos.get_file_qos_summary("/myspace/file.txt")

    assert result["status"] == "impossible"


@pytest.mark.asyncio
async def test_add_qos_requirement_posts_expression_and_replicas_and_fileid(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    file_id = "094576776E66743172323067776777"
    httpx_mock.add_response(
        method="POST",
        url="https://oneprovider.example/api/v3/oneprovider/qos_requirements",
        json={"qosRequirementId": "c84f669f9522c46976fee490d80651f0"},
        status_code=201,
    )

    result = await qos.add_qos_requirement(file_id, "country=PL", replicas_num=2)

    assert result["qosRequirementId"] == "c84f669f9522c46976fee490d80651f0"

    requests = httpx_mock.get_requests()
    posted = json.loads(requests[0].content)
    assert posted == {
        "expression": "country=PL",
        "replicasNum": 2,
        "fileId": file_id,
    }


@pytest.mark.asyncio
async def test_add_qos_requirement_rejects_invalid_replicas_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)

    with pytest.raises(ValueError, match=">= 1"):
        await qos.add_qos_requirement("file_id_xyz", "country=PL", replicas_num=0)


@pytest.mark.asyncio
async def test_add_qos_requirement_surfaces_api_error_for_invalid_expression(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Server-side rejection of e.g. 'geo==PL' — Python-trained-LLM == typo.

    Paper §5.4 H_qos_syntax metric depends on the agent reading this error
    and self-correcting. The OnedataApiError exposes errno/error_id/body so
    the MCP layer can surface a structured message back to the agent.
    """
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://oneprovider.example/api/v3/oneprovider/qos_requirements",
        status_code=400,
        json={
            "error": {
                "id": "badQosExpression",
                "details": {"errno": "einval"},
                "description": "Invalid QoS expression near '==': use '=' instead.",
            }
        },
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.add_qos_requirement("fileXYZABCDEFGHIJKL", "geo==PL")

    err = excinfo.value
    assert err.error_id == "badQosExpression"
    assert err.errno == "einval"


@pytest.mark.asyncio
async def test_get_qos_requirement_returns_full_record(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(
        method="GET",
        url=f"https://oneprovider.example/api/v3/oneprovider/qos_requirements/{qid}",
        json={
            "qosRequirementId": qid,
            "fileId": "fileXYZ",
            "qosExpression": "country=FR",
            "replicasNum": 2,
            "status": "fulfilled",
        },
    )

    result = await qos.get_qos_requirement(qid)

    assert result["qosExpression"] == "country=FR"
    assert result["status"] == "fulfilled"


@pytest.mark.asyncio
async def test_remove_qos_requirement_returns_none_on_204(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(
        method="DELETE",
        url=f"https://oneprovider.example/api/v3/oneprovider/qos_requirements/{qid}",
        status_code=204,
    )

    result = await qos.remove_qos_requirement(qid)

    assert result is None
