from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
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
        f"{label}: {method} {shorten_path(path)} - {kind} (service: {config.service})",
        response=None,
        kind=kind,
        service=config.service,
    )


# What the structured JSON body of a non-2xx answer may not show. Onedata's own error
# ids and descriptions reach the agent unchanged, except for these rules, all of which hold
# with or without a node list unless said otherwise:
#   1. a node's host name or address (with a port that follows it) is replaced by its
#      DECLARED name, so the text an agent sees is the same on every deployment of the same
#      layout. The nodes are the two hosts this server is configured with (see
#      `known_node_names`; declared as `onezone` and `oneprovider`) and, when the deploy
#      gives a node list (ONEDATA_MCP_NODE_NAMES, the fallback mode for a federation whose
#      real names must stay hidden), the list's nodes. A list entry for a host wins;
#   2. a URL is masked;
#   3. a `host=` or `port=` pair is masked, ONLY when a node list is given;
#   4. the value of a domain-bearing field of the error (`serviceDomain`, which every 25.0
#      error carries in its `ctx`, and `domain`, `host` and the like) reads `a provider`: it
#      names the service that answered, which may be a peer provider that is not configured;
#   5. a bare host name inside a text (a dotted name whose last label is a top-level
#      domain, such as `peer.site.example.com:8443`) is masked. A file name (`notes.txt`,
#      `data.v2.csv`), a version, an address, a QoS expression or an attribute name is not
#      one and passes untouched.
# Rules 1 and 2 both name no configured host; 4 and 5 name no other host either. Rules 1
# to 3 run in this order, so a URL or a pair naming a known node reads `<address>`.
# Anything else, a dotted file name or an unlisted IP address included, passes untouched.
# Transport errors and non-JSON bodies are this server's own fixed host-free texts and are
# the same in every mode.
_MASK = "<address>"
# A URL starts at a token start; the scan of a hostile `a.a.a. ...` body stays linear.
# Trailing sentence punctuation stays outside the URL.
_URL_RE = re.compile(r"(?<![\w.+-])[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>)\]\\]*[^\s'\"<>)\]\\.,;:!?]")
# `host='x'`, `hostname=x`, `port=8443` (a connection pool's repr).
_KEYWORD_RE = re.compile(
    r"\b(?:host|hostname|port)\s*=\s*"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;)\]}'\"]*[^\s,;)\]}'\".:!?])",
    re.IGNORECASE,
)
# The keys, in a serialized JSON error, whose string value is a domain (rule 4). The 25.0
# error format has one, `ctx.serviceDomain`; the others are the names Onedata's API uses for a
# host elsewhere, since an error's `details` is specific to each error type.
_DOMAIN_KEYS = ("serviceDomain", "domain", "domainName", "subdomain", "host", "hostname", "fqdn")
_DOMAIN_FIELD_RE = re.compile(
    r'("(?:' + "|".join(_DOMAIN_KEYS) + r')"\s*:\s*)"(?:[^"\\]|\\.)*"', re.IGNORECASE
)
_DOMAIN_VALUE = "a provider"
# A bare host name (rule 5): two or more labels and an alphabetic last label, not preceded by
# a name character, a dot, a hyphen, a slash, a backslash or `~` (so a path component, or the
# tail of a longer name, is not one), with an optional port, and not followed by a name
# character or `.` and one. A token start is the only place a match can begin, so the scan
# of a hostile `a.a.a. ...` body stays linear.
_BARE_HOST_RE = re.compile(
    r"(?<![\w.\-/\\~])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+)([A-Za-z]{2,24})"
    r"(?::\d{1,5})?(?![\w-]|\.\w)"
)
# What counts as a top-level domain for rule 5: the generic ones, the names a private network
# uses (`local`, `internal`, `test`, `example`, ...) and the country codes. A last label that
# is none of these is not a host name, which keeps file names out: `notes.txt`, `data.csv`,
# `archive.tar.gz`.
_TLD_WORDS = (
    "com org net edu gov mil int info biz name pro io dev app cloud tech online site xyz "
    "asia eu local localhost internal lan home corp intranet test example invalid arpa "
    "ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo "
    "br bs bt bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm "
    "do dz ec ee eg er es et fi fj fk fm fo fr ga gd ge gf gg gh gi gl gm gn gp gq gr gs gt "
    "gu gw gy hk hm hn hr ht hu id ie il im in iq ir is it je jm jo jp ke kg kh ki km kn kp "
    "kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp mq mr "
    "ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm "
    "pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sk sl sm sn so sr ss st su "
    "sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug uk us uy uz va vc ve "
    "vg vi vn vu wf ws ye yt za zm zw"
)
_TLDS = frozenset(_TLD_WORDS.split())
# Top-level domains that are also common file extensions or words: a name of two labels ending
# in one (`script.py`, `README.md`, `data.in`) is not masked; one of three or more is.
_AMBIGUOUS_WORDS = "py sh md rs pl pm ps cc so ai in id app"
_AMBIGUOUS_TLDS = frozenset(_AMBIGUOUS_WORDS.split())


