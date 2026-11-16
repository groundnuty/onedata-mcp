"""Tool answers carry no host, address or port, whatever the transport says.

A transport exception's text routinely names the node it could not reach
(``HTTPSConnectionPool(host=<FQDN>, port=8443)``, ``Hostname mismatch ...
'<FQDN>'``, ``for url 'https://<FQDN>:8443/...'``). FastMCP puts ``str(exc)``
into the tool's answer, so on an HPC federation an agent could read a node's
hostname and port, and a published trace would carry them too. These tests
pin the replacement: a stable message with the error's kind and the declared
service name, nothing else.
"""

from __future__ import annotations

import logging
import ssl
import traceback
from collections.abc import Callable

import httpx
import pytest
from fastmcp import Client, FastMCP
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pytest_httpx import HTTPXMock

from onedata_mcp import telemetry
from onedata_mcp.api import files
from onedata_mcp.config import OnedataConfig, get_oneprovider_config, get_onezone_config
from onedata_mcp.modules import spaces as spaces_module
from onedata_mcp.utils import (
    OnedataApiError,
    OnedataInvalidSpaceError,
    redact_endpoints,
    request,
    transport_error_kind,
)

NODE_FQDN = "node-17.hpc-site.example"
NODE_PORT = "8443"
NODE_IP = "10.20.30.40"
POOL_TEXT = (
    f"HTTPSConnectionPool(host='{NODE_FQDN}', port={NODE_PORT}): Max retries exceeded "
    f"with url: /api/v3/oneprovider/spaces (Caused by NewConnectionError: {NODE_IP})"
)
LEAKS = (NODE_FQDN, NODE_PORT, NODE_IP, "HTTPSConnectionPool", "Max retries", "NewConnection")


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token-z")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


def get_oneprovider_config_for(host_url: str) -> OnedataConfig:
    return OnedataConfig(
        base_url=f"{host_url}/api/v3/oneprovider",
        auth_headers={},
        verify_ssl=True,
        service="oneprovider",
    )


def _assert_host_free(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text, f"{leak!r} leaked into {text!r}"


def _tls_failure() -> httpx.ConnectError:
    """What httpx raises for a failed handshake: a ConnectError caused by ssl."""
    exc = httpx.ConnectError(
        f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, "
        f"certificate is not valid for '{NODE_FQDN}' (host={NODE_FQDN}, port={NODE_PORT})"
    )
    exc.__cause__ = ssl.SSLCertVerificationError(1, f"Hostname mismatch for {NODE_FQDN}")
    return exc


def _status_failure() -> httpx.HTTPStatusError:
    request_obj = httpx.Request("GET", f"https://{NODE_FQDN}:{NODE_PORT}/api/v3/oneprovider/spaces")
    return httpx.HTTPStatusError(
        f"Server error '503 Service Unavailable' for url 'https://{NODE_FQDN}:{NODE_PORT}/x'",
        request=request_obj,
        response=httpx.Response(503, request=request_obj),
    )


# (id, exception factory, expected kind): every kind the server maps, each with a text full of
# leaks. Factories, so no test shares an exception instance (and its traceback) with another.
KIND_CASES: list[tuple[str, Callable[[], BaseException], str]] = [
    ("connect", lambda: httpx.ConnectError(POOL_TEXT), "connection_error"),
    ("read", lambda: httpx.ReadError(POOL_TEXT), "connection_error"),
    ("os", lambda: OSError(POOL_TEXT), "connection_error"),
    ("unsupported-protocol", lambda: httpx.UnsupportedProtocol(POOL_TEXT), "connection_error"),
    ("connect-timeout", lambda: httpx.ConnectTimeout(POOL_TEXT), "timeout"),
    ("read-timeout", lambda: httpx.ReadTimeout(POOL_TEXT), "timeout"),
    ("builtin-timeout", lambda: TimeoutError(POOL_TEXT), "timeout"),
    ("tls", _tls_failure, "tls_error"),
    ("http-status", _status_failure, "http_503"),
    ("other", lambda: RuntimeError(POOL_TEXT), "request_error"),
]


@pytest.mark.parametrize(("name", "make", "kind"), KIND_CASES, ids=[c[0] for c in KIND_CASES])
def test_transport_error_kind_maps_each_error_class(
    name: str, make: Callable[[], BaseException], kind: str
) -> None:
    assert transport_error_kind(make()) == kind


def test_transport_error_kind_follows_the_cause_chain() -> None:
    """httpx wraps the ssl error: the kind comes from the cause, not the wrapper."""
    wrapped = httpx.ConnectError("generic text")
    wrapped.__cause__ = ssl.SSLError("handshake failure")
    assert transport_error_kind(wrapped) == "tls_error"
    # A timeout wins over a connection error further down the chain.
    wrapped = httpx.ConnectError("generic text")
    wrapped.__cause__ = TimeoutError("slow")
    assert transport_error_kind(wrapped) == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "make", "kind"), KIND_CASES, ids=[c[0] for c in KIND_CASES])
