from __future__ import annotations

import contextlib
import json
import logging
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


class OnedataError(RuntimeError):
    """Base class for Onedata-related errors."""

    def __init__(self, message: str, response: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response = response

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
        errno = details.get("errno")
        return errno if isinstance(errno, str) else None

    @property
    def error_id(self) -> str | None:
        error = self.body.get("error")
        if not isinstance(error, dict):
            return None
        error_id = error.get("id")
        return error_id if isinstance(error_id, str) else None


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
    url = config.base_url
    headers = dict(config.auth_headers)
    verify = config.verify_ssl

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

    try:
        with span_cm as span:
            if span is not None:
                span.set_attribute("http.request.method", method)
                span.set_attribute("url.path", path)
                telemetry.inject_traceparent(headers)
            if json_body is not None:
                async with httpx.AsyncClient(
                    base_url=url, headers=headers, verify=verify
                ) as client:
                    response = await client.request(method, path, params=params, json=json_body)
            else:
                async with httpx.AsyncClient(
                    base_url=url, headers=headers, verify=verify
                ) as client:
                    response = await client.request(method, path, params=params, content=body)
            if span is not None:
                span.set_attribute("http.response.status_code", response.status_code)
    except Exception as e:
        err = f"Onedata API request failed: {method} {path} - {e!s}"
        raise OnedataApiError(err, response=None) from e

    try:
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            response_body: Any = response.json()
        else:
            response_body = response.text
    except ValueError:
        response_body = response.content

    if response.is_error:
        response_payload = {"status_code": response.status_code, "body": response_body}
        details = _format_body_for_log(response_body)

        raise OnedataApiError(
            f"Onedata API request failed: {method} {path} "
            f"(status={response.status_code}) - {details}",
            response=response_payload,
        )

    logger.debug(
        "Onedata API request successful: %s %s\nStatus: %s\nHeaders: %s\nBody: %s",
        method,
        path,
        response.status_code,
        dict(response.headers),
        _format_body_for_log(response_body),
    )

    return {"status_code": response.status_code, "body": response_body}