@functools.lru_cache(maxsize=8)
def _node_name_regex(hosts: tuple[str, ...]) -> re.Pattern[str]:
    """One pattern for the given hosts: the longest first, whole names only.

    A host may be followed by a port, which goes with it (`[v6]:8443` for an IPv6
    address). `node1` does not match inside `node10` or `node1.other.example`.
    """
    ordered = sorted(hosts, key=len, reverse=True)
    plain = "|".join(re.escape(h) for h in ordered if ":" not in h)
    bare6 = "|".join(re.escape(h) for h in ordered if ":" in h)
    every = "|".join(re.escape(h) for h in ordered)
    named = []
    if plain:
        named.append(rf"(?P<plain>{plain})(?::\d{{1,5}})?")
    named.append(rf"\[(?P<bracket>{every})\](?::\d{{1,5}})?")
    alternatives = [r"(?<![\w.-])(?:" + "|".join(named) + r")(?![\w-]|\.\w)"]
    if bare6:
        # A bare IPv6 address may follow a prose colon (`peer:2001:db8::1`); one that is
        # followed by `:<hex>` is read as a longer address and left alone.
        alternatives.append(rf"(?<![\w.-])(?P<bare6>{bare6})(?![\w-]|\.\w|:[0-9A-Fa-f])")
    return re.compile("|".join(alternatives), re.IGNORECASE)


def replace_node_names(text: str, node_names: tuple[tuple[str, str], ...]) -> str:
    """Replace each known node's name or address in `text` by its declared name."""
    if not node_names:
        return text
    declared = dict(node_names)
    pattern = _node_name_regex(tuple(declared))

    def _declared_name(match: re.Match[str]) -> str:
        return declared[match.group(match.lastgroup or "plain").lower()]

    return pattern.sub(_declared_name, text)


