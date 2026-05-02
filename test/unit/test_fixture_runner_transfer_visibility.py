"""Tests for the wait-for-visibility loop in _prestage_transfer.

Background: pre-stage POSTs a new migration and Onedata returns the new
transferId immediately, but the transfer log endpoint
(`list_space_transfers`) has eventual-consistency lag — the just-created
tid can be invisible there for tens of seconds. Without the wait, the
agent's list_space_transfers during the trial misses the freshly-staged
tid and picks a leftover from an earlier P4 trial (transfer logs are
immutable in Onedata; per-LLM spaces accumulate trial history).
Witnessed 2026-05-02 in run T190757.

These tests pin the wait-loop behaviour using mocked
`transfers_api.list_space_transfers`:
- Tid visible on first poll → returns immediately
- Tid visible after a few polls → returns once it appears
- Tid never visible → raises RuntimeError after timeout
- Paging: tid on a non-first page → walks pages, finds it
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from benchmark.fixture_runner import _wait_for_transfer_visible


@pytest.mark.asyncio
async def test_returns_immediately_when_tid_visible_on_first_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot path: list returns the tid on the first call."""
    import benchmark.fixture_runner as f

    list_mock = AsyncMock(return_value={"transfers": ["target-tid"], "nextPageToken": None})
    monkeypatch.setattr(f.transfers_api, "list_space_transfers", list_mock)
    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(f.asyncio, "sleep", sleep_mock)

    await _wait_for_transfer_visible("space-id", "target-tid")
    # First state probed is "ongoing" — found there, no need to try "ended".
    assert list_mock.call_count == 1
    assert sleep_mock.call_count == 0  # didn't have to wait


@pytest.mark.asyncio
async def test_only_polls_ended_state_not_ongoing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wait-loop must specifically wait for ENDED state, not ongoing.
    An ongoing-then-ended race caused P4 fails on 2026-05-02 (run T195311)
    when the loop accepted visibility in ongoing and returned before the
    transfer migrated to ended — missing the agent's later
    list_space_transfers(state="ended") query."""
    import benchmark.fixture_runner as f

    states_queried: list[str] = []

    async def _fake_list(_space_id: str, *, state: str, limit: int, page_token: str | None) -> dict:
        states_queried.append(state)
        # tid is "ongoing" forever — the wait-loop must NOT return.
        if state == "ongoing":
            return {"transfers": ["target-tid"], "nextPageToken": None}
        # ENDED never has it — wait-loop should time out
        return {"transfers": [], "nextPageToken": None}

    monkeypatch.setattr(f.transfers_api, "list_space_transfers", _fake_list)
    monkeypatch.setattr(f.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(f, "TRANSFER_VISIBILITY_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(RuntimeError, match="did not appear"):
        await _wait_for_transfer_visible("space-id", "target-tid")
    # Wait-loop should ONLY query ended state, never ongoing.
    assert "ongoing" not in states_queried, (
        f"Wait-loop should query only 'ended' state, but queried: {set(states_queried)}"
    )
    assert "ended" in states_queried


@pytest.mark.asyncio
async def test_walks_pages_when_tid_is_on_a_later_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-LLM space has many transfers; target tid is on page 2."""
    import benchmark.fixture_runner as f

    pages = {
        ("space-id", "ended", None): {"transfers": ["other1"], "nextPageToken": "page2"},
        ("space-id", "ended", "page2"): {
            "transfers": ["target-tid", "other2"],
            "nextPageToken": None,
        },
    }

    async def _fake_list(space_id: str, *, state: str, limit: int, page_token: str | None) -> dict:
        return pages.get(
            (space_id, state, page_token),
            {"transfers": [], "nextPageToken": None},
        )

    monkeypatch.setattr(f.transfers_api, "list_space_transfers", _fake_list)
    monkeypatch.setattr(f.asyncio, "sleep", AsyncMock(return_value=None))

    await _wait_for_transfer_visible("space-id", "target-tid")


@pytest.mark.asyncio
async def test_returns_after_eventual_consistency_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First few polls miss the tid (eventual consistency); 4th poll finds it."""
    import benchmark.fixture_runner as f

    call_count = 0
    poll_when_visible = 4

    async def _fake_list(_space_id: str, *, state: str, limit: int, page_token: str | None) -> dict:
        nonlocal call_count
        if state == "ended":
            call_count += 1
        if call_count >= poll_when_visible and state == "ended":
            return {"transfers": ["target-tid"], "nextPageToken": None}
        return {"transfers": [], "nextPageToken": None}

    monkeypatch.setattr(f.transfers_api, "list_space_transfers", _fake_list)
    monkeypatch.setattr(f.asyncio, "sleep", AsyncMock(return_value=None))

    await _wait_for_transfer_visible("space-id", "target-tid")
    assert call_count >= poll_when_visible


@pytest.mark.asyncio
async def test_raises_after_timeout_when_tid_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tid never shows up — RuntimeError so the trial marks RESET_FAIL
    rather than silently producing a P4 oracle mismatch."""
    import benchmark.fixture_runner as f

    list_mock = AsyncMock(return_value={"transfers": [], "nextPageToken": None})
    monkeypatch.setattr(f.transfers_api, "list_space_transfers", list_mock)
    monkeypatch.setattr(f.asyncio, "sleep", AsyncMock(return_value=None))
    # Crank the timeout down so the test doesn't sit forever; sleeps are
    # mocked so this is fast either way, but the deadline check uses real
    # time.time(). 0.1s is enough for the deadline to expire after the
    # first poll round.
    monkeypatch.setattr(f, "TRANSFER_VISIBILITY_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(RuntimeError, match="did not appear in list_space_transfers"):
        await _wait_for_transfer_visible("space-id", "target-tid")
