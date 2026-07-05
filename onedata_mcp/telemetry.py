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
from collections.abc import Mapping
from typing import Any

from fastmcp.server.middleware import Middleware
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind, Status, StatusCode

logger = logging.getLogger(__name__)

_TRACER_NAME = "onedata-mcp"
_telemetry_enabled = False


def telemetry_enabled() -> bool:
    """True once an OTLP exporter has been installed via :func:`setup_telemetry`."""

    return _telemetry_enabled


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
    except ImportError as exc:  # pragma: no cover - defensive
        logger.warning(
            "OTEL_* is set but the OpenTelemetry SDK is not installed (%s); "
            "telemetry stays disabled",
            exc,
        )
        return False

    # Resource.create() reads OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES from
    # the environment; the OTLP exporter reads OTEL_EXPORTER_OTLP_* itself.
    provider = TracerProvider(resource=Resource.create())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _telemetry_enabled = True
    logger.info("OpenTelemetry tracing enabled (OTLP/HTTP exporter)")
    return True


def get_tracer() -> trace.Tracer:
    """Return the server tracer (a no-op tracer when telemetry is disabled)."""

    return trace.get_tracer(_TRACER_NAME)


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
                if span.is_recording():
                    span.set_attribute(ATTR_ERROR_TYPE, type(exc).__name__)
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            if span.is_recording():
                span.set_status(Status(StatusCode.OK))
            return result
