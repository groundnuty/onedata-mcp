from __future__ import annotations

import contextlib
import json
import logging
import re
import ssl
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from onedata_mcp import telemetry

if TYPE_CHECKING:
    from .config import OnedataConfig


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False


logger = logging.getLogger(__name__)


# An Onedata error id or errno is a plain identifier (`posix`, `enoent`, `badValueFileId`).
# Anything else (a URL, a host name) in those fields is not one, and is never
# passed on: both go into trace attributes and into the invalid-space check.
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")


def _identifier_or_none(value: Any) -> str | None:
    if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value):
        return value
    return None


class OnedataError(RuntimeError):
    """Base class for Onedata-related errors."""

    def __init__(
        self,
        message: str,
        response: dict[str, Any] | None = None,
        *,
        kind: str | None = None,
        service: str | None = None,
    ) -> None:
        super().__init__(message)
        self.response = response
        # Set on a transport failure: the stable error kind (see
        # `transport_error_kind`) and the declared name of the service.
        self.kind = kind
        self.service = service

    @property
    def body(self) -> dict[str, Any]:
        if not isinstance(self.response, dict):
            return {}
        body = self.response.get("body")
        return body if isinstance(body, dict) else {}

    @property
    def errno(self) -> str | None:
        error = self.body.get("error")
        if not isinstance(error, dict):
            return None
        details = error.get("details")
        if not isinstance(details, dict):
            return None
        return _identifier_or_none(details.get("errno"))

    @property
    def error_id(self) -> str | None:
        error = self.body.get("error")
        if not isinstance(error, dict):
            return None
        return _identifier_or_none(error.get("id"))


class OnedataPathNotFoundError(OnedataError):
    """Raised when a path is not found in Onedata."""


class OnedataApiError(OnedataError):
    """Raised when an Onedata API request fails."""


class OnedataInvalidSpaceError(OnedataError):
    """Raised when a space does not exist or is not supported by provider."""


def _format_body_for_log(body: Any) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, bytes):
        return f"<{len(body)} bytes>"
    try:
        return json.dumps(body, indent=2)
    except TypeError:
        return repr(body)


# ---------------------------------------------------------------------------
# Host-free errors
#
# FastMCP puts `str(exc)` into a tool's answer, and OpenTelemetry records an
# exception's text and stack trace (cause chain included) on the span it passes
# through. A transport exception's text names the node it could not reach
# (`HTTPSConnectionPool(host=<FQDN>, port=8443)`, a certificate's host name, a
# URL), so no raw transport text may travel in an exception raised to a tool.
# The answer carries a stable kind and the declared service name instead; the
# original text goes only to this server's own DEBUG log.
# ---------------------------------------------------------------------------


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception and everything it was raised from or while handling."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None


def transport_error_kind(exc: BaseException) -> str:
    """Classify an exception raised while calling a service, by type and never by text.

    One of `timeout`, `tls_error`, `http_<status>`, `connection_error`, or
    `request_error` for anything else. httpx wraps the `ssl` error of a failed
    handshake in a `ConnectError`, so the whole cause chain is read, with a
    timeout taking precedence over a TLS failure over a plain connection error.
    """
    chain = list(_exception_chain(exc))
    if any(isinstance(e, httpx.TimeoutException | TimeoutError) for e in chain):
        return "timeout"
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return "tls_error"
    for e in chain:
        if isinstance(e, httpx.HTTPStatusError):
            return f"http_{e.response.status_code}"
    if any(isinstance(e, httpx.TransportError | OSError) for e in chain):
        return "connection_error"
    return "request_error"


def transport_failure(
    exc: BaseException,
    config: OnedataConfig,
    method: str,
    path: str,
    *,
    label: str = "Onedata API request failed",
) -> OnedataApiError:
    """The host-free error to raise (`from None`) in place of a transport exception.

    The message holds the request's method and relative path, the error kind and
    the declared service name: no URL, address, port or library text. The
    original exception's full text is logged at DEBUG, on this server's own
    log, which no tool answer or span carries.
    """
    kind = transport_error_kind(exc)
    logger.debug(
        "Onedata transport failure (%s, service %s): %s %s: %s",
        kind,
        config.service,
        method,
        path,
        exc,
        exc_info=exc,
    )
    return OnedataApiError(
        f"{label}: {method} {path} - {kind} (service: {config.service})",
        response=None,
        kind=kind,
        service=config.service,
    )


