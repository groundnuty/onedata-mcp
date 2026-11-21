"""Unit tests for the transfers API client.

Mocked HTTP against shapes pinned to Onedata 25.0 swagger.
"""

import json

import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import transfers
from onedata_mcp.utils import MAX_ERROR_CHARS, OnedataApiError


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


@pytest.mark.asyncio
async def test_schedule_file_replication_posts_replication_body(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A file-id passes through un-resolved; body is a replication transfer."""
    import json

    _set_env(monkeypatch)
    file_id = "0000000000521f5567756964236600a3ch3f2e"
    target = "27c0ba1e...d591"
    httpx_mock.add_response(
        method="POST",
        url="https://oneprovider.example/api/v3/oneprovider/transfers",
        json={"transferId": "tr-abc123"},
    )

    result = await transfers.schedule_file_replication(file_id, target)

    assert result == {"transferId": "tr-abc123"}
    sent = json.loads(httpx_mock.get_requests()[-1].content)
    assert sent["type"] == "replication"
    assert sent["fileId"] == file_id
    assert sent["replicatingProviderId"] == target
    assert "evictingProviderId" not in sent  # replication only


@pytest.mark.asyncio
async def test_schedule_file_replication_surfaces_api_error(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Target provider not supporting the space -> structured OnedataApiError."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://oneprovider.example/api/v3/oneprovider/transfers",
        status_code=400,
        json={"error": {"id": "spaceNotSupportedBy", "details": {"providerId": "p2"}}},
    )

    with pytest.raises(OnedataApiError):
        await transfers.schedule_file_replication("file-id-x", "p2")


# ---------------------------------------------------------------------------
# schedule_file_replication: the error paths a model can trigger (design/12, gate 1).
#
# The swagger (paths/transfers.yaml, POST) lists 201, 400, 403, 404 and 500, each failure with
# the generic Error body. The 25.0 error ids and details below are PLAUSIBLE SHAPES, NOT
# RECORDED ONES: the live test (test/live/test_transfer_live.py) prints the real answers.
# ---------------------------------------------------------------------------

BASE = "https://oneprovider.example/api/v3/oneprovider"
TRANSFERS_URL = f"{BASE}/transfers"
SPACE_ID = "9JO5__9nL1b01YEvwnft1r20gwgwlkIp19ND_n8hgxs"
PROVIDERS = {
    "spaceId": SPACE_ID,
    "name": "k2val-x",
    "providers": [
        {"providerId": "pLocal0123456789abcdef", "providerName": "vistula-dc"},
        {"providerId": "pOwner0123456789abcdef", "providerName": "nordic-marine-dc"},
    ],
}


def _guid(space_id: str = SPACE_ID, uuid: str = "t_w-ghd1c7eDoMvTmo-gW_3aszvpHkGzy6Xw4") -> str:
    """A file id of the 25.0 shape: an object-id header, then `guid#<uuid>#<space id>` in hex."""
    return "00000000005CF470" + f"guid#{uuid}#{space_id}".encode().hex().upper()


FILE_ID = _guid()


def _post_error(httpx_mock: HTTPXMock, status: int, body: dict) -> None:
    httpx_mock.add_response(method="POST", url=TRANSFERS_URL, status_code=status, json=body)


async def _failure(file_arg: str, target: str) -> str:
    with pytest.raises(OnedataApiError) as excinfo:
        await transfers.schedule_file_replication(file_arg, target)
    return str(excinfo.value)


def test_the_space_id_is_read_from_a_file_id_of_the_25_shape() -> None:
    # the swagger's own example (definitions/transfer/transfer_file_create_request.yaml)
    example = (
        "00000000005CF4706775696423745F772D67686431633765446F4D76546D6F2D67575F3361737A7670486B"
        "477A7936587734507265584A7723394A4F355F5F396E4C31623031594576776E667431723230677767776C"
        "6B497031394E445F6E3868677873"
    )
    assert transfers._space_id_of_file_id(example) == SPACE_ID
    assert transfers._space_id_of_file_id(FILE_ID) == SPACE_ID
    for other in ("file-id-x", "", "abcd", "00" * 20, "/space/file.dat"):
        assert transfers._space_id_of_file_id(other) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_arg", "needle"),
    [
        ("k2val-x/logs/log_20200105.dat", "Paths must start with '/'"),
        ("logs/log_20200105.dat", "Paths must start with '/'"),
        ("", "is empty"),
        ("   ", "is empty"),
        (".", "is not a file id or a path"),
        ("..", "is not a file id or a path"),
        # a path must name its space first
        ("/log_20200105.dat", "must name the space first"),
        ("/", "must name the space first"),
        ("/k2val-x", "must name the space first"),
        ("/k2val-x/", "must name the space first"),
        (" /log_20200105.dat ", "must name the space first"),
    ],
)
async def test_a_wrong_form_of_file_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, file_arg: str, needle: str
) -> None:
    _set_env(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        await transfers.schedule_file_replication(file_arg, "pLocal0123456789abcdef")

    text = str(excinfo.value)
    assert needle in text
    assert "/<space_name>/<path_to_file>" in text  # says what form works
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "needle"),
    [
        ("", "is empty"),
        ("  ", "is empty"),
        ("/k2val-x/vistula-dc", "is not a provider id"),
        ("..", "is not a provider id"),
    ],
)
async def test_a_wrong_form_of_target_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, target: str, needle: str
) -> None:
    _set_env(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        await transfers.schedule_file_replication(FILE_ID, target)

    assert needle in str(excinfo.value)
    assert "supports the file's space" in str(excinfo.value)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_a_path_is_percent_encoded_whole_into_the_lookup_and_nothing_into_the_post_path(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """URL-encoding of every value put into a path: the lookup takes the whole path encoded,
    and the POST carries the file and provider ids in its body, never in its URL."""
    _set_env(monkeypatch)
    path = "/k2val x/logs/a?b#c.dat"
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/lookup-file-id/%2Fk2val%20x%2Flogs%2Fa%3Fb%23c.dat",
        json={"fileId": FILE_ID},
    )
    httpx_mock.add_response(method="POST", url=TRANSFERS_URL, json={"transferId": "t1"})

    assert await transfers.schedule_file_replication(path, "p?x#y") == {"transferId": "t1"}

    lookup, post = httpx_mock.get_requests()
    assert lookup.url.raw_path.decode() == (
        "/api/v3/oneprovider/lookup-file-id/%2Fk2val%20x%2Flogs%2Fa%3Fb%23c.dat"
    )
    assert post.url.raw_path.decode() == "/api/v3/oneprovider/transfers" and not post.url.query
    assert json.loads(post.content) == {
        "type": "replication",
        "dataSourceType": "file",
        "fileId": FILE_ID,
        "replicatingProviderId": "p?x#y",  # in the body, as given
    }


@pytest.mark.asyncio
async def test_an_id_with_url_characters_goes_into_the_body_only(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(method="POST", url=TRANSFERS_URL, json={"transferId": "t1"})

    await transfers.schedule_file_replication("abc?x=1#frag", "pLocal")

    (post,) = httpx_mock.get_requests()
    assert post.url.raw_path.decode() == "/api/v3/oneprovider/transfers" and not post.url.query
    assert json.loads(post.content)["fileId"] == "abc?x=1#frag"


@pytest.mark.asyncio
async def test_an_unknown_path_is_a_not_found_error_without_a_transfer(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/lookup-file-id/%2Fk2val-x%2Flogs%2Fnope.dat",
        status_code=400,
        json={"error": {"id": "posix", "details": {"errno": "enoent"}, "description": "enoent"}},
    )

    with pytest.raises(FileNotFoundError, match='Path "/k2val-x/logs/nope.dat" not found'):
        await transfers.schedule_file_replication("/k2val-x/logs/nope.dat", "pLocal")

    (only,) = httpx_mock.get_requests()  # the lookup; no transfer was posted
    assert only.url.raw_path.decode().endswith("/lookup-file-id/%2Fk2val-x%2Flogs%2Fnope.dat")


@pytest.mark.asyncio
async def test_an_error_that_names_the_file_field_blames_the_file(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A file name, or an id that names no file, when Onedata says the file is the bad value."""
    _set_env(monkeypatch)
    _post_error(
        httpx_mock, 400, {"error": {"id": "badValueIdentifier", "details": {"key": "fileId"}}}
    )

    text = await _failure("log_20200105.dat", "pLocal0123456789abcdef")

    assert text.startswith("No file is known by 'log_20200105.dat'")
    assert "a path starting with '/'" in text and "A file name alone is neither" in text
    assert "(status=400)" in text  # Onedata's own message follows
    assert len(httpx_mock.get_requests()) == 1  # no provider list: Onedata named the file


NOT_FOUND_BODIES = [
    {"error": {"id": "notFound", "description": "The resource could not be found."}},
    {"error": {"id": "posix", "details": {"errno": "enoent"}, "description": "enoent"}},
    {"error": {}},  # a bare 404
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", NOT_FOUND_BODIES, ids=["notFound", "enoent", "bare"])
async def test_a_404_for_a_file_given_as_an_id_points_at_both_arguments_and_lists_the_providers(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, body: dict
) -> None:
    """The file is a valid id (the SR1i case: the brief gives it) and the target is not. Onedata
    names no field, so the file is not blamed; both arguments are pointed at, with the
    providers that can be the target."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, 404, body)
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces/{SPACE_ID}", json=PROVIDERS)

    text = await _failure(FILE_ID, "pUnknown")

    hint = text.split(" [", 1)[0]
    assert hint.startswith("Onedata found nothing for the replication of ")
    assert "did not say which argument it means" in hint and "Check both" in hint
    assert "the target is a provider id" in hint and "a file name alone is neither" in hint
    assert "vistula-dc (id 'pLocal0123456789abcdef')" in hint
    assert "nordic-marine-dc (id 'pOwner0123456789abcdef')" in hint
    assert "No file is known" not in hint
    assert "(status=404)" in text  # Onedata's own message follows


@pytest.mark.asyncio
async def test_a_404_for_a_file_name_given_as_an_id_falls_back_to_plain_words_for_providers(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """No space can be found from a bare name, so the providers sentence is the plain one."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, 404, {"error": {"id": "notFound"}})

    text = await _failure("log_20200105.dat", "pLocal0123456789abcdef")

    hint = text.split(" [", 1)[0]
    assert hint.startswith("Onedata found nothing for the replication of 'log_20200105.dat'")
    assert "a file name alone is neither" in hint
    assert "Give the id (not the name) of a provider that supports the file's space." in hint
    assert "No file is known" not in hint


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503])
@pytest.mark.parametrize(
    "body",
    [
        {"error": {"id": "notFound"}},
        {"error": {"id": "posix", "details": {"errno": "enoent"}}},
        {"error": {"id": "forbidden"}},
        {"error": {"id": "posix", "details": {"errno": "eacces"}}},
        {"error": {"id": "spaceNotSupportedBy", "details": {"spaceId": SPACE_ID}}},
        {"error": {"id": "badValueIdentifier", "details": {"key": "replicatingProviderId"}}},
    ],
    ids=["notFound", "enoent", "forbidden", "eacces", "unsupported", "provider-key"],
)
async def test_a_5xx_is_passed_on_unchanged_whatever_its_body_mentions(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict
) -> None:
    """A not-found or a permission hint needs a 4xx. A 5xx says the provider failed, not what
    the failure was about: no hint, no provider listing, Onedata's message as it is."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, status, body)

    text = await _failure(FILE_ID, "pLocal0123456789abcdef")

    assert text.startswith(f"Onedata API request failed: POST /transfers (status={status})")
    for hint_word in (
        "Not allowed",
        "No file is known",
        "Onedata found nothing",
        "Cannot replicate",
    ):
        assert hint_word not in text
    assert len(httpx_mock.get_requests()) == 1  # nothing fetched to build a hint


@pytest.mark.asyncio
async def test_a_5xx_with_enoent_on_the_path_lookup_is_not_called_not_found(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The shared lookup maps an `enoent` of any status to `Path ... not found`; here a 5xx says
    the provider failed, and the file may also not exist. Nothing is posted."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/lookup-file-id/%2Fk2val-x%2Flogs%2Fnope.dat",
        status_code=500,
        json={"error": {"id": "posix", "details": {"errno": "enoent"}}},
    )

    with pytest.raises(OnedataApiError) as excinfo:  # not FileNotFoundError
        await transfers.schedule_file_replication("/k2val-x/logs/nope.dat", "pLocal")

    text = str(excinfo.value)
    assert text.startswith("This provider answered 500 and mentioned a missing item: the provider")
    assert "may also not exist" in text and "not found" not in text.split(" [", 1)[0]
    assert "(status=500)" in text
    assert len(httpx_mock.get_requests()) == 1  # no transfer was posted


@pytest.mark.asyncio
async def test_a_target_that_does_not_support_the_space_lists_the_providers_that_do(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """By id: the id of a provider outside the space, or of no provider at all."""
    _set_env(monkeypatch)
    _post_error(
        httpx_mock,
        400,
        {
            "error": {
                "id": "spaceNotSupportedBy",
                "details": {"spaceId": SPACE_ID, "providerId": "pOder"},
                "description": "Space is not supported by provider pOder.",
            }
        },
    )
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces/{SPACE_ID}", json=PROVIDERS)

    text = await _failure(FILE_ID, "pOder")

    assert text.startswith("Cannot replicate to 'pOder': no provider with that id supports")
    assert "vistula-dc (id 'pLocal0123456789abcdef')" in text
    assert "nordic-marine-dc (id 'pOwner0123456789abcdef')" in text
    assert "is a provider's name" not in text
    assert "Space is not supported by provider pOder." in text  # Onedata's message follows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": {"id": "spaceNotSupportedBy", "details": {"spaceId": SPACE_ID}}}),
        (400, {"error": {"id": "badValueIdentifier", "details": {"key": "replicatingProviderId"}}}),
        (400, {"error": {"id": "badValue", "description": "bad value"}}),  # a bare 400
        (404, {"error": {"id": "notFound", "description": "not found"}}),
    ],
)
async def test_a_provider_name_given_as_the_id_is_answered_with_its_id_first(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict
) -> None:
    """By name: a brief names the site, and the tool takes its id. The hint opens with what to
    do, and never says that a provider which supports the space does not."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, status, body)
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces/{SPACE_ID}", json=PROVIDERS)

    text = await _failure(FILE_ID, "vistula-dc")

    assert text.startswith("Pass its id 'pLocal0123456789abcdef', not its name: 'vistula-dc'")
    hint = text.split(" [", 1)[0]
    assert "no provider with that id" not in hint and "not a valid provider id" not in hint
    assert "not a provider that supports" not in hint and "No file is known" not in hint


