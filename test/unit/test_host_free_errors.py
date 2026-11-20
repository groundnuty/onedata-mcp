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

import json
import logging
import ssl
import time
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
from onedata_mcp.config import (
    OnedataConfig,
    get_node_names,
    get_oneprovider_config,
    get_onezone_config,
)
from onedata_mcp.modules import spaces as spaces_module
from onedata_mcp.utils import (
    MAX_ERROR_CHARS,
    OnedataApiError,
    OnedataInvalidSpaceError,
    http_failure_message,
    redact_endpoints,
    redact_json_text,
    replace_node_names,
    request,
    shorten_path,
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


def get_oneprovider_config_for(
    host_url: str, node_names: tuple[tuple[str, str], ...] = ()
) -> OnedataConfig:
    return OnedataConfig(
        base_url=f"{host_url}/api/v3/oneprovider",
        auth_headers={},
        verify_ssl=True,
        service="oneprovider",
        node_names=node_names,
    )


# The deploy's list for the tests: real name (with or without a port) -> declared name.
ZONE_FQDN = "zone-host.hpc-site.example"
NODE_IPV6 = "2001:db8::17"
NODE_NAMES = {
    f"{NODE_FQDN}:{NODE_PORT}": "oneprovider-a",
    NODE_IP: "oneprovider-a",
    ZONE_FQDN: "onezone",
    NODE_IPV6: "oneprovider-b",
}


def _set_node_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", json.dumps(NODE_NAMES))


@pytest.fixture(autouse=True)
def _no_ambient_node_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEDATA_MCP_NODE_NAMES", raising=False)


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
    # list_marketplace_spaces is the zone-side tool; it is registered only with the preview flag.
    spaces_module.register_module(mcp, experimental=True)

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


def _qos_error(description: str) -> dict:
    return {"error": {"id": "badQosExpression", "description": description}}


def _expected_message(body: dict) -> str:
    return "Onedata API request failed: POST /qos_requirements (status=400) - " + json.dumps(
        body, indent=2
    )


async def _post_qos_error(httpx_mock: HTTPXMock, body: dict) -> OnedataApiError:
    httpx_mock.add_response(
        method="POST",
        url="https://provider.example/api/v3/oneprovider/qos_requirements",
        status_code=400,
        json=body,
    )
    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "POST", "/qos_requirements", json_body={})
    return excinfo.value


