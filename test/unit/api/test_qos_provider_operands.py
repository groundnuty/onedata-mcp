"""A `providerId=<value>` operand of a new QoS rule is checked against the space's providers
(SF1, design/16).

Onedata takes `providerId=local`, a provider's NAME, and creates a rule that no storage matches:
it stays "impossible" for good. `add_qos_requirement(check_providers=True)`, which the MCP tool
turns on, refuses such a value before the rule is created, and says which id to use. The ids and
names are synthetic.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import qos

BASE = "https://oneprovider.example/api/v3/oneprovider"
SPACE_ID = "9JO5__9nL1b01YEvwnft1r20gwgwlkIp19ND_n8hgxs"
_PAYLOAD = f"guid#t_w-ghd1c7eDoMvTmo-gW_3aszvpHkGzy6Xw4#{SPACE_ID}".encode()
# a file id of the 25.0 shape: an 8-byte header whose sixth byte is the payload's length, then
# the payload, in hex
FILE_ID = (bytes([0, 0, 0, 0, 0, len(_PAYLOAD), 0x5C, 0xF4]) + _PAYLOAD).hex().upper()
LOCAL_ID, OWNER_ID = "pLocal0123456789abcdef", "pOwner0123456789abcdef"
PROVIDERS = [
    {"providerId": LOCAL_ID, "providerName": "provider-a"},
    {"providerId": OWNER_ID, "providerName": "provider-b"},
]


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token-z")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://oneprovider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


def _space_lists(httpx_mock: HTTPXMock, providers: list | None = None, **kw: object) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/spaces/{SPACE_ID}",
        json={"spaceId": SPACE_ID, "name": "space-x", "providers": providers or PROVIDERS},
        **kw,
    )


def _rule_is_created(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/qos_requirements",
        json={"qosRequirementId": "q1"},
        status_code=201,
    )


def _posts(httpx_mock: HTTPXMock) -> list[dict]:
    return [
        json.loads(r.content)
        for r in httpx_mock.get_requests()
        if r.url.path.endswith("/qos_requirements")
    ]


async def _add(expression: str, file_id: str = FILE_ID, **kw: object) -> dict:
    return await qos.add_qos_requirement(file_id, expression, check_providers=True, **kw)


@pytest.mark.asyncio
async def test_a_providers_name_is_refused_with_its_id_and_no_rule_is_created(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _space_lists(httpx_mock)

    with pytest.raises(ValueError) as excinfo:
        await _add("providerId=provider-a")

    text = str(excinfo.value)
    assert "No QoS requirement was added" in text
    assert "'provider-a' is a provider's name; providerId= takes its id: providerId=<id>" in text
    assert f"here providerId={LOCAL_ID}" in text
    assert f"provider-b (id '{OWNER_ID}')" in text  # the providers are listed
    assert _posts(httpx_mock) == []  # refused before the rule was created
    assert "oneprovider.example" not in text and "get_file" not in text  # no host, no tool


@pytest.mark.asyncio
async def test_a_value_that_is_neither_a_name_nor_an_id_is_refused_with_the_providers_listed(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _space_lists(httpx_mock)

    with pytest.raises(ValueError) as excinfo:
        await _add("providerId=provider-z")

    text = str(excinfo.value)
    assert "'provider-z' is neither the id nor the name of a provider that supports" in text
    assert f"provider-a (id '{LOCAL_ID}')" in text and f"provider-b (id '{OWNER_ID}')" in text
    assert "is a provider's name" not in text
    assert _posts(httpx_mock) == []


@pytest.mark.asyncio
async def test_a_valid_id_passes_and_the_expression_is_sent_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _space_lists(httpx_mock)
    _rule_is_created(httpx_mock)

    result = await _add(f"providerId={LOCAL_ID}", replicas_num=2)

    assert result == {"qosRequirementId": "q1"}
    assert _posts(httpx_mock) == [
        {"expression": f"providerId={LOCAL_ID}", "replicasNum": 2, "fileId": FILE_ID}
    ]


@pytest.mark.asyncio
async def test_a_compound_expression_is_checked_per_operand_and_others_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _space_lists(httpx_mock, is_reusable=True)
    _rule_is_created(httpx_mock)

    # all ids, beside an attribute operand and an exclusion: unchanged, one listing, one POST
    expression = f"(providerId={LOCAL_ID}|providerId={OWNER_ID})&country=PL\\providerId={OWNER_ID}"
    await _add(expression)
    assert _posts(httpx_mock) == [{"expression": expression, "replicasNum": 1, "fileId": FILE_ID}]

    # one id and one name: only the name is a problem, and it is the one reported
    with pytest.raises(ValueError) as excinfo:
        await _add(f"providerId={OWNER_ID} | providerId=provider-a")
    text = str(excinfo.value)
    assert "'provider-a' is a provider's name" in text
    assert "is a provider's name" in text and text.count(" is a provider's name") == 1
    assert f"'{OWNER_ID}' is" not in text  # the valid id is not blamed
    assert len(_posts(httpx_mock)) == 1  # the refused one created nothing

    # a name and an unknown value: both are reported, in one refusal
    with pytest.raises(ValueError) as excinfo:
        await _add("providerId=provider-b & providerId=provider-z")
    text = str(excinfo.value)
    assert "'provider-b' is a provider's name" in text
    assert "'provider-z' is neither the id nor the name" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression",
    [
        "anyStorage",
        "country=PL&type=ssd",
        # not the `providerId` key: another attribute that ends in it, or `==` (Onedata's to refuse)
        "myproviderId=provider-a",
        "providerId==provider-a",
    ],
)
async def test_an_expression_without_a_provider_operand_asks_for_no_listing(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, expression: str
) -> None:
    _set_env(monkeypatch)
    _rule_is_created(httpx_mock)

    await _add(expression)

    assert [r.method for r in httpx_mock.get_requests()] == ["POST"]
    assert _posts(httpx_mock)[0]["expression"] == expression


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 403, 404])
async def test_a_failed_listing_does_not_block_the_rule(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces/{SPACE_ID}", status_code=status)
    _rule_is_created(httpx_mock)

    result = await _add("providerId=provider-a")

    assert result == {"qosRequirementId": "q1"}
    assert _posts(httpx_mock)[0]["expression"] == "providerId=provider-a"  # as before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing", [{"providers": []}, {"providers": "none"}, ["not", "a", "dict"]]
)
async def test_an_empty_or_malformed_listing_does_not_block_the_rule(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, listing: object
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces/{SPACE_ID}", json=listing)
    _rule_is_created(httpx_mock)

    await _add("providerId=provider-a")

    assert _posts(httpx_mock)[0]["expression"] == "providerId=provider-a"


@pytest.mark.asyncio
async def test_a_file_id_that_does_not_decode_asks_for_no_listing(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The space cannot be told from the id: nothing is refused, no request is added."""
    _set_env(monkeypatch)
    _rule_is_created(httpx_mock)

    await _add("providerId=provider-a", file_id="094576776E66743172323067776777")

    assert [r.method for r in httpx_mock.get_requests()] == ["POST"]


@pytest.mark.asyncio
async def test_the_check_is_off_unless_asked_for(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The readiness tools and the fixtures call add_qos_requirement with ids Onedata gave them:
    they get no extra request."""
    _set_env(monkeypatch)
    _rule_is_created(httpx_mock)

    await qos.add_qos_requirement(FILE_ID, "providerId=provider-a")

    assert [r.method for r in httpx_mock.get_requests()] == ["POST"]


@pytest.mark.asyncio
async def test_a_path_is_resolved_first_and_its_space_is_the_one_checked(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/lookup-file-id/%2Fspace-x%2Flog.dat",
        json={"fileId": FILE_ID},
    )
    _space_lists(httpx_mock)

    with pytest.raises(ValueError, match="is a provider's name"):
        await _add("providerId=provider-b", file_id="/space-x/log.dat")

    assert _posts(httpx_mock) == []
