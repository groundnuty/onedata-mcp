"""Unit tests for DnsRebindingProtection middleware (M-13 fix).

Pins the contract: requests with valid localhost Host/Origin pass
through; everything else is rejected with HTTP 403.

Tests use a minimal in-memory ASGI invocation pattern — no Starlette
test-client needed. The middleware is a plain ASGI app, so we drive
it directly with a fake `app` (records that it was called) plus a
fake `send` (captures the response).
"""

from __future__ import annotations

from typing import Any

import pytest

from onedata_mcp._dns_rebinding import DnsRebindingProtection


class _FakeApp:
    """Records whether the inner app was reached."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        # Send a benign 200 so callers that DO reach the app see a
        # complete response (not strictly required for these tests).
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"OK"})


def _http_scope(host: str | None, origin: str | None = None) -> dict[str, Any]:
    """Build a minimal HTTP ASGI scope with the given Host/Origin headers."""
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode("latin-1")))
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    return {"type": "http", "headers": headers}


class _Capture:
    """Captures the ASGI messages sent by the middleware."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        for m in self.messages:
            if m.get("type") == "http.response.start":
                return int(m["status"])
        return -1

    @property
    def body(self) -> bytes:
        chunks = b""
        for m in self.messages:
            if m.get("type") == "http.response.body":
                chunks += m.get("body", b"")
        return chunks


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


# ---------------------------------------------------------------------------
# Allowed hosts / origins — must pass through to the inner app.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loopback_127_host_accepted() -> None:
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw(_http_scope(host="127.0.0.1:3037"), _noop_receive, cap)
    assert inner.called is True
    assert cap.status == 200


@pytest.mark.asyncio
async def test_loopback_localhost_host_accepted() -> None:
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw(_http_scope(host="localhost:3037"), _noop_receive, cap)
    assert inner.called is True
    assert cap.status == 200


@pytest.mark.asyncio
async def test_valid_origin_with_valid_host_accepted() -> None:
    """Browsers send Origin alongside Host — both must validate."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw(
        _http_scope(host="127.0.0.1:3037", origin="http://127.0.0.1:3037"),
        _noop_receive,
        cap,
    )
    assert inner.called is True
    assert cap.status == 200


@pytest.mark.asyncio
async def test_no_origin_header_accepted() -> None:
    """Non-browser clients (curl, MCP SDKs) often don't send Origin.
    Absence is OK — only an INVALID Origin is rejected."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw(_http_scope(host="127.0.0.1:3037"), _noop_receive, cap)
    assert inner.called is True
    assert cap.status == 200


# ---------------------------------------------------------------------------
# Rejected — DNS-rebinding attack shapes the conformance suite tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evil_host_rejected_403() -> None:
    """The exact shape the conformance suite tests."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw(_http_scope(host="evil.example.com"), _noop_receive, cap)
    assert inner.called is False
    assert cap.status == 403
    assert b"Invalid Host" in cap.body


@pytest.mark.asyncio
async def test_evil_origin_with_valid_host_rejected_403() -> None:
    """Subtler: Host header lies about the target while Origin is the
    attacker site. Both checks fire."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw(
        _http_scope(host="127.0.0.1:3037", origin="http://evil.example.com"),
        _noop_receive,
        cap,
    )
    assert inner.called is False
    assert cap.status == 403
    assert b"Invalid Origin" in cap.body


@pytest.mark.asyncio
async def test_wrong_port_in_host_rejected() -> None:
    """Host header with the right name but wrong port — also rejected."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw(_http_scope(host="127.0.0.1:9999"), _noop_receive, cap)
    assert inner.called is False
    assert cap.status == 403


@pytest.mark.asyncio
async def test_empty_host_rejected() -> None:
    """An empty Host header must also be rejected (would be valid HTTP/1.0
    but is unsafe in our threat model)."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw(_http_scope(host=""), _noop_receive, cap)
    assert inner.called is False
    assert cap.status == 403


# ---------------------------------------------------------------------------
# Custom bind host — operator chose an explicit non-loopback host.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_bind_host_added_to_allowlist() -> None:
    """If operator binds to e.g. 192.168.1.10, that exact host:port is
    accepted IN ADDITION TO loopback. Other IPs still rejected."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="192.168.1.10", port=3037)
    cap = _Capture()
    await mw(_http_scope(host="192.168.1.10:3037"), _noop_receive, cap)
    assert inner.called is True
    assert cap.status == 200


@pytest.mark.asyncio
async def test_loopback_still_works_when_explicit_bind_used() -> None:
    """Bind = 192.168.1.10 doesn't mean loopback is rejected — operators
    can still curl 127.0.0.1 from the same machine."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="192.168.1.10", port=3037)
    cap = _Capture()
    await mw(_http_scope(host="127.0.0.1:3037"), _noop_receive, cap)
    assert inner.called is True
    assert cap.status == 200


@pytest.mark.asyncio
async def test_wildcard_bind_only_allows_loopback() -> None:
    """0.0.0.0 / :: bind means 'listen everywhere' but the allow-list
    stays at loopback only — operator must explicitly enumerate other
    hosts. This prevents the operator from accidentally allow-listing
    'every IP on the box' just by binding wildcard."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="0.0.0.0", port=3037)
    cap = _Capture()
    await mw(_http_scope(host="0.0.0.0:3037"), _noop_receive, cap)
    assert inner.called is False
    assert cap.status == 403


# ---------------------------------------------------------------------------
# Non-HTTP scopes — pass through unchanged (websocket, lifespan).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_scope_passes_through() -> None:
    """Middleware MUST NOT reject ASGI lifespan messages (server startup)."""
    inner = _FakeApp()
    mw = DnsRebindingProtection(inner, host="127.0.0.1", port=3037)
    cap = _Capture()
    await mw({"type": "lifespan"}, _noop_receive, cap)
    assert inner.called is True
