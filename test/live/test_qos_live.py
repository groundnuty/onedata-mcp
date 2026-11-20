"""Live test of `remove_qos_requirement` against the LOCAL Compose federation.

Skipped unless ONEDATA_MCP_LIVE_QOS=1. With it set, the test REFUSES to run (it fails) unless
the Oneprovider host, and the Onezone host when one is set, is `127.0.0.1`, `localhost` or ends
in `.datahub.test`: it creates and deletes a file and QoS requirements, and must never touch
production.

    make test-qos-live      the usual ONEDATA_* variables, set by hand
    make valfed-qos-live    against the project's own federation, in its space omcp-live

Environment (the usual four, plus the space to work in):

    ONEDATA_ONEPROVIDER_HOST, ONEDATA_ONEPROVIDER_TOKEN
    ONEDATA_ONEZONE_HOST, ONEDATA_ONEZONE_TOKEN
    ONEDATA_MCP_LIVE_QOS_SPACE   name of a space the local provider supports

The tools are called through an in-process MCP client, so what is checked is what a model gets:
the answer on success and the error text on failure. What it settles (unit tests use fakes;
the swagger gives only the statuses):

- a requirement added on a file is removed by the tool, and the file's QoS summary stops
  listing it (printed: how long that took);
- removing the same id again fails, and the error says where the current ids are (printed: the
  status and Onedata's error, which the unit tests do not pin);
- an id that never existed fails the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP

from onedata_mcp.api.files import create_file, delete_file
from onedata_mcp.api.qos import get_file_qos_summary, remove_qos_requirement
from onedata_mcp.modules import qos as qos_module

ENABLE = "ONEDATA_MCP_LIVE_QOS"
SPACE_ENV = "ONEDATA_MCP_LIVE_QOS_SPACE"
GONE_TIMEOUT_S = 60.0
POLL_S = 1.0

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE) != "1", reason=f"live test: set {ENABLE}=1 (local Compose only)"
)


def is_local_host(host: str | None) -> bool:
    return bool(host) and (host in {"127.0.0.1", "localhost"} or host.endswith(".datahub.test"))


@pytest.fixture(autouse=True)
def _local_federation_only() -> None:
    hosts = {"ONEDATA_ONEPROVIDER_HOST": os.environ.get("ONEDATA_ONEPROVIDER_HOST", "")}
    if os.environ.get("ONEDATA_ONEZONE_HOST"):
        hosts["ONEDATA_ONEZONE_HOST"] = os.environ["ONEDATA_ONEZONE_HOST"]
    for name, url in hosts.items():
        host = urlparse(url).hostname
        if not is_local_host(host):
            pytest.fail(
                "refusing to run: the live QoS test writes a file and QoS requirements, and runs "
                f"only against 127.0.0.1, localhost or *.datahub.test ({name} host: {host!r})",
                pytrace=False,
            )


@pytest.fixture
def space() -> str:
    name = os.environ.get(SPACE_ENV, "").strip()
    if not name:
        pytest.fail(f"{SPACE_ENV} must name a space the local provider supports", pytrace=False)
    return name


@pytest.fixture
def qos_mcp() -> FastMCP:
    mcp = FastMCP(name="onedata-mcp-live-qos")
    qos_module.register_module(mcp)
    return mcp


@pytest_asyncio.fixture
async def probe_file(space: str):
    """A small file written through the local provider; deleted afterwards with every
    requirement still on it."""
    path = f"/{space}/qos-live-probe-{int(time.time())}-{secrets.token_hex(3)}.txt"
    await create_file(path, "qos probe\n")
    yield path
    # cleanup must not hide the test's result
    with contextlib.suppress(Exception):
        summary = await get_file_qos_summary(path)
        for qos_id in summary.get("requirements") or {}:
            with contextlib.suppress(Exception):
                await remove_qos_requirement(qos_id)
    await delete_file(path)


async def _call(mcp: FastMCP, tool: str, arguments: dict):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    return result, text


def _payload(result, text: str) -> dict:
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(text)


async def _requirement_ids(mcp: FastMCP, path: str) -> set[str]:
    result, text = await _call(mcp, "get_file_qos_summary", {"file_id_or_path": path})
    assert result.is_error is False, text
    return set(_payload(result, text).get("requirements") or {})


def _assert_points_to_the_ids(text: str) -> None:
    """A hint that sends the caller to the current ids, and not the permission hint (which
    would say a retry cannot help: wrong for an id that is merely unknown)."""
    assert "get_file_qos_summary" in text, text
    assert "Not allowed" not in text, text


def _assert_not_found_hint(text: str, qos_id: str) -> None:
    assert f"No QoS requirement with id '{qos_id}' exists" in text, text
    _assert_points_to_the_ids(text)


@pytest.mark.asyncio
async def test_a_requirement_is_removed_and_gone_from_the_summary_and_a_second_removal_fails(
    qos_mcp: FastMCP, probe_file: str
) -> None:
    path = probe_file
    result, text = await _call(
        qos_mcp,
        "add_file_qos_requirement",
        {"file_id_or_path": path, "expression": "anyStorage", "replicas_num": 1},
    )
    assert result.is_error is False, text
    qos_id = _payload(result, text)["qosRequirementId"]
    assert qos_id in await _requirement_ids(qos_mcp, path)

    result, text = await _call(qos_mcp, "remove_qos_requirement", {"qos_id": qos_id})

    assert result.is_error is False, text
    assert text == ""  # a removal answers nothing (the description says so)
    began = time.monotonic()
    while qos_id in await _requirement_ids(qos_mcp, path):
        assert time.monotonic() - began < GONE_TIMEOUT_S, f"{qos_id} still listed"
        await asyncio.sleep(POLL_S)
    print(f"summary stopped listing {qos_id} after {time.monotonic() - began:.1f} s")

    # the same id again: well-formed, and certainly gone
    result, text = await _call(qos_mcp, "remove_qos_requirement", {"qos_id": qos_id})

    assert result.is_error is True
    print(f"second removal answered: {text}")
    _assert_not_found_hint(text, qos_id)


@pytest.mark.asyncio
async def test_an_id_that_never_existed_fails_with_the_hint(qos_mcp: FastMCP) -> None:
    qos_id = secrets.token_hex(16)

    result, text = await _call(qos_mcp, "remove_qos_requirement", {"qos_id": qos_id})

    assert result.is_error is True
    print(f"unknown id answered: {text}")
    # 404 ("No QoS requirement ... exists") or 400 ("rejected ... as a QoS requirement id"):
    # either sends the caller to the current ids; which one 25.0 gives is printed above.
    _assert_points_to_the_ids(text)
