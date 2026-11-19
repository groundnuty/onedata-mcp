"""OpenTelemetry instrumentation for the Onedata MCP server.

Opt-in and no-op by default. When the standard ``OTEL_*`` environment variables
are unset, no tracer provider or exporter is installed: spans fall through to
the OpenTelemetry API's built-in no-op tracer, so there is no startup cost and
no exporter retry spam without a collector.

When enabled, the server emits one span per MCP tool call (tool name, duration,
success/error status, error class on failure) and continues an incoming W3C
``traceparent`` so a harness-initiated trace flows harness -> MCP server ->
Onedata REST as a single correlated trace.

Exporter configuration is exclusively via the standard ``OTEL_*`` environment
variables (``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_SERVICE_NAME``, ...). Nothing
is hardcoded — safe for a public repository.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from fastmcp.server.middleware import Middleware
from opentelemetry import metrics, trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind, Status, StatusCode

logger = logging.getLogger(__name__)

_TRACER_NAME = "onedata-mcp"
_telemetry_enabled = False
_metrics_enabled = False
_tool_duration_hist: metrics.Histogram | None = None
_rest_duration_hist: metrics.Histogram | None = None


def telemetry_enabled() -> bool:
    """True once an OTLP exporter has been installed via :func:`setup_telemetry`."""

    return _telemetry_enabled


def metrics_enabled() -> bool:
    """True once an OTLP meter provider has been installed via :func:`setup_metrics`."""

    return _metrics_enabled


def _otel_export_configured() -> bool:
    """Whether the standard OTEL_* env selects an OTLP traces endpoint."""

    return bool(
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


def setup_telemetry() -> bool:
    """Install an OTLP/HTTP tracer provider iff OTEL export is configured.

    Returns ``True`` when telemetry was enabled, ``False`` when it stayed a
    no-op (no endpoint configured, or the OTel SDK is unavailable). Idempotent:
    a second call is a cheap no-op. Never raises — a telemetry-setup failure
    must not take the server down.
    """

    global _telemetry_enabled
    if _telemetry_enabled:
        return True
    if not _otel_export_configured():
        return False

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Resource.create() reads OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES;
        # the OTLP exporter reads OTEL_EXPORTER_OTLP_* itself. Both the imports
        # AND the exporter construction are guarded: a missing SDK or a
        # malformed OTEL_* value must leave telemetry disabled, never crash the
        # server at construction time (honours this function's "never raises").
        provider = TracerProvider(resource=Resource.create())
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
    except Exception as exc:  # noqa: BLE001 — telemetry must never take the server down
        logger.warning("OpenTelemetry setup failed (%s); telemetry stays disabled", exc)
        return False

    _telemetry_enabled = True
    logger.info("OpenTelemetry tracing enabled (OTLP/HTTP exporter)")
    return True


def get_tracer() -> trace.Tracer:
    """Return the server tracer (a no-op tracer when telemetry is disabled)."""

    return trace.get_tracer(_TRACER_NAME)


def setup_metrics() -> bool:
    """Install an OTLP/HTTP meter provider iff OTEL export is configured.

    Mirrors :func:`setup_telemetry`: opt-in via the same OTEL_* gate, no-op when
    unset (metrics fall through to the API's no-op meter — no exporter, no retry
    spam), idempotent, and never raises.
    """

    global _metrics_enabled
    if _metrics_enabled:
        return True
    if not _otel_export_configured():
        return False

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        provider = MeterProvider(metric_readers=[reader], resource=Resource.create())
        metrics.set_meter_provider(provider)
    except Exception as exc:  # noqa: BLE001 — metrics must never take the server down
        logger.warning("OpenTelemetry metrics setup failed (%s); metrics stay disabled", exc)
        return False

    _metrics_enabled = True
    logger.info("OpenTelemetry metrics enabled (OTLP/HTTP exporter)")
    return True


def get_meter() -> metrics.Meter:
    """Return the server meter (a no-op meter when metrics are disabled)."""

    return metrics.get_meter(_TRACER_NAME)


def tool_call_duration() -> metrics.Histogram:
    """Cached histogram of MCP tool-call durations (ms).

    Created lazily from :func:`get_meter` on first use. ``setup_metrics`` runs
    at server construction, so by the first tool call the real meter provider is
    already installed; when metrics are disabled this is a no-op instrument.
    """

    global _tool_duration_hist
    if _tool_duration_hist is None:
        _tool_duration_hist = get_meter().create_histogram(
            "mcp.tool.call.duration",
            unit="ms",
            description="Duration of MCP tool calls",
        )
    return _tool_duration_hist


def rest_request_duration() -> metrics.Histogram:
    """Cached histogram of outbound Onedata REST-call durations (ms)."""

    global _rest_duration_hist
    if _rest_duration_hist is None:
        _rest_duration_hist = get_meter().create_histogram(
            "onedata.rest.request.duration",
            unit="ms",
            description="Duration of outbound Onedata REST calls",
        )
    return _rest_duration_hist


def _record(hist: metrics.Histogram, value: float, attributes: dict[str, Any]) -> None:
    """Best-effort histogram record — a metrics failure must never propagate."""

    try:
        hist.record(value, attributes)
    except Exception:  # noqa: BLE001 — observability is never worth failing the call
        logger.debug("metric record failed", exc_info=True)


def record_tool_call(tool_name: str, status: str, duration_ms: float) -> None:
    """Record an MCP tool-call duration sample (dims: tool name, ok/error)."""

    _record(
        tool_call_duration(),
        duration_ms,
        {ATTR_TOOL_NAME: tool_name, "status": status},
    )


def record_rest_request(method: str, status_class: str, duration_ms: float) -> None:
    """Record an outbound REST-call duration sample (dims: method, status class)."""

    _record(
        rest_request_duration(),
        duration_ms,
        {"http.request.method": method, "http.response.status_class": status_class},
    )


def context_from_carrier(carrier: Mapping[str, Any] | None):
    """Extract a remote W3C trace context from an MCP ``_meta`` carrier.

    The MCP client (e.g. the experiment harness) may place a ``traceparent`` in
    the request ``_meta``; extracting it lets the server span continue that
    trace. Returns ``None`` when there is nothing to extract.
    """

    if not carrier:
        return None
    if not isinstance(carrier, Mapping):
        return None
    # The default global propagator is W3C tracecontext + baggage.
    return extract(dict(carrier))


def inject_traceparent(headers: dict[str, str]) -> None:
    """Inject the current trace context into outbound Onedata REST headers.

    No-op when telemetry is disabled (no active recording span to propagate).
    """

    if _telemetry_enabled:
        inject(headers)


# Attribute keys — OTel GenAI semantic conventions where they fit.
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_OPERATION = "gen_ai.operation.name"
ATTR_ERROR_TYPE = "error.type"


class TracingMiddleware(Middleware):
    """FastMCP middleware emitting one span per tool call.

    Registered via ``mcp.add_middleware(TracingMiddleware())``. Only
    ``on_call_tool`` is overridden; every other MCP method passes through the
    base class unchanged.
    """

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        message = getattr(context, "message", None)
        tool_name = getattr(message, "name", None) or "unknown"

        carrier = getattr(message, "meta", None)
        if carrier is not None and not isinstance(carrier, Mapping):
            # pydantic Meta model -> dict
            dump = getattr(carrier, "model_dump", None)
            carrier = dump(exclude_none=True) if callable(dump) else None
        parent_ctx = context_from_carrier(carrier)

        tracer = get_tracer()
        t0 = time.perf_counter()
        status = "ok"
        with tracer.start_as_current_span(
            f"mcp.tool/{tool_name}",
            context=parent_ctx,
            kind=SpanKind.SERVER,
        ) as span:
            if span.is_recording():
                span.set_attribute(ATTR_TOOL_NAME, tool_name)
                span.set_attribute(ATTR_OPERATION, "execute_tool")
                span.set_attribute("mcp.method", "tools/call")
            try:
                result = await call_next(context)
            except Exception as exc:
                status = "error"
                if span.is_recording():
                    span.set_attribute(ATTR_ERROR_TYPE, type(exc).__name__)
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                record_tool_call(tool_name, status, (time.perf_counter() - t0) * 1000.0)
            if span.is_recording():
                span.set_status(Status(StatusCode.OK))
            return result
