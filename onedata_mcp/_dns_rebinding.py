"""ASGI middleware enforcing DNS-rebinding protection on the HTTP
transport.

Per the MCP-2025-11-25 security spec
(https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices#local-mcp-server-compromise),
local MCP servers MUST reject HTTP requests whose Host or Origin
header does not match the server's bound address. Without this check,
a malicious website can use DNS-rebinding to point a domain at
``127.0.0.1`` and issue ``fetch()`` requests against the user's
locally-running MCP server.

Rejection contract:
  - ``Host`` header MUST match an allow-listed value
    (``127.0.0.1:<port>``, ``localhost:<port>``, or — when the server
    binds to a non-loopback interface — that interface).
  - ``Origin`` header, when present, MUST be a scheme variant of an
    allow-listed Host. (Browser fetch() always sends Origin; non-
    browser clients omit it; we accept the absence.)
  - Otherwise, return HTTP 403.

Activates only on the HTTP transport — stdio is unaffected because
stdio doesn't speak HTTP headers at all.

See ``research/empirical-mcp-server-findings.md#m-13`` for the
finding history.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# Starlette's ASGI types. We do NOT import Starlette's Response/Middleware
# helpers because we want the middleware to be a plain ASGI app — it
# can be used by any ASGI framework, not just Starlette.
ASGIScope = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


class DnsRebindingProtection:
    """ASGI middleware that rejects requests with non-allow-listed
    Host or Origin headers.

    Constructor takes the bound ``host`` + ``port`` from the launcher
    (so the allow-list adapts to whatever the operator chose). Both
    ``127.0.0.1`` and ``localhost`` are accepted regardless of which
    address the server bound to (they refer to the same loopback).
    """

    def __init__(self, app: Any, *, host: str, port: int) -> None:
        self.app = app
        self.allowed_hosts = {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
        }
        # If the operator bound to something other than the loopback,
        # also accept that explicitly. Common case: 0.0.0.0 — operator
        # MUST provide an explicit host:port pair to allow-list (we
        # don't auto-include 0.0.0.0 because that's the wildcard).
        if host not in {"0.0.0.0", "::", "127.0.0.1", "localhost"}:
            self.allowed_hosts.add(f"{host}:{port}")
        # Origin allow-list mirrors hosts, both http and https schemes.
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts} | {
            f"https://{h}" for h in self.allowed_hosts
        }

    async def __call__(
        self,
        scope: ASGIScope,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        # Only validate HTTP requests (also catches `websocket`, fall through).
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        host = headers.get("host", "")
        origin = headers.get("origin")  # may be None

        if host not in self.allowed_hosts:
            await _send_403(send, f"Invalid Host header: {host!r}")
            return
        if origin is not None and origin not in self.allowed_origins:
            await _send_403(send, f"Invalid Origin header: {origin!r}")
            return

        await self.app(scope, receive, send)


async def _send_403(send: ASGISend, message: str) -> None:
    """Send an ASGI HTTP 403 response with a plain-text body.

    Kept as a free function (not a method) so unit tests can call it
    independently of an instantiated middleware.
    """
    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})
