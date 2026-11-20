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
async def test_get_qos_requirement_percent_encodes_the_id(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://oneprovider.example/api/v3/oneprovider/qos_requirements/abc%3Fx%23y",
        json={"qosRequirementId": "abc?x#y"},
    )

    result = await qos.get_qos_requirement("abc?x#y")

    assert result["qosRequirementId"] == "abc?x#y"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "  ", "/space/file.dat", ".", ".."])
async def test_get_qos_requirement_refuses_ids_that_cannot_name_a_requirement(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(ValueError):
        await qos.get_qos_requirement(bad)


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


# ---------------------------------------------------------------------------
# remove_qos_requirement: the error paths a model can trigger.
#
# The swagger (paths/qos_requirements/qid.yaml, DELETE) lists 204, 400, 403, 404 and 500, each
# with the generic Error body; the exact 25.0 error ids are not pinned there, so the hints are
# keyed on the status. The bodies below are plausible shapes, not recorded ones.
# ---------------------------------------------------------------------------

QOS_URL = "https://oneprovider.example/api/v3/oneprovider/qos_requirements"
WHERE = "get_file_qos_summary"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("qid", "sent"),
    [
        # unencoded, `?` and `#` would cut the path short and remove requirement "abc"
        ("abc?x=1", "abc%3Fx%3D1"),
        ("abc#frag", "abc%23frag"),
        ("a b", "a%20b"),
    ],
)
async def test_remove_qos_requirement_percent_encodes_the_id(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, qid: str, sent: str
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{sent}", status_code=204)

    assert await qos.remove_qos_requirement(qid) is None

    (req,) = httpx_mock.get_requests()
    assert req.url.raw_path.decode() == f"/api/v3/oneprovider/qos_requirements/{sent}"
    assert not req.url.query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("qid", "needle"),
    [
        ("/k2val-x/logs/log_20200107.dat", "is a path"),
        ("logs/log_20200107.dat", "is a path"),
        ("", "is empty"),
        ("   ", "is empty"),
        # dot segments: the HTTP client would resolve them to another URL (`..` to the API root)
        ("..", "is not a QoS requirement id"),
        (".", "is not a QoS requirement id"),
    ],
)
async def test_remove_qos_requirement_refuses_a_wrong_kind_of_id_before_any_request(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, qid: str, needle: str
) -> None:
    _set_env(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert needle in text
    assert WHERE in text  # says where a valid id comes from
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error": {"id": "notFound", "description": "The resource could not be found."}},
        {"error": {"id": "posix", "details": {"errno": "enoent"}, "description": "enoent"}},
    ],
)
async def test_remove_qos_requirement_unknown_id_says_where_the_ids_are(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, body: dict
) -> None:
    """An id that names no requirement (removed already, or never one): 404."""
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=404, json=body)

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    err = excinfo.value
    text = str(err)
    assert text.startswith(f"No QoS requirement with id '{qid}' exists")
    assert "removed already" in text
    assert WHERE in text
    # Onedata's own message follows, and the response stays on the error
    assert "(status=404)" in text and body["error"]["description"] in text
    assert err.response["status_code"] == 404
    assert err.error_id == body["error"]["id"]


@pytest.mark.asyncio
async def test_remove_qos_requirement_unknown_id_with_a_non_json_404_keeps_the_hint(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    qid = "deadbeef"
    httpx_mock.add_response(
        method="DELETE",
        url=f"{QOS_URL}/{qid}",
        status_code=404,
        text="<html>not found</html>",
        headers={"Content-Type": "text/html"},
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert "No QoS requirement with id 'deadbeef' exists" in text
    assert WHERE in text
    assert "<html>" not in text  # a non-JSON page is never shown (it may name a node)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "needle"),
    [
        (404, {"error": {"id": "notFound", "description": "not found"}}, "No QoS requirement"),
        (400, {"error": {"id": "badValueIdentifier", "description": "bad id"}}, "rejected"),
    ],
)
async def test_remove_qos_requirement_given_a_file_id_points_to_the_requirement_ids(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict, needle: str
) -> None:
    """A model that passes the file's id where the requirement's belongs."""
    _set_env(monkeypatch)
    file_id = "000000000052A1E3677569642331"
    httpx_mock.add_response(
        method="DELETE", url=f"{QOS_URL}/{file_id}", status_code=status, json=body
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(file_id)

    text = str(excinfo.value)
    assert needle in text
    assert WHERE in text
    if status == 404:
        assert "a file id is not one" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"error": {"id": "forbidden", "description": "You are not authorized."}}),
        (400, {"error": {"id": "posix", "details": {"errno": "eacces"}, "description": "eacces"}}),
        (403, {"error": {"id": "posix", "details": {"errno": "eperm"}, "description": "eperm"}}),
    ],
)
async def test_remove_qos_requirement_permission_error_says_a_retry_will_not_help(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict
) -> None:
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=status, json=body)

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert text.startswith(f"Not allowed to remove QoS requirement '{qid}'")
    assert "space_manage_qos" in text
    assert "will not help" in text
    assert excinfo.value.response["status_code"] == status


@pytest.mark.asyncio
async def test_remove_qos_requirement_server_error_is_passed_on_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Nothing the caller can fix: no hint, Onedata's message as it is."""
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(
        method="DELETE",
        url=f"{QOS_URL}/{qid}",
        status_code=500,
        json={"error": {"id": "internalServerError", "description": "boom"}},
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert text.startswith("Onedata API request failed: DELETE /qos_requirements/")
    assert WHERE not in text


@pytest.mark.asyncio
async def test_remove_qos_requirement_echoes_a_long_id_cut_short(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    qid = "a" * 300
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=404, json={})

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    hint = str(excinfo.value).split(" [", 1)[0]
    assert "a" * 81 not in hint and "..." in hint
