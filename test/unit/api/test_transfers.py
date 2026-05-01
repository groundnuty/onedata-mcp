"""Unit tests for the transfers API client.

Mocked HTTP against shapes pinned to Onedata 25.0 swagger.
"""

import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import transfers


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token-z")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://oneprovider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


@pytest.mark.asyncio
async def test_list_space_transfers_returns_ids_and_page_token(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    space_id = "fb519d81146bcc635b890ff03a5da0fdch34fe"
    httpx_mock.add_response(
        method="GET",
        url=(
            f"https://oneprovider.example/api/v3/oneprovider/spaces/{space_id}/transfers"
            "?state=ongoing&limit=100"
        ),
        json={
            "transfers": [
                "2727a9fe5f5df6b43a8033386d2990e8ch5df6",
                "4bd9b58f6387622bf07f7388945e4fc4ch8762",
            ],
            "nextPageToken": "8471726779817b3a",
        },
    )

    result = await transfers.list_space_transfers(space_id)

    assert result["transfers"] == [
        "2727a9fe5f5df6b43a8033386d2990e8ch5df6",
        "4bd9b58f6387622bf07f7388945e4fc4ch8762",
    ]
    assert result["nextPageToken"] == "8471726779817b3a"


@pytest.mark.asyncio
async def test_list_space_transfers_passes_state_and_limit_and_token(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    space_id = "sp1"
    httpx_mock.add_response(
        method="GET",
        url=(
            f"https://oneprovider.example/api/v3/oneprovider/spaces/{space_id}/transfers"
            "?state=ended&limit=3&page_token=757136151113c2f"
        ),
        json={"transfers": [], "nextPageToken": None},
    )

    result = await transfers.list_space_transfers(
        space_id, state="ended", limit=3, page_token="757136151113c2f"
    )

    assert result["transfers"] == []
    assert result["nextPageToken"] is None


@pytest.mark.asyncio
async def test_list_space_transfers_rejects_limit_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)

    with pytest.raises(ValueError, match="between 1 and 1000"):
        await transfers.list_space_transfers("sp1", limit=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await transfers.list_space_transfers("sp1", limit=1001)


@pytest.mark.asyncio
async def test_get_transfer_returns_detail(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    tid = "2727a9fe5f5df6b43a8033386d2990e8ch5df6"
    httpx_mock.add_response(
        method="GET",
        url=f"https://oneprovider.example/api/v3/oneprovider/transfers/{tid}",
        json={
            "type": "migration",
            "spaceId": "sp1",
            "fileId": "fileXYZ",
            "filePath": "/sp1/datasets/x.bin",
            "transferState": "active",
            "replicatingProviderId": "p_dst",
            "evictingProviderId": "p_src",
            "scheduleTime": 1700000000,
            "startTime": 1700000005,
            "finishTime": 0,
            "filesToProcess": 1,
            "filesProcessed": 0,
            "bytesReplicated": 0,
        },
    )

    result = await transfers.get_transfer(tid)

    assert result["type"] == "migration"
    assert result["replicatingProviderId"] == "p_dst"
    assert result["evictingProviderId"] == "p_src"