def _host_of(url: Any) -> str | None:
    """The lower-cased host name or address of a configured URL; None for anything unusable."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        host = urlparse(url.strip()).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def known_node_names(config: OnedataConfig) -> tuple[tuple[str, str], ...]:
    """The `(host, declared name)` pairs an error text may not show a host of.

    The hosts this server is configured with, whichever service the failed call was for
    (ONEDATA_ONEZONE_HOST as `onezone`, ONEDATA_ONEPROVIDER_HOST as `oneprovider`, and the
    call's own base URL as its `config.service`), and then the deploy's node list, whose entry
    for the same host wins. Never raises: an unset or malformed host is left out.
    """
    merged: dict[str, str] = {}
    for url, name in (
        (os.getenv("ONEDATA_ONEZONE_HOST"), "onezone"),
        (os.getenv("ONEDATA_ONEPROVIDER_HOST"), "oneprovider"),
        (config.base_url, config.service),
    ):
        host = _host_of(url)
        if host:
            merged[host] = name
    merged.update(dict(config.node_names))
    return tuple(sorted(merged.items()))


def _mask_bare_host(match: re.Match[str]) -> str:
    last = match.group(2).lower()
    labels = match.group(1).count(".") + 1
    if last in _TLDS and (labels >= 3 or last not in _AMBIGUOUS_TLDS):
        return _MASK
    return match.group(0)


def _redact(text: str, names: tuple[tuple[str, str], ...], with_pairs: bool) -> str:
    text = replace_node_names(text, names)
    text = _URL_RE.sub(_MASK, text)
    if with_pairs:
        text = _KEYWORD_RE.sub(_MASK, text)
    return _BARE_HOST_RE.sub(_mask_bare_host, text)


def redact_endpoints(text: str, config: OnedataConfig) -> str:
    """Apply the rules above to a service's error text; every other byte is kept.

    A configured host is replaced and a URL masked in every mode; the `host=`/`port=` pairs
    are masked only when the deploy gives a node list (`config.node_names`).
    """
    return _redact(text, known_node_names(config), bool(config.node_names))


_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def redact_json_text(text: str, config: OnedataConfig) -> str:
    """`redact_endpoints` applied to each string of serialized JSON, as decoded.

    On the serialized text an escape such as `\\n` or `\\"` would sit against a name
    and break its boundary (`\\nnode17`, `host=\\"x\\"`). A string no rule touches is
    kept byte for byte. The value of a domain-bearing field (`serviceDomain`, `domain`, ...) is
    replaced first, whatever it holds.
    """
    names = known_node_names(config)
    with_pairs = bool(config.node_names)
    text = _DOMAIN_FIELD_RE.sub(lambda m: f'{m.group(1)}"{_DOMAIN_VALUE}"', text)

    def _one(match: re.Match[str]) -> str:
        literal = match.group(0)
        try:
            decoded = json.loads(literal)
        except ValueError:  # not JSON (the repr fallback): redact the literal as it is
            return _redact(literal, names, with_pairs)
        redacted = _redact(decoded, names, with_pairs)
        return literal if redacted == decoded else json.dumps(redacted)

    return _JSON_STRING_RE.sub(_one, text)


# A tool's error text is read by a model, so it is bounded: Onedata's own message is cut
# at the limit, after the redaction (a cut before it could leave half a host name that no
# rule matches), and a path segment (typically an id) is echoed at no more than a
# limited length.
MAX_ERROR_CHARS = 2000
_PATH_SEGMENT_MAX = 128


def bound_message(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """`text` cut to `limit` characters, `...` marking the cut. Redact before calling this."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def shorten_path(path: str) -> str:
    """The request path with each overlong segment (an id) cut short, `...` marking the cut."""
    return "/".join(
        seg if len(seg) <= _PATH_SEGMENT_MAX else seg[:_PATH_SEGMENT_MAX] + "..."
        for seg in path.split("/")
    )


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

    A structured (JSON) body is Onedata's own error and reaches the agent with the rules above
    applied to it by `redact_json_text` (a domain-bearing field's value included). Anything
    else (an HTML page from a gateway, plain text, bytes) is replaced by `http_<status>` and
    the service name, because such a page may name the upstream node. The path's overlong
    segments are cut short and the whole message is bounded (`MAX_ERROR_CHARS`).
    """
    if isinstance(body, dict | list):
        detail = redact_json_text(_format_body_for_log(body), config)
    else:
        detail = f"http_{status} (service: {config.service})"
    return bound_message(f"{label}: {method} {shorten_path(path)} (status={status}) - {detail}")


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
                            span.set_attribute(
                                "onedata.error_id", redact_endpoints(onedata_error.error_id, config)
                            )
                        if onedata_error.errno:
                            span.set_attribute(
                                "onedata.errno", redact_endpoints(onedata_error.errno, config)
                            )
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
