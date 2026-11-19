import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import spaces


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://oneprovider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


PROVIDER_SPACES_URL = "https://oneprovider.example/api/v3/oneprovider/spaces"
ONEZONE_ADMIN_LISTING_PATH = "/api/v3/onezone/spaces"


def _provider_listing_entry(space_id: str, name: str) -> dict:
    """One entry of Oneprovider's GET /spaces answer (Onedata 25.0)."""
    return {
        "name": name,
        "spaceId": space_id,
        "providers": [{"providerId": "p1", "providerName": "Provider One"}],
        "fileId": f"root-{space_id}",
        "dirId": f"root-{space_id}",
        "trashDirId": f"trash-{space_id}",
        "archivesDirId": f"archives-{space_id}",
    }


@pytest.mark.asyncio
async def test_get_space_details_returns_response_body(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/sp1",
        json={
            "spaceId": "sp1",
            "name": "Space One",
            "description": "desc",
            "providers": {"p1": {}},
            "organizationName": "Org",
            "creationTime": 1700000000,
            "tags": ["a", "b"],
        },
    )

    result = await spaces.get_space_details("sp1")

    assert result["spaceId"] == "sp1"
    assert result["name"] == "Space One"


@pytest.mark.asyncio
async def test_list_user_spaces_fetches_ids_and_details_and_maps_fields(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url=PROVIDER_SPACES_URL,
        json=[
            _provider_listing_entry("sp1", "Space One"),
            _provider_listing_entry("sp2", "Space Two"),
        ],
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/sp1",
        json={
            "spaceId": "sp1",
            "name": "Space One",
            "description": "desc1",
            "providers": {"p1": {}},
            "organizationName": "Org1",
            "creationTime": 1700000001,
            "tags": ["tag1"],
            "ignoredField": "ignored",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/sp2",
        json={
            "spaceId": "sp2",
            "name": "Space Two",
            "description": "desc2",
            "providers": {"p2": {}},
            "organizationName": "Org2",
            "creationTime": 1700000002,
            "tags": [],
        },
    )

    result = await spaces.list_user_spaces()

    assert len(result) == 2
    assert result[0] == {
        "tags": ["tag1"],
        "description": "desc1",
        "spaceId": "sp1",
        "providers": {"p1": {}},
        "organizationName": "Org1",
        "name": "Space One",
        "creationTime": 1700000001,
    }
    assert result[1]["spaceId"] == "sp2"
    assert "ignoredField" not in result[0]


@pytest.mark.asyncio
async def test_list_user_spaces_returns_empty_when_no_spaces(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(method="GET", url=PROVIDER_SPACES_URL, json=[])

    result = await spaces.list_user_spaces()

    assert result == []


@pytest.mark.asyncio
async def test_get_marketplace_space_details_returns_response_body(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/marketplace/sp1",
        json={
            "name": "Meteo dataset",
            "index": "Meteo dataset@2c0160248ba9a66f45da751ca459535a",
            "description": "Meteorological data for major Polish cities.",
            "organizationName": "ACK Cyfronet AGH",
            "tags": ["archival", "big-data", "open-data", "environment"],
            "providerNames": ["krakow", "paris"],
            "totalSupportSize": 30500000000,
            "creationTime": 1576152793,
        },
    )

    result = await spaces.get_marketplace_space_details("sp1")

    assert result["name"] == "Meteo dataset"
    assert result["organizationName"] == "ACK Cyfronet AGH"
    assert result["providerNames"] == ["krakow", "paris"]


@pytest.mark.asyncio
async def test_list_marketplace_spaces_returns_paginated_detailed_list(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://onezone.example/api/v3/onezone/spaces/marketplace/list",
        json={
            "spaces": [
                {"spaceId": "sp1", "index": "aqua@sp1"},
                {"spaceId": "sp2", "index": "terra@sp2"},
            ],
            "isLast": False,
            "nextPageToken": "next-token",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/marketplace/sp1",
        json={
            "name": "Aqua dataset",
            "description": "Water data",
            "organizationName": "Org 1",
            "tags": ["open-data"],
            "providerNames": ["krakow"],
            "totalSupportSize": 100,
            "creationTime": 1576152793,
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://onezone.example/api/v3/onezone/spaces/marketplace/sp2",
        json={
            "name": "Terra dataset",
            "index": "terra@sp2",
            "description": "Soil data",
            "organizationName": "Org 2",
            "tags": ["environment"],
            "providerNames": ["paris"],
            "totalSupportSize": 200,
            "creationTime": 1576152794,
        },
    )

    result = await spaces.list_marketplace_spaces(tags=["open-data"], limit=2, offset=0)

    assert result["isLast"] is False
    assert result["nextPageToken"] == "next-token"
    assert len(result["spaces"]) == 2
    assert result["spaces"][0]["spaceId"] == "sp1"
    assert result["spaces"][0]["index"] == "aqua@sp1"
    assert result["spaces"][1]["spaceId"] == "sp2"
    assert result["spaces"][1]["index"] == "terra@sp2"

    requests = httpx_mock.get_requests()
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v3/onezone/spaces/marketplace/list"
    assert requests[0].content == b'{"limit":2,"offset":0,"tags":["open-data"]}'


@pytest.mark.asyncio
async def test_list_marketplace_spaces_rejects_limit_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)

    with pytest.raises(ValueError, match="between 1 and 50"):
        await spaces.list_marketplace_spaces(limit=0)

    with pytest.raises(ValueError, match="between 1 and 50"):
        await spaces.list_marketplace_spaces(limit=51)


# ---------------------------------------------------------------------------
# get_space_providers (Onedata 25.0, GET oneprovider /spaces/{sid})
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_space_providers_calls_oneprovider_not_onezone(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://oneprovider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    space_id = "fb519d81146bcc635b890ff03a5da0fdch34fe"
    httpx_mock.add_response(
        method="GET",
        url=f"https://oneprovider.example/api/v3/oneprovider/spaces/{space_id}",
        json={
            "name": "My Space 1",
            "spaceId": space_id,
            "providers": [
                {"providerId": "p1", "providerName": "MyPrivateCloud"},
                {"providerId": "p2", "providerName": "PublicCloud1"},
            ],
            "fileId": "rootFileId",
            "dirId": "rootFileId",
            "trashDirId": "trashId",
            "archivesDirId": "archivesId",
        },
    )

    result = await spaces.get_space_providers(space_id)

    assert result["name"] == "My Space 1"
    assert len(result["providers"]) == 2
    assert {p["providerId"] for p in result["providers"]} == {"p1", "p2"}
    # Confirm we hit the oneprovider host, not onezone
    requests = httpx_mock.get_requests()
    assert requests[0].url.host == "oneprovider.example"
    assert "/api/v3/oneprovider/spaces/" in requests[0].url.path


# ---------------------------------------------------------------------------
# resolve_space_id_or_name: a name resolves through the user's own spaces,
# read from Oneprovider's GET /spaces, never Onezone's admin listing
# ---------------------------------------------------------------------------


def _mock_user_spaces(httpx_mock: HTTPXMock, spaces_by_id: dict[str, str]) -> None:
    httpx_mock.add_response(
        method="GET",
        url=PROVIDER_SPACES_URL,
        json=[_provider_listing_entry(sid, name) for sid, name in spaces_by_id.items()],
    )
    for sid, name in spaces_by_id.items():
        httpx_mock.add_response(
            method="GET",
            url=f"https://onezone.example/api/v3/onezone/spaces/{sid}",
            json={"spaceId": sid, "name": name, "providers": {"p1": 1024}},
        )


@pytest.mark.asyncio
async def test_resolve_space_name_reads_the_provider_listing_not_the_onezone_admin_listing(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _mock_user_spaces(httpx_mock, {"sp-a": "Alpha", "sp-b": "Beta"})

    result = await spaces.resolve_space_id_or_name("Beta")

    assert result == "sp-b"
    requests = httpx_mock.get_requests()
    assert requests[0].url.host == "oneprovider.example"
    assert requests[0].url.path == "/api/v3/oneprovider/spaces"
    assert all(r.url.path != ONEZONE_ADMIN_LISTING_PATH for r in requests)


@pytest.mark.asyncio
async def test_resolve_space_unknown_name_lists_only_the_users_own_names(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _mock_user_spaces(httpx_mock, {"sp-a": "Alpha"})

    with pytest.raises(ValueError, match=r"'Gamma' not found\. Available names: \['Alpha'\]"):
        await spaces.resolve_space_id_or_name("Gamma")


@pytest.mark.asyncio
async def test_resolve_space_id_shaped_value_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    space_id = "9742830720c0ef94496dad1d96595736ch776e"

    assert await spaces.resolve_space_id_or_name(space_id) == space_id
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_list_user_spaces_refuses_a_listing_that_is_not_a_list(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(method="GET", url=PROVIDER_SPACES_URL, json={"spaces": ["sp1"]})

    with pytest.raises(ValueError, match="answered a dict, not a list of spaces"):
        await spaces.list_user_spaces()


@pytest.mark.asyncio
async def test_list_user_spaces_refuses_an_entry_without_a_space_id(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(method="GET", url=PROVIDER_SPACES_URL, json=[{"name": "Alpha"}])

    with pytest.raises(ValueError, match="an entry without a spaceId"):
        await spaces.list_user_spaces()