@pytest.mark.asyncio
async def test_a_description_naming_a_known_node_shows_its_declared_name(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The deploy's list maps a node's name, with the port that follows it, to its declared name."""
    _set_env(monkeypatch)
    _set_node_names(monkeypatch)
    error = await _post_qos_error(
        httpx_mock,
        _qos_error(
            f"Invalid QoS expression near '==': use '=' instead. Provider {NODE_FQDN}:{NODE_PORT} "
            f"could not reach {NODE_IP} or {ZONE_FQDN}, nor [{NODE_IPV6}]:{NODE_PORT}."
        ),
    )

    assert str(error) == _expected_message(
        _qos_error(
            "Invalid QoS expression near '==': use '=' instead. Provider oneprovider-a "
            "could not reach oneprovider-a or onezone, nor oneprovider-b."
        )
    )
    for leak in (NODE_FQDN, NODE_PORT, NODE_IP, ZONE_FQDN, NODE_IPV6):
        assert leak not in str(error)
    assert error.error_id == "badQosExpression"


@pytest.mark.asyncio
async def test_with_a_node_list_a_url_and_a_pair_are_masked(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The fallback mode: URLs and `host=` / `port=` pairs read `<address>`, listed node or not."""
    _set_env(monkeypatch)
    text = (
        f"Contacted https://{NODE_FQDN}:{NODE_PORT}/qos, host='{NODE_FQDN}', port={NODE_PORT}, "
        "hostname=other.example and http://unlisted.example/x"
    )
    answer = await _redacted_answer(monkeypatch, httpx_mock, text, node_names=True)

    assert answer == _expected_message(
        _qos_error("Contacted <address>, <address>, <address>, <address> and <address>")
    )


@pytest.mark.asyncio
async def test_without_a_node_list_a_url_and_a_host_name_are_masked_and_a_port_and_an_ip_pass(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """No list: a URL and a bare host name are masked, a configured host is replaced. A `port=`
    pair and an IP address, which name no host by themselves, reach the caller as written."""
    _set_env(monkeypatch)
    text = (
        f"Contacted https://{NODE_FQDN}:{NODE_PORT}/qos, host='{NODE_FQDN}', port={NODE_PORT}, "
        f"hostname=other.example, http://unlisted.example/x and the node {NODE_FQDN}:{NODE_PORT} "
        f"at {NODE_IP}"
    )
    answer = await _redacted_answer(monkeypatch, httpx_mock, text, node_names=False)

    assert answer == _expected_message(
        _qos_error(
            f"Contacted <address>, host='<address>', port={NODE_PORT}, hostname=<address>, "
            f"<address> and the node <address> at {NODE_IP}"
        )
    )
    for part in (NODE_FQDN, "other.example", "unlisted.example", "http://", "https://"):
        assert part not in answer
    assert f"port={NODE_PORT}" in answer and NODE_IP in answer


@pytest.mark.asyncio
@pytest.mark.parametrize("node_names", [False, True], ids=["unset", "listed"])
async def test_a_configured_host_never_reaches_the_caller_with_or_without_a_node_list(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, node_names: bool
) -> None:
    """The Onezone and the Oneprovider this server is configured with read as their declared
    service names in every mode, whichever service the call was for; whole names only. A longer
    name that merely contains one is another host name, so it is masked, not renamed."""
    _set_env(monkeypatch)
    text = (
        "Provider provider.example:8443 and PROVIDER.EXAMPLE, zone onezone.example; not "
        "provider.example.other.test, myprovider.example or provider.examples"
    )
    answer = await _redacted_answer(monkeypatch, httpx_mock, text, node_names=node_names)

    assert answer == _expected_message(
        _qos_error(
            "Provider oneprovider and oneprovider, zone onezone; not "
            "<address>, <address> or provider.examples"
        )
    )


@pytest.mark.asyncio
async def test_a_node_list_entry_for_a_configured_host_wins(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", json.dumps({"provider.example": "site-a"}))
    error = await _post_qos_error(httpx_mock, _qos_error("down: provider.example"))

    assert str(error) == _expected_message(_qos_error("down: site-a"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "not a url", "http://[::1", "provider.example:8443"])
async def test_an_unset_or_malformed_configured_host_never_hides_the_error(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, bad: str
) -> None:
    """The redaction reads the zone's host too; whatever it holds, the real error is raised."""
    _set_env(monkeypatch)
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", bad)
    error = await _post_qos_error(httpx_mock, _qos_error("Invalid near '=='"))

    assert str(error) == _expected_message(_qos_error("Invalid near '=='"))


@pytest.mark.asyncio
async def test_a_long_path_segment_and_a_long_body_are_bounded_after_the_redaction(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """An overlong id is echoed cut short, and the whole message stops at MAX_ERROR_CHARS. A
    host name that would straddle the cut is gone before it (the cut comes second)."""
    _set_env(monkeypatch)
    long_id = "b" * 500
    httpx_mock.add_response(
        method="DELETE",
        url=f"https://provider.example/api/v3/oneprovider/things/{long_id}",
        status_code=400,
        json=_qos_error("x" * 1980 + " provider.example" + "y" * 3000),
    )
    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "DELETE", f"/things/{long_id}")

    text = str(excinfo.value)
    assert len(text) == MAX_ERROR_CHARS and text.endswith("...")
    assert long_id not in text and "b" * 129 not in text


def test_no_part_of_a_host_survives_wherever_the_cut_falls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host is replaced before the message is cut, so a cut cannot leave half of it."""
    _set_env(monkeypatch)
    config = get_oneprovider_config_for("https://provider.example")
    for offset in range(MAX_ERROR_CHARS - 140, MAX_ERROR_CHARS + 10):
        body = _qos_error("x" * offset + " provider.example " + "y" * 50)
        text = http_failure_message(config, "POST", "/qos_requirements", 400, body)
        assert len(text) <= MAX_ERROR_CHARS
        assert "r.example" not in text and "ider.exa" not in text, offset


def test_shorten_path_cuts_only_the_overlong_segments() -> None:
    assert shorten_path("/a/b%2Fc/d") == "/a/b%2Fc/d"
    assert shorten_path(f"/qos_requirements/{'z' * 200}") == f"/qos_requirements/{'z' * 128}..."


@pytest.mark.asyncio
async def test_a_transport_error_message_shortens_a_long_id_too(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    long_id = "c" * 500
    httpx_mock.add_exception(httpx.ConnectError(POOL_TEXT))
    with pytest.raises(OnedataApiError) as excinfo:
        await request(get_oneprovider_config(), "DELETE", f"/qos_requirements/{long_id}")

    text = str(excinfo.value)
    assert long_id not in text and "c" * 129 not in text
    _assert_host_free(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "  ", "{}"])
async def test_an_unset_empty_or_blank_node_list_is_the_no_list_mode(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, value: str | None
) -> None:
    _set_env(monkeypatch)
    if value is None:
        monkeypatch.delenv("ONEDATA_MCP_NODE_NAMES", raising=False)
    else:
        monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", value)
    text = f"see https://{NODE_FQDN}:{NODE_PORT}/x host={NODE_FQDN} port={NODE_PORT} {NODE_IP}"
    error = await _post_qos_error(httpx_mock, _qos_error(text))

    # the URL and the host name are masked; a port pair and an IP address pass
    assert str(error) == _expected_message(
        _qos_error(f"see <address> host=<address> port={NODE_PORT} {NODE_IP}")
    )


@pytest.mark.asyncio
async def test_a_dotted_file_name_a_version_and_an_ip_pass_untouched(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    _set_node_names(monkeypatch)
    text = (
        "No such file /space/notes.txt or data.v2.csv (e.g. a.b), archive.tar.gz, script.py, "
        "README.md, /space/sub.dom.pl/x, at 12:30:45 on 2026-11-16T12:30:45Z, v1.2.3-beta, "
        "std::string, 192.0.2.7, "
        f"{NODE_FQDN}0 and {NODE_IP}9 are not host names"
    )
    error = await _post_qos_error(httpx_mock, _qos_error(text))

    assert str(error) == _expected_message(_qos_error(text))


@pytest.mark.asyncio
@pytest.mark.parametrize("node_names", [False, True], ids=["unset", "listed"])
async def test_an_unlisted_bare_host_name_is_masked_wherever_it_stands(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, node_names: bool
) -> None:
    text = (
        "Cannot reach other-node.site.example, x" + NODE_FQDN + ":" + NODE_PORT + " or "
        "peer.site-b.pl. Mail ops@peer.example.com; at node7.example.com."
    )
    _set_env(monkeypatch)
    answer = await _redacted_answer(monkeypatch, httpx_mock, text, node_names=node_names)

    assert answer == _expected_message(
        _qos_error(
            "Cannot reach <address>, <address> or <address>. Mail ops@<address>; at <address>."
        )
    )


@pytest.mark.asyncio
async def test_without_a_node_list_a_body_is_onedatas_text_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Compose: no list, and no URL, host name or configured host in the text, so Onedata's own
    text is byte-identical, addresses and `host=` words included."""
    _set_env(monkeypatch)
    plain = _qos_error(
        f"Invalid near '==' for {NODE_IP} {NODE_IPV6} lab.data notes.txt "
        "hostname 'x' doesn't match 'y'; Host: h"
    )
    error = await _post_qos_error(httpx_mock, plain)

    assert str(error) == _expected_message(plain)


async def _redacted_answer(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, text: str, *, node_names: bool
) -> str:
    if node_names:
        _set_node_names(monkeypatch)
    else:
        monkeypatch.delenv("ONEDATA_MCP_NODE_NAMES", raising=False)
    return str(await _post_qos_error(httpx_mock, _qos_error(text)))


def test_replace_node_names_matches_whole_names_case_insensitively() -> None:
    names = (("node1", "short"), ("node1.hpc.example", "long"), ("2001:db8::1", "v6"))
    replaced = replace_node_names(
        "NODE1.HPC.EXAMPLE:8443, node1:9000. node10 node1.other.example x-node1 [2001:db8::1]:8443 "
        "2001:db8::1 2001:db8::10 and node1.",
        names,
    )

    assert replaced == (
        "long, short. node10 node1.other.example x-node1 v6 v6 2001:db8::10 and short."
    )


@pytest.mark.asyncio
async def test_a_known_node_after_a_newline_or_in_a_quoted_pair_is_replaced_or_masked(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Redaction reads the decoded strings: `\\n` and `\\"` in the JSON text break no boundary."""
    _set_env(monkeypatch)
    monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", json.dumps({"node17": "provider-a"}))
    error = await _post_qos_error(
        httpx_mock, _qos_error('Failed:\nnode17:8443\tand host="node17" failed\nport=9')
    )

    assert str(error) == _expected_message(
        _qos_error("Failed:\nprovider-a\tand <address> failed\n<address>")
    )


def test_a_pair_mask_stops_before_sentence_punctuation_and_closing_brackets() -> None:
    config = get_oneprovider_config_for(
        "https://provider.example", node_names=(("node-1.example", "provider-a"),)
    )

    assert (
        redact_endpoints("failed on port=8443. Retry (host=x.example), {port=1}; port='9'.", config)
        == "failed on <address>. Retry (<address>), {<address>}; <address>."
    )


def test_redact_endpoints_without_a_node_list_masks_urls_and_host_names_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    config = get_oneprovider_config_for("https://provider.example")
    plain = f"port={NODE_PORT} {NODE_IP} lab.data notes.txt"
    text = f"https://{NODE_FQDN}:{NODE_PORT}/x {plain} and provider.example:8443 at {NODE_FQDN}"

    assert redact_endpoints(plain, config) == plain
    assert redact_endpoints(text, config) == f"<address> {plain} and oneprovider at <address>"
    as_json = json.dumps({"d": text, "n": [plain]})
    assert redact_json_text(as_json, config) == json.dumps(
        {"d": f"<address> {plain} and oneprovider at <address>", "n": [plain]}
    )
    # a string that nothing touches stays byte for byte
    assert redact_json_text(json.dumps({"n": [plain]}), config) == json.dumps({"n": [plain]})


def test_a_listed_ipv6_address_does_not_match_inside_a_longer_one() -> None:
    names = (("2001:db8::1", "v6"),)

    assert (
        replace_node_names(
            "a 2001:db8::1:2, b 2001:db8::1: c [2001:db8::1]:8443 d 12001:db8::1 e (2001:db8::1) "
            "f peer:2001:db8::1 unavailable",
            names,
        )
        == "a 2001:db8::1:2, b v6: c v6 d 12001:db8::1 e (v6) f peer:v6 unavailable"
    )


@pytest.mark.asyncio
async def test_an_error_id_equal_to_a_known_node_reaches_the_span_as_its_declared_name(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(telemetry, "get_tracer", lambda: tracer)
    monkeypatch.setattr(telemetry, "_telemetry_enabled", True)
    _set_env(monkeypatch)
    monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", json.dumps({"node17": "provider-a"}))
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        status_code=500,
        json={"error": {"id": "node17", "details": {"errno": "node17"}}},
    )

    with pytest.raises(OnedataApiError):
        await request(get_oneprovider_config(), "GET", "/spaces")

    span = next(s for s in exporter.get_finished_spans() if s.name == "onedata.rest GET")
    assert span.attributes["onedata.error_id"] == "provider-a"
    assert span.attributes["onedata.errno"] == "provider-a"
    assert "node17" not in repr(span.to_json())


@pytest.mark.asyncio
async def test_without_a_node_list_the_span_attributes_are_onedatas_ids_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The span follows the body's rule: no list, no replacement."""
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
        json={"error": {"id": "node17", "details": {"errno": "node17"}}},
    )

    with pytest.raises(OnedataApiError):
        await request(get_oneprovider_config(), "GET", "/spaces")

    span = next(s for s in exporter.get_finished_spans() if s.name == "onedata.rest GET")
    assert span.attributes["onedata.error_id"] == "node17"
    assert span.attributes["onedata.errno"] == "node17"


def test_replace_node_names_with_no_list_is_the_identity() -> None:
    assert replace_node_names(f"{NODE_FQDN}:{NODE_PORT}", ()) == f"{NODE_FQDN}:{NODE_PORT}"


@pytest.mark.parametrize("unit", ["a.", "ab.", "1.", "ab-", "1:", "1.1.", "host=' ", "http:/"])
@pytest.mark.parametrize("listed", [True, False], ids=["listed", "no-list"])
def test_redaction_cost_stays_linear_on_hostile_input(unit: str, listed: bool) -> None:
    """Adversarial `a.a.a. ...` bodies must not cost a rescan from every label.

    The URL and configured-host rules run in every mode; the pair rule only with a list.
    """
    config = get_oneprovider_config_for(
        "https://provider.example", node_names=(("node-1.example", "a"),) if listed else ()
    )
    text = unit * 200_000
    started = time.perf_counter()
    redact_endpoints(text, config)
    assert time.perf_counter() - started < 5.0


# ---------------------------------------------------------------------------
# The deploy's input: ONEDATA_MCP_NODE_NAMES
# ---------------------------------------------------------------------------


def test_node_names_unset_or_empty_means_no_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEDATA_MCP_NODE_NAMES", raising=False)
    assert get_node_names() == ()
    monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", "  ")
    assert get_node_names() == ()
    monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", "{}")
    assert get_node_names() == ()


def test_node_names_keys_are_hosts_with_or_without_a_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ONEDATA_MCP_NODE_NAMES",
        json.dumps(
            {
                "Node-1.Site.Example:8443": "provider-a",
                "node-1.site.example": "provider-a",
                "10.0.0.5": "provider-a",
                "[2001:db8::5]:8443": "provider-b",
                "2001:db8::6": "provider-b",
            }
        ),
    )

    assert get_node_names() == (
        ("10.0.0.5", "provider-a"),
        ("2001:db8::5", "provider-b"),
        ("2001:db8::6", "provider-b"),
        ("node-1.site.example", "provider-a"),
    )


@pytest.mark.parametrize(
    "value",
    [
        "not json secret-node.example",
        '["secret-node.example"]',
        '{"secret-node.example": ""}',
        '{"secret-node.example": 5}',
        '{"": "provider-a"}',
        '{"https://secret-node.example/x": "provider-a"}',
        '{"secret node.example": "provider-a"}',
        '{"secret-node.example": "a", "secret-node.example:8443": "b"}',
    ],
)
def test_a_malformed_node_list_is_refused_without_echoing_its_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", value)

    with pytest.raises(ValueError, match="ONEDATA_MCP_NODE_NAMES") as excinfo:
        get_node_names()

    assert "secret-node" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_the_server_refuses_to_start_on_a_malformed_node_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onedata_mcp import main as main_module

    monkeypatch.setenv("ONEDATA_MCP_NODE_NAMES", "{")

    with pytest.raises(ValueError, match="ONEDATA_MCP_NODE_NAMES"):
        main_module._create_onedata_mcp_server()


def test_both_config_getters_carry_the_node_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    _set_node_names(monkeypatch)

    assert get_onezone_config().node_names == get_oneprovider_config().node_names
    assert ("node-17.hpc-site.example", "oneprovider-a") in get_oneprovider_config().node_names


@pytest.mark.asyncio
async def test_a_tool_answer_shows_the_declared_name_of_a_known_node(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Through FastMCP, as the agent sees it."""
    _set_env(monkeypatch)
    _set_node_names(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        status_code=500,
        json={
            "error": {"id": "internalServerError", "description": f"{NODE_FQDN}:{NODE_PORT} down"}
        },
    )
    mcp = FastMCP(name="onedata-mcp-test")
    spaces_module.register_module(mcp)

    async with Client(mcp) as client:
        result = await client.call_tool("list_user_spaces", {}, raise_on_error=False)

    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    assert result.is_error is True
    assert "oneprovider-a down" in text
    assert NODE_FQDN not in text
    assert NODE_PORT not in text


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
    _set_node_names(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        json=[],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/dsadas/files",
        status_code=400,
        json={
            "error": {
                "id": "spaceNotSupportedBy",
                "details": {"spaceId": f"https://{NODE_FQDN}:{NODE_PORT}/x", "providerId": "p1"},
            }
        },
    )

    # the shared resolver of the stable tools (list_children has its own text since design/14)
    with pytest.raises(OnedataInvalidSpaceError) as excinfo:
        await files.list_files_recursively("dsadas", limit=10)

    _assert_host_free(str(excinfo.value))
    assert str(excinfo.value).startswith('Space "<address>" does not exist.')


@pytest.mark.asyncio
async def test_list_children_says_the_space_is_unsupported_without_echoing_the_service(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """design/14: it names the target the caller gave, never the service's echoed id, and
    Onedata's own message that follows is redacted."""
    _set_env(monkeypatch)
    _set_node_names(monkeypatch)
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

    with pytest.raises(OnedataApiError) as excinfo:
        await files.list_children("dsadas", limit=10, offset=0)

    _assert_host_free(str(excinfo.value))
    assert str(excinfo.value).startswith(
        "This provider does not support the space of file id 'dsadas'"
    )


@pytest.mark.asyncio
async def test_without_a_node_list_an_invalid_space_error_masks_a_url_the_service_echoes(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _set_env(monkeypatch)
    echoed = f"https://{NODE_FQDN}:{NODE_PORT}/x"
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
        json=[],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/dsadas/files",
        status_code=400,
        json={
            "error": {
                "id": "spaceNotSupportedBy",
                "details": {"spaceId": echoed, "providerId": "p1"},
            }
        },
    )

    with pytest.raises(OnedataInvalidSpaceError) as excinfo:
        await files.list_files_recursively("dsadas", limit=10)

    assert str(excinfo.value).startswith('Space "<address>" does not exist.')


@pytest.mark.asyncio
@pytest.mark.parametrize("node_names", [False, True], ids=["unset", "listed"])
async def test_a_transport_error_and_a_non_json_body_keep_their_fixed_form_in_both_modes(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, node_names: bool
) -> None:
    """Library texts, not Onedata's: host-free whether or not a node list is given."""
    _set_env(monkeypatch)
    if node_names:
        _set_node_names(monkeypatch)
    httpx_mock.add_exception(
        httpx.ConnectError(POOL_TEXT),
        method="GET",
        url="https://provider.example/api/v3/oneprovider/spaces",
    )
    httpx_mock.add_response(
        method="GET",
        url="https://provider.example/api/v3/oneprovider/data/f1",
        status_code=502,
        headers={"Content-Type": "text/html"},
        text=f"<h1>Bad Gateway</h1> https://{NODE_FQDN}:{NODE_PORT} host={NODE_FQDN} {NODE_IP}",
    )

    with pytest.raises(OnedataApiError) as transport:
        await request(get_oneprovider_config(), "GET", "/spaces")
    with pytest.raises(OnedataApiError) as non_json:
        await request(get_oneprovider_config(), "GET", "/data/f1")

    assert str(transport.value) == (
        "Onedata API request failed: GET /spaces - connection_error (service: oneprovider)"
    )
    assert str(non_json.value) == (
        "Onedata API request failed: GET /data/f1 (status=502) - http_502 (service: oneprovider)"
    )
    _assert_host_free(str(transport.value))
    _assert_host_free(str(non_json.value))


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


# ---------------------------------------------------------------------------
# A peer's domain: the 25.0 error carries `ctx.serviceDomain`, the domain of the service that
# answered, which is not necessarily a host this server is configured with.
# ---------------------------------------------------------------------------

PEER_DOMAIN = "provider-b.default.svc.cluster.local"
PEER_SITE = "peer-lisbon.onedata.example.com"


def _peer_error() -> dict:
    """A 25.0 error body (swagger `Error`) from a peer provider, with the domain in its ctx,
    another in `details`, and a bare host name in the description."""
    return {
        "error": {
            "id": "posix",
            "description": f"Operation failed with POSIX error: enoent (see {PEER_SITE}:8443).",
            "details": {"errno": "enoent", "domain": PEER_SITE, "nested": {"Hostname": "n7"}},
            "ctx": {
                "timestamp": 1750762637248,
                "serviceReleaseVersion": "25.0",
                "serviceId": "a7ee2722915b8bf7fb064eb65087cc61ch9357",
                "serviceDomain": PEER_DOMAIN,
                "serviceBuildVersion": "231-g8d26f953c3",
                "service": "opw",
                "onedataErrorsRevision": "ca3af8cb",
                "module": "metadata_req",
                "line": 45,
            },
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("node_names", [False, True], ids=["unset", "listed"])
async def test_a_peer_providers_domain_in_an_error_never_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, node_names: bool
) -> None:
    _set_env(monkeypatch)
    if node_names:
        _set_node_names(monkeypatch)
    error = await _post_qos_error(httpx_mock, _peer_error())

    text = str(error)
    for leak in (PEER_DOMAIN, PEER_SITE, "cluster.local", "onedata.example.com", "n7"):
        assert leak not in text, leak
    # only the domain-bearing values changed: the rest of the error reads as Onedata wrote it
    expected = _peer_error()
    expected["error"]["ctx"]["serviceDomain"] = "a provider"
    expected["error"]["details"]["domain"] = "a provider"
    expected["error"]["details"]["nested"]["Hostname"] = "a provider"
    expected["error"]["description"] = "Operation failed with POSIX error: enoent (see <address>)."
    assert text == _expected_message(expected)
    assert error.errno == "enoent" and error.error_id == "posix"
    # the response kept on the error is the raw body: it is not what a model reads
    assert error.response["body"]["error"]["ctx"]["serviceDomain"] == PEER_DOMAIN


@pytest.mark.parametrize(
    "key", ["serviceDomain", "domain", "domainName", "subdomain", "host", "hostname", "fqdn"]
)
@pytest.mark.parametrize("spelling", [str, str.lower, str.upper])
def test_a_domain_bearing_field_reads_a_provider_whatever_it_holds(key: str, spelling) -> None:
    config = get_oneprovider_config_for("https://provider.example")
    body = {"error": {"id": "x", "ctx": {spelling(key): "anything at all"}, "keep": "provider2"}}

    shown = redact_json_text(json.dumps(body, indent=2), config)

    assert json.loads(shown) == {
        "error": {"id": "x", "ctx": {spelling(key): "a provider"}, "keep": "provider2"}
    }


def test_a_domain_bearing_field_that_holds_no_string_and_a_value_named_like_one_are_kept() -> None:
    config = get_oneprovider_config_for("https://provider.example")
    body = {"domain": None, "host": 7, "k": "host", "list": ["domain", "host"], "d": 'a "host": b'}

    assert redact_json_text(json.dumps(body), config) == json.dumps(body)


# QoS expressions and attribute names are not host names and survive in every mode
QOS_TEXTS = [
    "providerId=abc & anyStorage",
    "geo=PL",
    "country=FR & type=ssd",
    "geo=EU \\ providerId=p123",
    "storage.type=ssd | cloud.region=eu",
    "Invalid QoS expression near '==': use '=' instead (storageId=s1 | geo=PL & anyStorage).",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("node_names", [False, True], ids=["unset", "listed"])
@pytest.mark.parametrize("expression", QOS_TEXTS)
async def test_a_qos_expression_survives_the_redaction(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, node_names: bool, expression: str
) -> None:
    _set_env(monkeypatch)
    body = _qos_error(f"Bad expression {expression}")
    if node_names:
        _set_node_names(monkeypatch)
    error = await _post_qos_error(httpx_mock, body)

    assert str(error) == _expected_message(body)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # a host name: generic, private-network and country TLDs; with a port; in an address
        ("peer.example.com", "<address>"),
        ("peer.example.com:8443.", "<address>."),
        ("store.site-b.pl", "<address>"),
        ("a.b.c.internal", "<address>"),
        ("peer.test and peer.example and peer.local", "<address> and <address> and <address>"),
        ("user@peer.example.com", "user@<address>"),
        ("(peer.example.com)", "(<address>)"),
        ("Peer-1.Site.EXAMPLE", "<address>"),
        # a file name, a version, a path component, an attribute name: not a host name
        (
            "notes.txt data.v2.csv archive.tar.gz report.pdf",
            "notes.txt data.v2.csv archive.tar.gz report.pdf",
        ),
        ("script.py README.md run.sh data.in", "script.py README.md run.sh data.in"),
        ("/space/dir.v2.pl/file.example.com", "/space/dir.v2.pl/file.example.com"),
        (
            "v1.2.3 25.0 1.2.3.4 10.0.0.1:8443 192.0.2.7",
            "v1.2.3 25.0 1.2.3.4 10.0.0.1:8443 192.0.2.7",
        ),
        ("storage.type region.geo e.g. i.e.", "storage.type region.geo e.g. i.e."),
        ("peer.example.com0 xpeer.examples", "peer.example.com0 xpeer.examples"),
        # two labels ending in an ambiguous code stay; three or more are a host name
        ("data.pl", "data.pl"),
        ("store.data.pl", "<address>"),
    ],
)
def test_a_bare_host_name_is_masked_and_a_file_name_a_version_or_an_attribute_is_not(
    text: str, expected: str
) -> None:
    config = get_oneprovider_config_for("https://provider.example")

    assert redact_endpoints(text, config) == expected
    assert redact_endpoints(text, config) == redact_endpoints(
        redact_endpoints(text, config), config
    )


@pytest.mark.asyncio
async def test_a_peer_providers_domain_does_not_reach_the_invalid_space_error(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """files.py echoes `details.spaceId` through the same redaction: a host-like id is masked."""
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
                "details": {"spaceId": PEER_SITE, "providerId": "p1"},
                "ctx": {"serviceDomain": PEER_DOMAIN},
            }
        },
    )

    # list_children reads spaceNotSupportedBy as "this provider does not support the space"
    # (the file tools' review: the space may still exist), not as a missing space
    with pytest.raises(OnedataApiError) as excinfo:
        await files.list_children("dsadas", limit=10, offset=0)

    assert str(excinfo.value).startswith("This provider does not support the space of file id")
    assert PEER_SITE not in str(excinfo.value) and PEER_DOMAIN not in str(excinfo.value)
