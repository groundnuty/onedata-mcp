"""Tests for the OpenTelemetry instrumentation (opt-in, no-op by default)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from pytest_httpx import HTTPXMock

from onedata_mcp import telemetry
from onedata_mcp.telemetry import TracingMiddleware


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """A real tracer wired to an in-memory exporter, injected into the module.

    Avoids the process-global TracerProvider (set-once) by monkeypatching
    ``telemetry.get_tracer`` and flipping the enabled flag.
    """

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(telemetry, "get_tracer", lambda: tracer)
    monkeypatch.setattr(telemetry, "_telemetry_enabled", True)
    return exporter


def _ctx(tool_name: str, meta: object = None) -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(name=tool_name, meta=meta))


# --- setup / no-op --------------------------------------------------------


def test_setup_is_noop_when_otel_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_telemetry_enabled", False)
    assert telemetry.setup_telemetry() is False
    assert telemetry.telemetry_enabled() is False


def test_setup_reports_enabled_when_endpoint_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry, "_telemetry_enabled", False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # Do not touch the global provider across the suite: assert the gating
    # decision, not the provider install.
    assert telemetry._otel_export_configured() is True


def test_setup_stays_disabled_on_exporter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed OTEL_* endpoint must disable telemetry, not crash the server."""
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as otlp

    monkeypatch.setattr(telemetry, "_telemetry_enabled", False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    def boom(*_a: object, **_k: object) -> None:
        raise ValueError("malformed endpoint")

    monkeypatch.setattr(otlp, "OTLPSpanExporter", boom)
    assert telemetry.setup_telemetry() is False
    assert telemetry.telemetry_enabled() is False


@pytest.mark.asyncio
async def test_middleware_passthrough_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry, "_telemetry_enabled", False)

    async def call_next(_ctx: object) -> str:
        return "RESULT"

    result = await TracingMiddleware().on_call_tool(_ctx("list_user_spaces"), call_next)
    assert result == "RESULT"


# --- span emission --------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_span_per_tool_call(span_exporter: InMemorySpanExporter) -> None:
    async def call_next(_ctx: object) -> str:
        return "ok"

    await TracingMiddleware().on_call_tool(_ctx("list_user_spaces"), call_next)

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "mcp.tool/list_user_spaces"
    assert span.attributes["gen_ai.tool.name"] == "list_user_spaces"
    assert span.attributes["gen_ai.operation.name"] == "execute_tool"
    assert span.status.status_code == StatusCode.OK


@pytest.mark.asyncio
async def test_error_span_records_status_and_error_type(
    span_exporter: InMemorySpanExporter,
) -> None:
    async def call_next(_ctx: object) -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await TracingMiddleware().on_call_tool(_ctx("delete_file"), call_next)

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "ValueError"
    # exception was recorded as a span event
    assert any(e.name == "exception" for e in span.events)


# --- trace-context continuation -------------------------------------------


@pytest.mark.asyncio
async def test_honors_incoming_traceparent(
    span_exporter: InMemorySpanExporter,
) -> None:
    trace_id_hex = "0af7651916cd43dd8448eb211c80319c"
    traceparent = f"00-{trace_id_hex}-b7ad6b7169203331-01"

    async def call_next(_ctx: object) -> str:
        return "ok"

    await TracingMiddleware().on_call_tool(
        _ctx("list_user_spaces", meta={"traceparent": traceparent}),
        call_next,
    )

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    # The server span must continue the harness-initiated trace.
    assert format(spans[0].context.trace_id, "032x") == trace_id_hex


@pytest.mark.asyncio
async def test_new_trace_when_no_traceparent(
    span_exporter: InMemorySpanExporter,
) -> None:
    async def call_next(_ctx: object) -> str:
        return "ok"

    await TracingMiddleware().on_call_tool(_ctx("list_user_spaces"), call_next)
    spans = span_exporter.get_finished_spans()
    # A root span (no remote parent) still gets a valid trace id.
    assert spans[0].parent is None
    assert spans[0].context.trace_id != 0


# --- REST child-span status (#10) ------------------------------------------


def _set_oneprovider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://oneprovider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


@pytest.mark.asyncio
async def test_rest_span_marked_error_on_non_2xx(
    span_exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    from onedata_mcp.api import transfers
    from onedata_mcp.utils import OnedataApiError

    _set_oneprovider_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://oneprovider.example/api/v3/oneprovider/transfers/tX",
        status_code=404,
        json={"error": {"id": "posix", "details": {"errno": "enoent"}}},
    )

    with pytest.raises(OnedataApiError):
        await transfers.get_transfer("tX")

    rest = [s for s in span_exporter.get_finished_spans() if s.name == "onedata.rest GET"]
    assert len(rest) == 1
    span = rest[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["http.response.status_code"] == 404
    assert span.attributes["onedata.error_id"] == "posix"
    assert span.attributes["onedata.errno"] == "enoent"


@pytest.mark.asyncio
async def test_rest_span_ok_on_2xx(
    span_exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    from onedata_mcp.api import transfers

    _set_oneprovider_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://oneprovider.example/api/v3/oneprovider/transfers/tY",
        status_code=200,
        json={"transferState": "completed"},
    )

    await transfers.get_transfer("tY")

    rest = [s for s in span_exporter.get_finished_spans() if s.name == "onedata.rest GET"]
    assert len(rest) == 1
    span = rest[0]
    assert span.status.status_code != StatusCode.ERROR
    assert "onedata.error_id" not in span.attributes
