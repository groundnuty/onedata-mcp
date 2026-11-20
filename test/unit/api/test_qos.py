"""Unit tests for the QoS API client.

Mocked HTTP responses against shapes pinned to Onedata 25.0 swagger
(oneprovider-swagger tag 25.0; commit 39da981).
"""

import http.server
import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import qos
from onedata_mcp.utils import MAX_ERROR_CHARS, OnedataApiError

QOS_URL = "https://oneprovider.example/api/v3/oneprovider/qos_requirements"
# What the refusals and hints say about where an id is. They name no tool.
WHERE = "QoS requirement ids are the keys of `requirements` in its QoS summary"


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


# Ids that cannot name a requirement, with a word of the refusal each gets. Shared by the get and
# the remove tests: both tools go through the same guard.
REFUSED_IDS = [
    ("", "is empty"),
    ("   ", "is empty"),
    ("/k2val-x/logs/log_20200107.dat", "is a path"),
    ("logs/log_20200107.dat", "is a path"),
    # dot segments: the HTTP client would resolve them to another URL (`..` to the API root)
    ("..", "is not a QoS requirement id"),
    (".", "is not a QoS requirement id"),
    (" .. ", "is not a QoS requirement id"),
    ("c84f 669f", "has whitespace inside"),
    ("c84f\t669f", "has whitespace inside"),
    ("c84f\n669f", "has whitespace inside"),
    ("c84f\x00669f", "has a control character"),
    ("c84f\x1b669f", "has a control character"),
    ("providerId=p123", "looks like a QoS expression"),
    ("anyStorage & type=ssd", "has whitespace inside"),
    ("geo=EU\\providerId=p123", "looks like a QoS expression"),
    ("c84f\\669f", "has a backslash"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("bad", "needle"), REFUSED_IDS)
async def test_get_qos_requirement_refuses_ids_that_cannot_name_a_requirement(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, bad: str, needle: str
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        await qos.get_qos_requirement(bad)

    text = str(excinfo.value)
    assert needle in text and WHERE in text
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_get_qos_requirement_strips_surrounding_whitespace_and_uses_the_stripped_id(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(
        method="GET", url=f"{QOS_URL}/{qid}", json={"qosRequirementId": qid, "status": "pending"}
    )

    result = await qos.get_qos_requirement(f" \n{qid}\t ")

    assert result["qosRequirementId"] == qid
    (req,) = httpx_mock.get_requests()
    assert req.url.raw_path.decode() == f"/api/v3/oneprovider/qos_requirements/{qid}"


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
# with the generic Error body; which error ids and statuses 25.0 really uses is not pinned there.
# A specific hint therefore needs a structured Onedata error (`error.id` / `error.details.errno`)
# on a 4xx status, and anything less gets a generic hint that keeps the uncertainty. The bodies
# below are plausible shapes, not recorded ones.
# ---------------------------------------------------------------------------

NOT_FOUND = {"error": {"id": "notFound", "description": "The resource could not be found."}}
ENOENT = {"error": {"id": "posix", "details": {"errno": "enoent"}, "description": "enoent"}}
SPECIFIC = "No QoS requirement with id"
GENERIC_404 = "Onedata answered 404 without saying the requirement is missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("qid", "sent"),
    [
        # unencoded, `?` and `#` would cut the path short and remove requirement "abc"
        ("abc?x", "abc%3Fx"),
        ("abc#frag", "abc%23frag"),
        ("a%20b", "a%2520b"),  # a literal `%` is itself encoded
        ("c84f%2e%2e", "c84f%252e%252e"),
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
async def test_remove_qos_requirement_strips_surrounding_whitespace_and_uses_the_stripped_id(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A trailing space would be sent as `%20` and name another (unknown) id."""
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=204)

    assert await qos.remove_qos_requirement(f"  {qid}\n") is None

    (req,) = httpx_mock.get_requests()
    assert req.url.raw_path.decode() == f"/api/v3/oneprovider/qos_requirements/{qid}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("qid", "needle"), REFUSED_IDS)
async def test_remove_qos_requirement_refuses_a_wrong_kind_of_id_before_any_request(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, qid: str, needle: str
) -> None:
    _set_env(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert needle in text
    assert WHERE in text  # says where a valid id comes from
    assert "get_file_qos_summary" not in text  # and names no tool, which may not be offered
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 400])
@pytest.mark.parametrize("body", [NOT_FOUND, ENOENT], ids=["notFound", "enoent"])
async def test_remove_qos_requirement_unknown_id_says_where_the_ids_are(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict
) -> None:
    """A structured not-found on a 4xx status: the one case that says the id may be gone."""
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=status, json=body)

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    err = excinfo.value
    text = str(err)
    assert text.startswith(f"No QoS requirement with id '{qid}' exists")
    assert "removed already" in text
    assert WHERE in text
    # Onedata's own message follows, and the response stays on the error
    assert f"(status={status})" in text and body["error"]["description"] in text
    assert err.response["status_code"] == status
    assert err.error_id == body["error"]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "headers", "text"),
    [
        # a gateway's HTML page, or a bare 404: nothing says the requirement is missing
        (404, None, {"Content-Type": "text/html"}, "<html>not found</html>"),
        (404, None, {}, ""),
        # structured, but not a not-found
        (404, {"error": {"id": "spaceNotSupportedBy", "description": "elsewhere"}}, None, None),
        (404, {}, None, None),
    ],
)
async def test_remove_qos_requirement_a_404_that_does_not_say_missing_keeps_the_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    status: int,
    body: dict | None,
    headers: dict | None,
    text: str | None,
) -> None:
    _set_env(monkeypatch)
    qid = "deadbeef"
    kwargs = {"json": body} if body is not None else {"text": text, "headers": headers}
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=status, **kwargs)

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    shown = str(excinfo.value)
    assert shown.startswith(GENERIC_404 + "; check the id and the provider.")
    assert WHERE in shown
    assert SPECIFIC not in shown and "removed already" not in shown
    assert "<html>" not in shown  # a non-JSON page is never shown (it may name a node)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error": {"id": "badValueIdentifier", "description": "bad id"}},
        {"error": {"id": "posix", "details": {"errno": "ebusy"}, "description": "busy"}},
        {},
    ],
)
async def test_remove_qos_requirement_an_unrecognised_400_does_not_blame_the_id(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, body: dict
) -> None:
    """Onedata also answers 400 for POSIX and domain errors: check the id, but do not say it is
    wrong, and do not say it is gone."""
    _set_env(monkeypatch)
    file_id = "000000000052A1E3677569642331"  # a model that passed the file's id
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{file_id}", status_code=400, json=body)

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(file_id)

    text = str(excinfo.value)
    assert text.startswith("Onedata answered 400 without saying the requirement is missing")
    assert "a file id is not one" in text and WHERE in text
    assert "rejected" not in text and SPECIFIC not in text and "removed already" not in text


@pytest.mark.asyncio
async def test_remove_qos_requirement_given_a_file_id_that_is_not_found_says_a_file_id_is_not_one(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    file_id = "000000000052A1E3677569642331"
    httpx_mock.add_response(
        method="DELETE", url=f"{QOS_URL}/{file_id}", status_code=404, json=NOT_FOUND
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(file_id)

    text = str(excinfo.value)
    assert SPECIFIC in text and "a file id is not one" in text and WHERE in text


@pytest.mark.asyncio
async def test_remove_qos_requirement_a_5xx_mentioning_a_missing_item_is_not_a_removal(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=500, json=ENOENT)

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert text.startswith("Onedata answered 500 and mentioned a missing item")
    assert "the removal is not confirmed" in text
    assert SPECIFIC not in text and "removed already" not in text
    assert excinfo.value.response["status_code"] == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"error": {"id": "forbidden", "description": "You are not authorized."}}),
        (400, {"error": {"id": "posix", "details": {"errno": "eacces"}, "description": "eacces"}}),
        (403, {"error": {"id": "posix", "details": {"errno": "eperm"}, "description": "eperm"}}),
        (403, None),
    ],
)
async def test_remove_qos_requirement_permission_error_says_a_retry_will_not_help(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict | None
) -> None:
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    kwargs = {"json": body} if body is not None else {"text": "denied"}
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=status, **kwargs)

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert text.startswith(f"Not allowed to remove QoS requirement '{qid}'")
    assert "space_manage_qos" in text
    assert "will not help" in text
    # scoped to this requirement's space: it does not say every removal will fail
    assert "this requirement belongs to" in text and "is not affected" in text
    assert excinfo.value.response["status_code"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 401])
async def test_remove_qos_requirement_other_errors_are_passed_on_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int
) -> None:
    """Nothing the caller can fix: no hint, Onedata's message as it is."""
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(
        method="DELETE",
        url=f"{QOS_URL}/{qid}",
        status_code=status,
        json={"error": {"id": "internalServerError", "description": "boom"}},
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert text.startswith("Onedata API request failed: DELETE /qos_requirements/")
    assert WHERE not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_remove_qos_requirement_a_redirect_is_not_a_removal(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int
) -> None:
    """httpx follows no redirect and does not count a 3xx as an error: it would read as success
    while the requirement is still there."""
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(
        method="DELETE",
        url=f"{QOS_URL}/{qid}",
        status_code=status,
        headers={"Location": "https://elsewhere.example/api/v3/oneprovider/x"},
        text="<html>moved to https://elsewhere.example/x</html>",
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    err = excinfo.value
    text = str(err)
    assert text.startswith(f"Onedata answered {status}, not 204")
    assert "removal is not confirmed" in text and "may still exist" in text
    assert WHERE in text
    assert "elsewhere" not in text and "<html>" not in text and "https://" not in text
    assert err.response == {"status_code": status, "body": None}
    assert len(httpx_mock.get_requests()) == 1  # not followed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 202, 204])
async def test_remove_qos_requirement_any_2xx_is_a_success(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int
) -> None:
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=status)

    assert await qos.remove_qos_requirement(qid) is None


HOSTS = ("oneprovider.example", "onezone.example")
PEER_DOMAIN = "dev-oneprovider-krakow.default.svc.cluster.local"
LONG_ID = "a" * 300


def _hostname_bearing(status_error_id: str, errno: str | None = None) -> dict:
    error: dict = {
        "id": status_error_id,
        "description": (
            "Failed at oneprovider.example:443 (zone onezone.example, peer peer-krakow.plgrid.pl), "
            "see https://oneprovider.example/api/v3/oneprovider/qos_requirements/x and "
            "http://other.example/y"
        ),
        # the domain of the service that answered: a peer provider, which is not configured
        "ctx": {"serviceDomain": PEER_DOMAIN, "line": 45},
    }
    error["details"] = {"errno": errno, "domain": PEER_DOMAIN} if errno else {"host": PEER_DOMAIN}
    return {"error": error}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, _hostname_bearing("notFound")),
        (404, _hostname_bearing("somethingElse")),
        (400, _hostname_bearing("posix", "enoent")),
        (400, _hostname_bearing("posix", "ebusy")),
        (403, _hostname_bearing("forbidden")),
        (500, _hostname_bearing("posix", "enoent")),
        (500, _hostname_bearing("internalServerError")),
    ],
)
async def test_remove_qos_requirement_the_complete_message_holds_no_host_url_or_long_id(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict
) -> None:
    """With no node list: every kind of hint, with a body that names both hosts, URLs and a peer
    provider's domain, and an id of 300 characters. The whole message is host-free and bounded,
    and the id is not repeated in full in the appended request path."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="DELETE", url=f"{QOS_URL}/{LONG_ID}", status_code=status, json=body
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(LONG_ID)

    text = str(excinfo.value)
    for host in HOSTS:
        assert host not in text
    assert "other.example" not in text and "://" not in text
    # a peer's domain, as a field value and as a bare name in the description
    assert PEER_DOMAIN not in text and "cluster.local" not in text
    assert "peer-krakow" not in text and "plgrid.pl" not in text
    assert len(text) <= MAX_ERROR_CHARS
    assert LONG_ID not in text and "a" * 129 not in text
    assert text.count("a" * 80) <= 2  # the hint's echo and the path's: each cut short


@pytest.mark.asyncio
async def test_remove_qos_requirement_a_huge_body_is_bounded_and_ends_in_an_ellipsis(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    qid = "c84f669f9522c46976fee490d80651f0"
    body = {"error": {"id": "notFound", "description": "n" * 10_000}}
    httpx_mock.add_response(method="DELETE", url=f"{QOS_URL}/{qid}", status_code=404, json=body)

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(qid)

    text = str(excinfo.value)
    assert text.startswith(f"No QoS requirement with id '{qid}' exists")
    assert len(text) == MAX_ERROR_CHARS and text.endswith("...")


@pytest.mark.asyncio
async def test_remove_qos_requirement_echoes_a_long_id_cut_short(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="DELETE", url=f"{QOS_URL}/{LONG_ID}", status_code=404, json=NOT_FOUND
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await qos.remove_qos_requirement(LONG_ID)

    text = str(excinfo.value)
    hint = text.split(" [", 1)[0]
    assert "a" * 81 not in hint and "..." in hint
    assert LONG_ID not in text and "a" * 129 not in text  # the whole message, path included


# ---------------------------------------------------------------------------
# The live test's own guards (test/live/test_qos_live.py): it cannot run without a federation,
# so what keeps it local and what its cleanup may remove are checked here.
# ---------------------------------------------------------------------------

LIVE_TEST = Path(__file__).resolve().parents[2] / "live" / "test_qos_live.py"


def _live():
    spec = importlib.util.spec_from_file_location("qos_live_under_test", LIVE_TEST)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass in it looks its module up here
    spec.loader.exec_module(module)
    return module


def _resolver(*addresses: str):
    def resolve(host, port, *args, **kwargs):
        return [
            (
                socket.AF_INET6 if ":" in a else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (a, port, 0, 0) if ":" in a else (a, port),
            )
            for a in addresses
        ]

    return resolve


def test_the_live_guard_accepts_a_name_that_resolves_only_to_loopback() -> None:
    live = _live()
    assert live.resolve_loopback_only("a.datahub.test", 443, _resolver("127.0.0.1")) == (
        "127.0.0.1",
    )
    both = live.resolve_loopback_only("localhost", 443, _resolver("::1", "127.0.0.1", "127.0.0.2"))
    assert both == ("127.0.0.1", "127.0.0.2", "::1")


@pytest.mark.parametrize(
    "addresses",
    [("10.0.0.5",), ("127.0.0.1", "192.0.2.7"), ("::1", "2001:db8::1"), ()],
    ids=["private", "mixed", "mixed-v6", "none"],
)
def test_the_live_guard_refuses_a_name_with_any_non_loopback_address(addresses) -> None:
    live = _live()
    with pytest.raises(live.NotLoopback):
        live.resolve_loopback_only("a.datahub.test", 443, _resolver(*addresses))


def test_the_live_guard_refuses_a_name_that_does_not_resolve() -> None:
    live = _live()

    def broken(host, port, *args, **kwargs):
        raise socket.gaierror("no such host")

    with pytest.raises(live.NotLoopback):
        live.resolve_loopback_only("a.datahub.test", 443, broken)


def test_the_live_run_refuses_a_datahub_test_name_mapped_to_another_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _live()
    before = socket.getaddrinfo
    with pytest.raises(pytest.fail.Exception, match="not loopback-only"):
        live.enforce_local_federation(
            monkeypatch,
            {"ONEDATA_ONEPROVIDER_HOST": "https://op.datahub.test:8443"},
            resolve=_resolver("10.1.2.3"),
        )
    assert socket.getaddrinfo is before  # nothing was pinned


@pytest.mark.parametrize("url", ["https://example.org", "", "https://127.0.0.1.example.org"])
def test_the_live_run_refuses_a_host_that_is_not_local_by_name(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    live = _live()
    with pytest.raises(pytest.fail.Exception, match="refusing to run"):
        live.enforce_local_federation(
            monkeypatch, {"ONEDATA_ONEPROVIDER_HOST": url}, resolve=_resolver("127.0.0.1")
        )


@pytest.mark.asyncio
async def test_the_live_run_pins_the_connection_to_the_checked_address_and_ignores_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name resolves nowhere on its own: the request reaches the local server only because
    the checked address is what the name resolves to."""
    live = _live()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://203.0.113.9:3128")
        live.enforce_local_federation(
            monkeypatch,
            {"ONEDATA_ONEPROVIDER_HOST": f"http://pinned.datahub.test:{port}"},
            resolve=_resolver("127.0.0.1"),
        )

        async with httpx.AsyncClient() as client:  # trusts the environment, as the tools' does
            response = await client.get(f"http://pinned.datahub.test:{port}/")

        assert response.status_code == 200 and response.text == "ok"
        assert {info[4][0] for info in socket.getaddrinfo("pinned.datahub.test", port)} == {
            "127.0.0.1"
        }
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_the_live_cleanup_removes_only_the_ids_the_test_created_and_survives_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A summary also lists ids inherited from the space root: those are never removed. A
    failing step does not stop the next one."""
    live = _live()
    removed: list[str] = []
    deleted: list[str] = []

    async def fake_remove(qos_id: str) -> None:
        removed.append(qos_id)
        if qos_id == "mine-1":
            raise RuntimeError("already gone")

    async def fake_delete(path: str) -> None:
        deleted.append(path)
        raise RuntimeError("cannot delete")

    monkeypatch.setattr(live, "remove_qos_requirement", fake_remove)
    monkeypatch.setattr(live, "delete_file", fake_delete)
    probe = live.Probe(path="/space/probe.txt", file_id="f1", created=["mine-1", "mine-2"])

    await live.cleanup_probe(probe)  # raises nothing

    assert removed == ["mine-2", "mine-1"]  # every tracked id, nothing else
    assert deleted == ["/space/probe.txt"]
