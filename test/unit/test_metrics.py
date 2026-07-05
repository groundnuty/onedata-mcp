"""Tests for the OTel metrics instrumentation (opt-in, no-op by default)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from pytest_httpx import HTTPXMock

from onedata_mcp import telemetry
from onedata_mcp.telemetry import TracingMiddleware


@pytest.fixture
def metric_reader(monkeypatch: pytest.MonkeyPatch) -> InMemoryMetricReader:
    """In-memory meter injected into the module, cached instruments reset.

    Avoids the process-global MeterProvider (set-once) by monkeypatching
    ``telemetry.get_meter`` + flipping the flag, and clears the module-cached
    histograms so they rebind to this test meter.
    """

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("test")
    monkeypatch.setattr(telemetry, "get_meter", lambda: meter)
    monkeypatch.setattr(telemetry, "_metrics_enabled", True)
    monkeypatch.setattr(telemetry, "_tool_duration_hist", None)
    monkeypatch.setattr(telemetry, "_rest_duration_hist", None)
    return reader


def _points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    return list(metric.data.data_points)
    return []


def _ctx(tool_name: str) -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(name=tool_name, meta=None))


def _set_oneprovider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://oneprovider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")


# --- tool-call duration ----------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_records_duration_ok(metric_reader: InMemoryMetricReader) -> None:
    async def call_next(_ctx: object) -> str:
        return "ok"

    await TracingMiddleware().on_call_tool(_ctx("list_user_spaces"), call_next)

    points = _points(metric_reader, "mcp.tool.call.duration")
    assert len(points) == 1
    p = points[0]
    assert p.attributes["gen_ai.tool.name"] == "list_user_spaces"
    assert p.attributes["status"] == "ok"
    assert p.count == 1


@pytest.mark.asyncio
async def test_tool_call_records_duration_error(metric_reader: InMemoryMetricReader) -> None:
    async def call_next(_ctx: object) -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await TracingMiddleware().on_call_tool(_ctx("delete_file"), call_next)

    points = _points(metric_reader, "mcp.tool.call.duration")
    assert len(points) == 1
    assert points[0].attributes["gen_ai.tool.name"] == "delete_file"
    assert points[0].attributes["status"] == "error"


# --- REST-call duration ----------------------------------------------------


@pytest.mark.asyncio
async def test_rest_call_records_duration_2xx(
    metric_reader: InMemoryMetricReader,
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

    points = _points(metric_reader, "onedata.rest.request.duration")
    assert len(points) == 1
    assert points[0].attributes["http.request.method"] == "GET"
    assert points[0].attributes["http.response.status_class"] == "2xx"


@pytest.mark.asyncio
async def test_rest_call_records_duration_non_2xx(
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    from onedata_mcp.api import transfers
    from onedata_mcp.utils import OnedataApiError

    _set_oneprovider_env(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://oneprovider.example/api/v3/oneprovider/transfers/tZ",
        status_code=404,
        json={"error": {"id": "posix"}},
    )

    with pytest.raises(OnedataApiError):
        await transfers.get_transfer("tZ")

    points = _points(metric_reader, "onedata.rest.request.duration")
    assert len(points) == 1
    assert points[0].attributes["http.response.status_class"] == "4xx"


# --- no-op when disabled ---------------------------------------------------


def test_setup_metrics_noop_when_otel_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_metrics_enabled", False)
    assert telemetry.setup_metrics() is False
    assert telemetry.metrics_enabled() is False


@pytest.mark.asyncio
async def test_tool_call_harmless_when_metrics_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry, "_metrics_enabled", False)

    async def call_next(_ctx: object) -> str:
        return "RESULT"

    # Recording against the no-op meter must not raise or change behaviour.
    result = await TracingMiddleware().on_call_tool(_ctx("list_user_spaces"), call_next)
    assert result == "RESULT"