# Patterns for the endpoints a service or a transport library prints. Every one
# that scans a run of name characters starts at the beginning of a token
# (`(?<![\w.+-])`) and every other repetition is bounded, so a hostile body of
# `a.a.a.a. ...` costs linear time, not a rescan from each label.
_TOKEN_START = r"(?<![\w.+-])"
_URL_RE = re.compile(_TOKEN_START + r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>)\]\\]+")
# `host='x'`, `port=8443`, `hostname 'x'` (a pool's or a certificate check's repr).
_KEYWORD_RE = re.compile(
    r"\b(?:host|hostname|port)(?:\s*[=:]\s*|\s+(?=['\"]))(?:'[^']*'|\"[^\"]*\"|[^\s,;)'\"]+)",
    re.IGNORECASE,
)
# A bracketed IPv6 literal, a bare one (eight groups, or with `::`; a time like
# 12:30:45 is neither), and an IPv4 address.
_IPV6_BRACKET_RE = re.compile(r"\[[0-9A-Fa-f:.]{2,45}\](?::\d{1,5})?")
_IPV6_FULL_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])")
_IPV6_SHORT_RE = re.compile(
    r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?::"
    r"(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?(?![\w:])"
)
_IPV4_RE = re.compile(_TOKEN_START + r"\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?(?!\w)")
# `name:port` for a dotted or a single-label name.
_HOST_PORT_RE = re.compile(
    _TOKEN_START
    + r"(?:[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*"
    + r"|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+):\d{2,5}(?!\w)"
)
# A host-name-shaped token: two or more labels, the last alphabetic and of two or more
# characters. `e.g.`, `p.m.`, `v1.2` and `a.b` are not; `a.example`, `notes.txt` and
# `node.site.example` are.
_FQDN_RE = re.compile(_TOKEN_START + r"(?:[A-Za-z0-9][A-Za-z0-9-]*\.)+[A-Za-z][A-Za-z-]+(?![\w-])")
_ENDPOINT_PATTERNS = (
    _URL_RE,
    _KEYWORD_RE,
    _IPV6_BRACKET_RE,
    _IPV6_FULL_RE,
    _IPV6_SHORT_RE,
    _HOST_PORT_RE,
    _IPV4_RE,
)
# A service's error body is a sentence or two. A longer one is cut, which also
# bounds the work done on it.
_MAX_REDACTED_CHARS = 8192


def redact_endpoints(text: str, config: OnedataConfig, *, host_names: bool = True) -> str:
    """Replace URLs, `host:port` pairs, IP addresses, host names and this service's own host.

    Applied to the structured JSON error bodies a service answers with, which are
    kept in an error message because an agent corrects itself from Onedata's own
    error id and description. The text is cut at `_MAX_REDACTED_CHARS`. With
    `host_names=False`, bare dotted names (a space called `lab.data`) are left
    alone, for a value the agent itself typed that the service echoes back.
    """
    if len(text) > _MAX_REDACTED_CHARS:
        text = text[:_MAX_REDACTED_CHARS] + "...(truncated)"
    patterns = _ENDPOINT_PATTERNS + ((_FQDN_RE,) if host_names else ())
    for pattern in patterns:
        text = pattern.sub("<address>", text)
    host = urlparse(config.base_url).hostname
    if host:
        text = re.sub(re.escape(host), "<address>", text, flags=re.IGNORECASE)
    return text


def http_failure_message(
    config: OnedataConfig,
    method: str,
    path: str,
    status: int,
    body: Any,
    *,
    label: str = "Onedata API request failed",
) -> str:
    """The message for a non-2xx answer: the status, and the body when it is safe to show.

    A structured (JSON) body is Onedata's own error and is kept, with endpoints
    redacted. Anything else (an HTML page from a gateway, plain text, bytes) is
    replaced by `http_<status>` and the service name, because such a page may
    name the upstream node.
    """
    if isinstance(body, dict | list):
        detail = redact_endpoints(_format_body_for_log(body), config)
    else:
        detail = f"http_{status} (service: {config.service})"
    return f"{label}: {method} {path} (status={status}) - {detail}"


