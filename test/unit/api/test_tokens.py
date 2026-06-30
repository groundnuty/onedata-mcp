import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import tokens


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


@pytest.mark.asyncio
async def test_examine_access_token_posts_token_and_returns_body(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://onezone.example/api/v3/onezone/tokens/examine",
        json={"caveats": [{"type": "data.readonly"}], "type": {"accessToken": {}}},
    )

    result = await tokens.examine_access_token("serialized-token")

    assert isinstance(result, dict)
    assert result["caveats"][0]["type"] == "data.readonly"


@pytest.mark.asyncio
async def test_examine_access_token_rejects_non_object_body(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://onezone.example/api/v3/onezone/tokens/examine",
        json=["not", "an", "object"],
    )

    with pytest.raises(TypeError):
        await tokens.examine_access_token("serialized-token")


def test_readonly_caveat_detected_when_present() -> None:
    examined = {"caveats": [{"type": "time"}, {"type": "data.readonly"}]}
    assert tokens.token_has_data_readonly_caveat(examined) is True


def test_readonly_caveat_absent_when_no_caveats() -> None:
    assert tokens.token_has_data_readonly_caveat({"caveats": []}) is False
    assert tokens.token_has_data_readonly_caveat({}) is False


def test_readonly_caveat_tolerates_malformed_caveats() -> None:
    # caveats not a list, or entries not dicts — must not raise, must be False
    assert tokens.token_has_data_readonly_caveat({"caveats": "nope"}) is False
    assert tokens.token_has_data_readonly_caveat({"caveats": ["str", 1, None]}) is False