@pytest.mark.asyncio
async def test_a_provider_field_key_without_a_provider_list_claims_no_non_support(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Onedata names the provider field but the providers cannot be listed: the value is not a
    valid provider id for the space; whether some provider supports the space is not claimed."""
    _set_env(monkeypatch)
    _post_error(
        httpx_mock, 400, {"error": {"id": "badValueIdentifier", "details": {"key": "providerId"}}}
    )

    text = await _failure("file-id-x", "vistula-dc")

    hint = text.split(" [", 1)[0]
    assert hint.startswith("Cannot replicate to 'vistula-dc': it is not a valid provider id for")
    assert "Give the id (not the name)" in hint
    assert "no provider with that id supports" not in hint
    assert len(httpx_mock.get_requests()) == 1  # nothing to list the providers from


@pytest.mark.asyncio
async def test_a_bare_400_does_not_blame_one_argument(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _post_error(httpx_mock, 400, {"error": {"id": "badValue", "description": "bad value"}})
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces/{SPACE_ID}", json=PROVIDERS)

    text = await _failure(FILE_ID, "pLocal0123456789abcdef")

    assert "Check both arguments" in text
    assert "the target is a provider id" in text and "a path starting with '/'" in text
    assert "vistula-dc (id 'pLocal0123456789abcdef')" in text


@pytest.mark.asyncio
async def test_without_a_space_to_list_the_hint_falls_back_to_plain_words(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A file id of another shape and no space in the details: no extra request."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, 400, {"error": {"id": "spaceNotSupportedBy"}})

    text = await _failure("file-id-x", "vistula-dc")

    assert "Give the id (not the name) of a provider that supports the file's space." in text
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_a_failed_listing_does_not_hide_the_original_error(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _post_error(
        httpx_mock, 400, {"error": {"id": "spaceNotSupportedBy", "details": {"spaceId": SPACE_ID}}}
    )
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/spaces/{SPACE_ID}", status_code=500, json={"error": {}}
    )

    text = await _failure(FILE_ID, "pOder")

    assert text.startswith("Cannot replicate to 'pOder'")
    assert "Give the id (not the name)" in text
    assert "spaceNotSupportedBy" in text  # Onedata's own answer is still there


@pytest.mark.asyncio
async def test_a_space_id_in_the_details_that_is_not_an_id_is_not_put_into_a_url(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The details come from the server: an odd value falls back to the file id's space."""
    _set_env(monkeypatch)
    _post_error(
        httpx_mock, 400, {"error": {"id": "spaceNotSupportedBy", "details": {"spaceId": "../x?y"}}}
    )
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces/{SPACE_ID}", json=PROVIDERS)

    await _failure(FILE_ID, "pOder")

    paths = [r.url.raw_path.decode() for r in httpx_mock.get_requests()]
    assert paths == ["/api/v3/oneprovider/transfers", f"/api/v3/oneprovider/spaces/{SPACE_ID}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"error": {"id": "forbidden", "description": "You are not authorized."}}),
        (400, {"error": {"id": "posix", "details": {"errno": "eacces"}, "description": "eacces"}}),
        (403, {"error": {"id": "posix", "details": {"errno": "eperm"}, "description": "eperm"}}),
    ],
)
async def test_a_permission_error_says_a_retry_will_not_help(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict
) -> None:
    _set_env(monkeypatch)
    _post_error(httpx_mock, status, body)

    text = await _failure(FILE_ID, "pLocal0123456789abcdef")

    assert text.startswith("Not allowed to schedule a replication of ")
    assert "space_schedule_replication" in text and "will not help" in text
    assert "providers that support" not in text  # not mistaken for a provider problem


