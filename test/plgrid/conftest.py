from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
# So skipif + fixtures see the same vars as runtime (pytest does not load .env by itself).
load_dotenv(_REPO_ROOT / ".env")

_PLGRID_DIR = Path(__file__).resolve().parent
if str(_PLGRID_DIR) not in sys.path:
    sys.path.insert(0, str(_PLGRID_DIR))

from forge_logging import patch_forge_trace_test_result  # noqa: E402
from forge_pytest_integration import LAST_FORGE_RUN  # noqa: E402


class _SuppressFastMCPValidationExceptionLogs(logging.Filter):
    """Drop fastmcp's logger.exception() for Pydantic tool-arg validation (very noisy in e2e)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and record.getMessage().startswith("Error validating tool "))


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("fastmcp.server.server").addFilter(_SuppressFastMCPValidationExceptionLogs())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    hooked = yield
    rep = hooked.get_result() if hasattr(hooked, "get_result") else hooked
    if rep.when != "call":
        return hooked
    if rep.skipped:
        return hooked
    run = item.stash.get(LAST_FORGE_RUN, None)
    if run is None or run.trace_path_written is None:
        return hooked
    patch_forge_trace_test_result(run.trace_path_written, passed=bool(rep.passed))
    return hooked


@pytest.fixture(scope="session")
def forge_api_key() -> str:
    key = os.getenv("PLGRID_FORGE_API_KEY", "").strip()
    if not key:
        pytest.skip("PLGRID_FORGE_API_KEY is not set")
    return key


@pytest.fixture(scope="session")
def forge_model() -> str:
    model = os.getenv("PLGRID_FORGE_MODEL", "").strip()
    if not model:
        pytest.skip("PLGRID_FORGE_MODEL is not set")
    return model


@pytest.fixture(scope="session")
def forge_base_url() -> str:
    return os.getenv("PLGRID_FORGE_BASE_URL", "https://llmlab.plgrid.pl/api/v1").rstrip("/")


@pytest.fixture(scope="session")
def mcp_application() -> Any:
    """A server with the preview flag on: these tests use tools master registers only with it
    (the harvester tools). The server is built here rather than taken from ``main.mcp`` and the
    flag is set only while it is built, so it does not leak into the other tests of a session.
    An explicit ONEDATA_MCP_EXPERIMENTAL in the environment wins."""
    from onedata_mcp import main

    patch = pytest.MonkeyPatch()
    try:
        if "ONEDATA_MCP_EXPERIMENTAL" not in os.environ:
            patch.setenv("ONEDATA_MCP_EXPERIMENTAL", "1")
        return main._create_onedata_mcp_server()
    finally:
        patch.undo()
