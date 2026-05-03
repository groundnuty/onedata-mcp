"""Unit tests for OpenAICompatAdapter — retry-with-backoff on transient
upstream failures.

Witnessed 2026-05-02 on V4-pro via OpenRouter→SiliconFlow at
--scenario-parallelism 1: 8 of 9 trials hit RateLimitError mid-trial,
burning the budget without producing usable records. The retry helper
gives upstream rate-limit windows time to clear.

These tests verify:
- 429 (RateLimitError) → retried with backoff, eventually succeeds
- APITimeoutError / APIConnectionError → also retried (transient class)
- Non-transient errors (BadRequestError, AuthenticationError, etc.)
  → propagate immediately, no retry
- All retries exhausted → final error propagates with last exception type
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from benchmark.llm_adapters.openai_compat import _create_with_retry


def _make_rate_limit_error() -> RateLimitError:
    """Mint a real RateLimitError without doing live HTTP."""
    response = httpx.Response(
        status_code=429,
        headers={"retry-after": "1"},
        text='{"error": {"message": "rate limited", "code": 429}}',
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    return RateLimitError("rate limited", response=response, body=None)


def _make_timeout_error() -> APITimeoutError:
    """APITimeoutError — transient, also retried."""
    return APITimeoutError(request=httpx.Request("POST", "https://example.com/v1/chat/completions"))


def _success_response() -> object:
    class FakeResp:
        choices = ()
        usage = None

    return FakeResp()


@pytest.mark.asyncio
async def test_retry_succeeds_after_one_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """First call 429s, retry succeeds. Verifies the retry path works."""
    # Patch sleep to keep tests fast.
    import benchmark.llm_adapters.openai_compat as mod

    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock(return_value=None))

    call_count = 0

    async def _fake_create(**_kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _make_rate_limit_error()
        return _success_response()

    fake_client = AsyncMock()
    fake_client.chat.completions.create = _fake_create

    result = await _create_with_retry(
        fake_client,
        model="x",
        messages=[],
        tools=[],
        temperature=0.0,
        max_tokens=100,
        extra_body=None,
    )
    assert result is not None
    assert call_count == 2  # initial + 1 retry


@pytest.mark.asyncio
async def test_retry_eventually_exhausts(monkeypatch: pytest.MonkeyPatch) -> None:
    """All RETRY_MAX_ATTEMPTS attempts 429 — final RateLimitError propagates."""
    import benchmark.llm_adapters.openai_compat as mod

    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock(return_value=None))

    call_count = 0

    async def _fake_create(**_kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        raise _make_rate_limit_error()

    fake_client = AsyncMock()
    fake_client.chat.completions.create = _fake_create

    with pytest.raises(RateLimitError):
        await _create_with_retry(
            fake_client,
            model="x",
            messages=[],
            tools=[],
            temperature=0.0,
            max_tokens=100,
            extra_body=None,
        )
    # Reads the constant rather than hardcoding so future bumps
    # don't break the test.
    assert call_count == mod.RETRY_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_retry_handles_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """APITimeoutError is also a transient error — retried."""
    import benchmark.llm_adapters.openai_compat as mod

    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock(return_value=None))

    call_count = 0

    async def _fake_create(**_kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise _make_timeout_error()
        return _success_response()

    fake_client = AsyncMock()
    fake_client.chat.completions.create = _fake_create

    result = await _create_with_retry(
        fake_client,
        model="x",
        messages=[],
        tools=[],
        temperature=0.0,
        max_tokens=100,
        extra_body=None,
    )
    assert result is not None
    assert call_count == 3


@pytest.mark.asyncio
async def test_non_transient_error_propagates_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ValueError (or any non-transient error) is not retried."""
    import benchmark.llm_adapters.openai_compat as mod

    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(mod.asyncio, "sleep", sleep_mock)

    call_count = 0

    async def _fake_create(**_kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        raise ValueError("malformed request — not a transient class")

    fake_client = AsyncMock()
    fake_client.chat.completions.create = _fake_create

    with pytest.raises(ValueError, match="malformed request"):
        await _create_with_retry(
            fake_client,
            model="x",
            messages=[],
            tools=[],
            temperature=0.0,
            max_tokens=100,
            extra_body=None,
        )
    assert call_count == 1  # no retry attempted
    assert sleep_mock.call_count == 0  # didn't even hit the backoff path