@pytest.mark.asyncio
async def test_a_server_error_is_passed_on_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Nothing the caller can fix: no hint, Onedata's message as it is."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, 500, {"error": {"id": "internalServerError", "description": "boom"}})

    text = await _failure(FILE_ID, "pLocal0123456789abcdef")

    assert text.startswith("Onedata API request failed: POST /transfers (status=500)")
    assert "Not allowed" not in text and "No file is known" not in text


@pytest.mark.asyncio
async def test_a_second_replication_of_the_same_file_is_sent_as_is(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Onedata does not refuse a replication of a file that is already being copied (the
    swagger lists no such status): the tool keeps no state of its own and adds no check."""
    _set_env(monkeypatch)
    httpx_mock.add_response(method="POST", url=TRANSFERS_URL, json={"transferId": "t1"})
    httpx_mock.add_response(method="POST", url=TRANSFERS_URL, json={"transferId": "t2"})

    first = await transfers.schedule_file_replication(FILE_ID, "pLocal0123456789abcdef")
    second = await transfers.schedule_file_replication(FILE_ID, "pLocal0123456789abcdef")

    assert (first, second) == ({"transferId": "t1"}, {"transferId": "t2"})


@pytest.mark.asyncio
async def test_a_long_value_is_echoed_cut_short(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _post_error(httpx_mock, 404, {"error": {"id": "notFound"}})

    text = await _failure("a" * 300, "pLocal0123456789abcdef")

    hint = text.split(" [", 1)[0]
    assert "a" * 81 not in hint and "..." in hint


# --- paths: the lookup's errors, a not-found after it, the providers from the space name ------

ONEZONE = "https://onezone.example/api/v3/onezone"
PATH = "/k2val-x/logs/log_20200110.dat"
LOOKUP_URL = f"{BASE}/lookup-file-id/%2Fk2val-x%2Flogs%2Flog_20200110.dat"
PLAIN_ID = "file0000000000000000000000000001"  # not of the guid shape: no space id inside


def _resolves(httpx_mock: HTTPXMock, file_id: str = PLAIN_ID) -> None:
    httpx_mock.add_response(method="POST", url=LOOKUP_URL, json={"fileId": file_id})


def _space_name_resolves(httpx_mock: HTTPXMock) -> None:
    """The space name `k2val-x` to its id: the user's spaces at the provider, each named by
    Onezone; then the space's providers."""
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces", json=[{"spaceId": SPACE_ID}])
    named = {"spaceId": SPACE_ID, "name": "k2val-x"}
    httpx_mock.add_response(method="GET", url=f"{ONEZONE}/spaces/{SPACE_ID}", json=named)
    httpx_mock.add_response(method="GET", url=f"{BASE}/spaces/{SPACE_ID}", json=PROVIDERS)


@pytest.mark.asyncio
async def test_both_arguments_are_stripped_before_they_are_checked_and_sent(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _resolves(httpx_mock)
    httpx_mock.add_response(method="POST", url=TRANSFERS_URL, json={"transferId": "t1"})

    await transfers.schedule_file_replication(f"  {PATH}\n", " pLocal0123456789abcdef ")

    lookup, post = httpx_mock.get_requests()
    assert lookup.url.raw_path.decode() == LOOKUP_URL.removeprefix("https://oneprovider.example")
    assert json.loads(post.content)["replicatingProviderId"] == "pLocal0123456789abcdef"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"error": {"id": "forbidden", "description": "You are not authorized."}}),
        (400, {"error": {"id": "posix", "details": {"errno": "eacces"}, "description": "eacces"}}),
    ],
)
async def test_a_permission_error_of_the_path_lookup_gets_the_permission_hint(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int, body: dict
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(method="POST", url=LOOKUP_URL, status_code=status, json=body)

    text = await _failure(PATH, "pLocal0123456789abcdef")

    assert text.startswith(f"Not allowed to schedule a replication of '{PATH}'")
    assert "will not help" in text
    assert len(httpx_mock.get_requests()) == 1  # no transfer was posted


@pytest.mark.asyncio
async def test_another_lookup_error_passes_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    body = {"error": {"id": "internalServerError"}}
    httpx_mock.add_response(method="POST", url=LOOKUP_URL, status_code=500, json=body)

    text = await _failure(PATH, "pLocal0123456789abcdef")

    assert text.startswith("Onedata API request failed: POST /lookup-file-id/")


@pytest.mark.asyncio
async def test_a_not_found_after_the_path_resolved_points_at_the_target_not_the_file(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The path resolved to a file, so a 404 of the POST is not about the file. The providers
    come from the path's space name, since this file id carries no space id."""
    _set_env(monkeypatch)
    _resolves(httpx_mock)
    _post_error(httpx_mock, 404, {"error": {"id": "notFound", "description": "not found"}})
    _space_name_resolves(httpx_mock)

    text = await _failure(PATH, "pUnknown")

    hint = text.split(" [", 1)[0]
    assert "No file is known" not in hint and "A file name alone" not in hint
    assert "The path resolved to a file, so check the target" in hint
    assert "vistula-dc (id 'pLocal0123456789abcdef')" in hint


@pytest.mark.asyncio
async def test_a_not_found_naming_the_file_field_still_blames_the_file(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _resolves(httpx_mock)
    _post_error(httpx_mock, 404, {"error": {"id": "notFound", "details": {"key": "fileId"}}})

    text = await _failure(PATH, "pLocal0123456789abcdef")

    assert text.startswith(f"No file is known by '{PATH}'")


@pytest.mark.asyncio
async def test_a_provider_name_with_a_path_is_resolved_through_the_space_name(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _resolves(httpx_mock)
    _post_error(httpx_mock, 400, {"error": {"id": "spaceNotSupportedBy"}})
    _space_name_resolves(httpx_mock)

    text = await _failure(PATH, "vistula-dc")

    assert text.startswith("Pass its id 'pLocal0123456789abcdef', not its name")


# --- every hint is redacted and bounded as a whole; names are matched raw ---------------------

HOST = "oneprovider.example"  # the configured Oneprovider (see _set_env)


@pytest.mark.asyncio
async def test_a_hint_that_echoes_the_configured_host_is_redacted_and_the_whole_is_bounded(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The target is echoed in the hint; Onedata's message is long. Neither gets past the
    redaction and the bound, which cover the hint and Onedata's text together."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, 404, {"error": {"id": "notFound", "description": "n" * 10_000}})

    text = await _failure("plain-file-id", HOST)

    assert text.startswith("Onedata found nothing for the replication of 'plain-file-id' to")
    assert HOST not in text
    assert "'oneprovider'" in text  # the configured host reads as its declared name
    assert len(text) == MAX_ERROR_CHARS and text.endswith("...")


@pytest.mark.asyncio
async def test_the_path_lookups_permission_hint_is_redacted_and_bounded_too(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/lookup-file-id/%2F{HOST}%2Fa.dat",
        status_code=403,
        json={"error": {"id": "forbidden", "description": "n" * 10_000}},
    )

    text = await _failure(f"/{HOST}/a.dat", "pLocal0123456789abcdef")

    assert text.startswith("Not allowed to schedule a replication of '/oneprovider/a.dat'")
    # the hint's echo is redacted (the request path that follows is the caller's own, encoded)
    assert HOST not in text.split(" [", 1)[0]
    assert len(text) == MAX_ERROR_CHARS and text.endswith("...")


@pytest.mark.asyncio
async def test_a_provider_whose_name_redaction_would_change_is_still_matched_by_its_raw_name(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A provider named like the configured host: redaction shows it as `oneprovider`, which
    is not what the caller typed. The target is matched against the name as Onedata gives it,
    so the caller still gets "Pass its id"; the name is redacted only as it is shown."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, 404, {"error": {"id": "notFound"}})
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/spaces/{SPACE_ID}",
        json={
            "spaceId": SPACE_ID,
            "providers": [
                {"providerId": "pHost0123456789abcdef", "providerName": HOST},
                {"providerId": "pOther0123456789abcdef", "providerName": "site-a"},
            ],
        },
    )

    text = await _failure(FILE_ID, HOST)

    assert text.startswith("Pass its id 'pHost0123456789abcdef', not its name: 'oneprovider'")
    assert HOST not in text
    assert "oneprovider (id 'pHost0123456789abcdef')" in text  # shown redacted


@pytest.mark.asyncio
async def test_a_target_that_reads_like_the_placeholder_for_a_nameless_provider_is_not_its_name(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A provider with no name is shown as `(no name)`: a target of that text is not a match."""
    _set_env(monkeypatch)
    _post_error(httpx_mock, 404, {"error": {"id": "notFound"}})
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/spaces/{SPACE_ID}",
        json={
            "spaceId": SPACE_ID,
            "providers": [
                {"providerId": "pNameless0123456789ab"},
                {"providerId": "pEmpty0123456789abcde", "providerName": ""},
                {"providerId": "pOther0123456789abcdef", "providerName": "site-a"},
            ],
        },
    )

    text = await _failure(FILE_ID, "(no name)")

    assert not text.startswith("Pass its id")
    hint = text.split(" [", 1)[0]
    assert "(no name) (id 'pNameless0123456789ab')" in hint
    assert "(no name) (id 'pEmpty0123456789abcde')" in hint
    assert "site-a (id 'pOther0123456789abcdef')" in hint


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
async def test_a_5xx_unsupported_space_on_the_path_lookup_is_not_a_missing_space(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, status: int
) -> None:
    """The shared stable resolver reads `spaceNotSupportedBy` on any status as "Space ... does
    not exist"; the replication's own lookup keeps a 5xx as the provider's failure."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/lookup-file-id/%2Fk2val-x%2Flogs%2Fa.dat",
        status_code=status,
        json={"error": {"id": "spaceNotSupportedBy", "details": {"spaceId": "s1"}}},
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await transfers.schedule_file_replication("/k2val-x/logs/a.dat", "pLocal")

    text = str(excinfo.value)
    assert "does not exist" not in text and f"(status={status})" in text
    assert len(httpx_mock.get_requests()) == 1  # no transfer was posted


@pytest.mark.asyncio
async def test_a_4xx_unsupported_space_on_the_path_lookup_says_so_without_a_missing_space(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/lookup-file-id/%2Fk2val-x%2Flogs%2Fa.dat",
        status_code=400,
        json={"error": {"id": "spaceNotSupportedBy", "details": {"spaceId": "s1"}}},
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await transfers.schedule_file_replication("/k2val-x/logs/a.dat", "pLocal")

    text = str(excinfo.value)
    assert text.startswith("This provider does not support the space of")
    assert "the space may still exist" in text and "does not exist" not in text.split(" [")[0]