def decode_response_body(response: httpx.Response) -> Any:
    """The response's JSON body for a JSON answer, else its text, else its bytes."""
    try:
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            return response.json()
        return response.text
    except ValueError:
        return response.content


async def _send(
    config: OnedataConfig,
    method: str,
    path: str,
    headers: dict[str, str],
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    body: bytes | None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        base_url=config.base_url, headers=headers, verify=config.verify_ssl
    ) as client:
        if json_body is not None:
            return await client.request(method, path, params=params, json=json_body)
        return await client.request(method, path, params=params, content=body)


def _mark_span_error(span: Any, description: str) -> None:
    """Set a REST span's status to ERROR with a host-free description."""
    if span is not None:
        span.set_status(telemetry.Status(telemetry.StatusCode.ERROR, description))


async def request(
    config: OnedataConfig,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    body: bytes | None = None,
    additional_headers: dict[str, str] | None = None,
) -> Any:
    method = method.upper()
    headers = dict(config.auth_headers)

    headers.update(additional_headers or {})

    # Outbound child span + W3C traceparent injection, so a harness-initiated
    # trace continues into the Onedata REST call. No-op when telemetry is off.
    if telemetry.telemetry_enabled():
        span_cm = telemetry.get_tracer().start_as_current_span(
            f"onedata.rest {method}",
            kind=telemetry.SpanKind.CLIENT,
        )
    else:
        span_cm = contextlib.nullcontext()

    # Both errors are built inside the span's lifetime and raised after the
    # block. Keeping construction inside the `with` lets us set the REST span's
    # ERROR status + onedata.* attrs before the span ends (#10), and keeps the
    # raw transport exception from escaping through the span, which would
    # record its text (the node's host and port) and stack trace on the trace.
    onedata_error: OnedataApiError | None = None
    transport_error: OnedataApiError | None = None
    response: httpx.Response | None = None
    t0 = time.perf_counter()
    try:
        with span_cm as span:
            if span is not None:
                span.set_attribute("http.request.method", method)
                span.set_attribute("url.path", path)
                telemetry.inject_traceparent(headers)
            try:
                response = await _send(config, method, path, headers, params, json_body, body)
            except Exception as exc:
                transport_error = transport_failure(exc, config, method, path)
                if span is not None:
                    span.set_attribute("error.type", transport_error.kind or "request_error")
                _mark_span_error(span, transport_error.kind or "request_error")

            if response is not None:
                response_body = decode_response_body(response)

                if span is not None:
                    span.set_attribute("http.response.status_code", response.status_code)

                # A non-2xx Onedata response is a REST-level failure: mark the span
                # ERROR here (not only on network exceptions), so Tempo queries
                # filtering onedata.rest spans by status.code==ERROR don't undercount.
                if response.is_error:
                    onedata_error = OnedataApiError(
                        http_failure_message(
                            config, method, path, response.status_code, response_body
                        ),
                        response={"status_code": response.status_code, "body": response_body},
                    )
                    if span is not None:
                        if onedata_error.error_id:
                            span.set_attribute("onedata.error_id", onedata_error.error_id)
                        if onedata_error.errno:
                            span.set_attribute("onedata.errno", onedata_error.errno)
                    _mark_span_error(span, f"HTTP {response.status_code}")
    except Exception as e:
        # Anything else that went wrong around the call: still host-free.
        telemetry.record_rest_request(method, "exception", (time.perf_counter() - t0) * 1000.0)
        raise transport_failure(e, config, method, path) from None

    if transport_error is not None:
        telemetry.record_rest_request(method, "exception", (time.perf_counter() - t0) * 1000.0)
        raise transport_error from None

    # Record REST duration for both success and non-2xx (status class from the
    # response); the network-exception path is recorded above.
    if response is not None:
        telemetry.record_rest_request(
            method,
            f"{response.status_code // 100}xx",
            (time.perf_counter() - t0) * 1000.0,
        )
    if onedata_error is not None:
        raise onedata_error

    logger.debug(
        "Onedata API request successful: %s %s\nStatus: %s\nHeaders: %s\nBody: %s",
        method,
        path,
        response.status_code,
        dict(response.headers),
        _format_body_for_log(response_body),
    )

    return {"status_code": response.status_code, "body": response_body}