async def test_request_answer_holds_kind_and_service_but_no_host(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    name: str,
    make: Callable[[], BaseException],
    kind: str,
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_exception(make())

    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "GET", "/spaces")

    message = str(excinfo.value)
    _assert_host_free(message)
    assert message == f"Onedata API request failed: GET /spaces - {kind} (service: oneprovider)"
    assert excinfo.value.kind == kind
    assert excinfo.value.service == "oneprovider"
    assert excinfo.value.response is None


@pytest.mark.asyncio
async def test_zone_calls_are_named_onezone(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_exception(httpx.ConnectTimeout(POOL_TEXT))

    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_onezone_config(), "GET", "/spaces/sp1")

    assert str(excinfo.value) == (
        "Onedata API request failed: GET /spaces/sp1 - timeout (service: onezone)"
    )


@pytest.mark.asyncio
async def test_the_original_exception_is_not_chained_to_the_answer(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A traceback or span formatted from the raised error must not carry the raw text."""
    _set_env(monkeypatch)
    httpx_mock.add_exception(httpx.ConnectError(POOL_TEXT))

    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "GET", "/spaces")

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    _assert_host_free("".join(traceback.format_exception(excinfo.value)))


@pytest.mark.asyncio
async def test_rest_span_holds_the_kind_but_not_the_raw_text(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The outbound span would otherwise record the raw exception text and stack trace."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(telemetry, "get_tracer", lambda: tracer)
    monkeypatch.setattr(telemetry, "_telemetry_enabled", True)
    _set_env(monkeypatch)
    httpx_mock.add_exception(httpx.ConnectError(POOL_TEXT))

    with pytest.raises(OnedataApiError):
        await request(get_oneprovider_config(), "GET", "/spaces")

    spans = [s for s in exporter.get_finished_spans() if s.name == "onedata.rest GET"]
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == telemetry.StatusCode.ERROR
    assert span.status.description == "connection_error"
    rendered = repr(span.to_json())
    _assert_host_free(rendered)
    assert "connection_error" in rendered


@pytest.mark.asyncio
async def test_full_text_goes_to_the_servers_own_debug_log_only(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_exception(httpx.ConnectError(POOL_TEXT))
    httpx_mock.add_exception(httpx.ConnectError(POOL_TEXT))

    with caplog.at_level(logging.INFO, logger="onedata_mcp.utils"), pytest.raises(OnedataApiError):
        await request(get_oneprovider_config(), "GET", "/spaces")
    assert NODE_FQDN not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="onedata_mcp.utils"), pytest.raises(OnedataApiError):
        await request(get_oneprovider_config(), "GET", "/spaces")
    assert NODE_FQDN in caplog.text


@pytest.mark.asyncio
async def test_a_tool_answer_holds_neither_host_nor_port(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Through FastMCP, as the agent sees it."""
    _set_env(monkeypatch)
    httpx_mock.add_exception(httpx.ConnectError(POOL_TEXT))
    mcp = FastMCP(name="onedata-mcp-test")
    spaces_module.register_module(mcp)

    async with Client(mcp) as client:
        result = await client.call_tool("list_user_spaces", {}, raise_on_error=False)

    assert result.is_error is True
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    _assert_host_free(text)
    assert "connection_error" in text
    assert "oneprovider" in text


@pytest.mark.asyncio
async def test_a_tool_answer_for_a_zone_call_names_the_zone(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_exception(_tls_failure())
    mcp = FastMCP(name="onedata-mcp-test")
    spaces_module.register_module(mcp)

    async with Client(mcp) as client:
        result = await client.call_tool("list_marketplace_spaces", {}, raise_on_error=False)

    assert result.is_error is True
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    _assert_host_free(text)
    assert "tls_error" in text
    assert "onezone" in text


# ---------------------------------------------------------------------------
# The other places an HTTP failure reaches an answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_service_error_page_naming_a_node_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A gateway's HTML error page names its upstream; the answer keeps only the status."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        status_code=502,
        headers={"Content-Type": "text/html"},
        text=f"<h1>Bad Gateway</h1> upstream {NODE_FQDN} port {NODE_PORT} at {NODE_IP}",
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "GET", "/spaces")

    _assert_host_free(str(excinfo.value))
    assert str(excinfo.value) == (
        "Onedata API request failed: GET /spaces (status=502) - http_502 (service: oneprovider)"
    )
    assert excinfo.value.response == {"status_code": 502, "body": excinfo.value.response["body"]}


@pytest.mark.asyncio
async def test_a_structured_onedata_error_is_kept_with_endpoints_redacted(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Agents self-correct from Onedata's own error id and description; only endpoints go."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/qos_requirements",
        status_code=400,
        json={
            "error": {
                "id": "badQosExpression",
                "description": (
                    "Invalid QoS expression near '==': use '=' instead. "
                    f"Contacted https://{NODE_FQDN}:{NODE_PORT}/qos and {NODE_IP}:{NODE_PORT} "
                    "and provider.example"
                ),
            }
        },
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "POST", "/qos_requirements", json_body={})

    message = str(excinfo.value)
    assert "badQosExpression" in message
    assert "Invalid QoS expression near '==': use '=' instead." in message
    _assert_host_free(message)
    assert "provider.example" not in message
    assert excinfo.value.error_id == "badQosExpression"


@pytest.mark.parametrize(
    "leaky",
    [
        "HTTPSConnectionPool(host='node-99.other-site.example', port=8443): Max retries exceeded",
        "unreachable 2001:db8::17 and fe80::1 and [2001:db8::1]:8443",
        "unreachable 2001:db8:0:0:0:0:0:17",
        "contacted node17:8443 and node-99.other-site.example:9443/path",
        "contacted http://node-99.other-site.example/x and 192.0.2.7",
    ],
)
@pytest.mark.asyncio
async def test_endpoint_shapes_in_a_service_description_are_redacted(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, leaky: str
) -> None:
    """A node other than the configured one, in the shapes a transport or a service prints."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        status_code=500,
        json={"error": {"id": "internalServerError", "description": leaky}},
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "GET", "/spaces")

    message = str(excinfo.value)
    for fragment in ("99.other-site", "node17", "2001:db8", "fe80", "192.0.2.7", "8443", "9443"):
        assert fragment not in message, f"{fragment!r} leaked into {message!r}"
    assert "internalServerError" in message


def test_redaction_leaves_times_versions_and_prose_alone() -> None:
    text = (
        "at 12:30:45 on 2026-11-16T12:30:45Z ratio 3:10, std::string, v1.2.3-beta, "
        "i.e. p.m. e.g. a.b, "
        "Invalid QoS expression near '==': use '=' instead (e.g. country=PL)."
    )
    assert redact_endpoints(text, get_oneprovider_config_for("https://provider.example")) == text


@pytest.mark.parametrize(
    "leaky",
    [
        "hostname 'node-99.other-site.example' doesn't match 'cert.other-site.example'",
        "hostname 'node-99.other-site.example' doesn't match 'a.other-site'",
        "hostname 'node-99.other-site.example' doesn't match 'x.example'",
        "hostname 'node99' doesn't match",
        "gave up on node-99.other-site.example.",
        "gave up on 192.0.2.7.",
    ],
)
def test_redaction_masks_host_names_and_addresses_with_sentence_punctuation(leaky: str) -> None:
    redacted = redact_endpoints(leaky, get_oneprovider_config_for("https://provider.example"))
    for fragment in ("99", "other-site", "192.0.2.7", "x.example", "a.example"):
        assert fragment not in redacted, f"{fragment!r} survived in {redacted!r}"


def test_redaction_keeps_a_dotted_name_the_agent_typed_when_asked_to() -> None:
    config = get_oneprovider_config_for("https://provider.example")
    assert redact_endpoints("lab.data", config, host_names=False) == "lab.data"
    assert redact_endpoints("lab.data", config) == "<address>"
    # URLs and addresses go either way.
    assert redact_endpoints("https://n.example:8443/x", config, host_names=False) == "<address>"


@pytest.mark.parametrize("unit", ["a.", "ab.", "1.", "ab-", "1:", "1.1."])
def test_redaction_cost_stays_linear_on_hostile_input(unit: str) -> None:
    """Adversarial `a.a.a. ...` bodies must not cost a rescan from every label."""
    import time

    config = get_oneprovider_config_for("https://provider.example")
    text = unit * 200_000
    started = time.perf_counter()
    redacted = redact_endpoints(text, config)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0
    assert redacted.endswith("...(truncated)")
    assert len(redacted) < 20_000  # the input was cut at 8192 characters before the work


@pytest.mark.asyncio
async def test_an_error_id_or_errno_that_is_not_an_identifier_is_not_passed_on(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Both go into span attributes, so a URL in either must not."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(telemetry, "get_tracer", lambda: tracer)
    monkeypatch.setattr(telemetry, "_telemetry_enabled", True)
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        status_code=500,
        json={
            "error": {
                "id": f"https://{NODE_FQDN}:{NODE_PORT}/failure",
                "details": {"errno": f"{NODE_FQDN}:{NODE_PORT}"},
            }
        },
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "GET", "/spaces")

    assert excinfo.value.error_id is None
    assert excinfo.value.errno is None
    span = next(s for s in exporter.get_finished_spans() if s.name == "onedata.rest GET")
    _assert_host_free(repr(span.to_json()))
    assert "onedata.error_id" not in span.attributes
    assert "onedata.errno" not in span.attributes


@pytest.mark.asyncio
async def test_an_invalid_space_error_redacts_the_id_the_service_echoes(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        json=[],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/dsadas/children",
        status_code=400,
        json={
            "error": {
                "id": "spaceNotSupportedBy",
                "details": {"spaceId": f"https://{NODE_FQDN}:{NODE_PORT}/x", "providerId": "p1"},
            }
        },
    )

    with pytest.raises(OnedataInvalidSpaceError) as excinfo:
        await files.list_children("dsadas", limit=10, offset=0)

    _assert_host_free(str(excinfo.value))
    assert str(excinfo.value).startswith('Space "<address>" does not exist.')


@pytest.mark.asyncio
async def test_a_structured_error_without_endpoints_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    body = {"error": {"id": "posix", "details": {"errno": "enoent"}}}
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/f1",
        status_code=404,
        json=body,
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "GET", "/data/f1")

    import json

    assert str(excinfo.value) == (
        "Onedata API request failed: GET /data/f1 (status=404) - " + json.dumps(body, indent=2)
    )
    assert excinfo.value.errno == "enoent"


@pytest.mark.asyncio
async def test_a_successful_answer_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        json=[{"name": "s", "spaceId": "sp1"}],
    )

    answer = await request(get_oneprovider_config(), "GET", "/spaces")

    assert answer == {"status_code": 200, "body": [{"name": "s", "spaceId": "sp1"}]}


@pytest.mark.asyncio
async def test_download_transport_error_is_host_free(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The download path calls httpx directly, so it needs the same wrapping."""
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid1",
        json={"type": "REG", "size": 3},
    )
    httpx_mock.add_exception(
        httpx.ConnectError(POOL_TEXT),
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid1/content",
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await files.download_file_with_meta("fid1")

    assert str(excinfo.value) == (
        "Onedata API request failed: GET /data/fid1/content - connection_error "
        "(service: oneprovider)"
    )


@pytest.mark.asyncio
async def test_download_status_error_is_host_free(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid1",
        json={"type": "REG", "size": 3},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/fid1/content",
        status_code=502,
        text=f"upstream {NODE_FQDN}:{NODE_PORT}",
    )

    with pytest.raises(RuntimeError) as excinfo:
        await files.download_file_with_meta("fid1")

    _assert_host_free(str(excinfo.value))
    assert str(excinfo.value) == (
        "Onedata API request failed: GET /data/fid1/content (status=502) - http_502 "
        "(service: oneprovider)"
    )


@pytest.mark.asyncio
async def test_move_transport_error_is_host_free(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The CDMI move names its URL in the message today; it must name only the path."""
    _set_env(monkeypatch)
    httpx_mock.add_exception(
        httpx.ConnectError(POOL_TEXT),
        method="PUT",
        url="https://provider.example/cdmi/myspace/new.txt",
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await files.move_file("/myspace/old.txt", "/myspace/new.txt")

    message = str(excinfo.value)
    _assert_host_free(message)
    assert "provider.example" not in message
    assert message == (
        "CDMI move failed: PUT /cdmi/myspace/new.txt - connection_error (service: oneprovider)"
    )


@pytest.mark.asyncio
async def test_move_status_error_names_the_path_not_the_url(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    httpx_mock.add_response(
        method="PUT",
        url="https://provider.example/cdmi/myspace/new.txt",
        status_code=500,
        text=f"oneprovider boom on {NODE_FQDN}:{NODE_PORT}",
    )

    with pytest.raises(OnedataApiError) as excinfo:
        await files.move_file("/myspace/old.txt", "/myspace/new.txt")

    message = str(excinfo.value)
    _assert_host_free(message)
    assert "provider.example" not in message
    assert message == (
        "CDMI move failed: PUT /cdmi/myspace/new.txt (status=500) - http_500 (service: oneprovider)"
    )
