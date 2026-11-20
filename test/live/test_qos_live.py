"""Live test of `remove_qos_requirement` (and the `get_qos_requirement` it shares a guard with)
against the LOCAL Compose federation.

Skipped unless ONEDATA_MCP_LIVE_QOS=1. With it set, the test REFUSES to run (it fails) unless the
Oneprovider host, and the Onezone host when one is set, is `127.0.0.1`, `localhost` or ends in
`.datahub.test` AND resolves only to loopback addresses (a `*.datahub.test` name mapped to any
other address is refused). It creates and deletes a file and QoS requirements, and must never
touch production. Once the check passes, name resolution for those hosts is pinned to the
addresses that were checked (the TLS host name stays the URL's), and proxy variables are
ignored, so the connection cannot go anywhere else.

    make test-qos-live      the usual ONEDATA_* variables, set by hand
    make valfed-qos-live    against the project's own federation, in its space omcp-live

Environment (the usual four, plus the space to work in):

    ONEDATA_ONEPROVIDER_HOST, ONEDATA_ONEPROVIDER_TOKEN
    ONEDATA_ONEZONE_HOST, ONEDATA_ONEZONE_TOKEN
    ONEDATA_MCP_LIVE_QOS_SPACE   name of a space the local provider supports

The tools are called through an in-process MCP client, so what is checked is what a model gets:
the answer on success and the error text on failure. What it settles (unit tests use fakes;
the swagger gives only the statuses). Every failure is checked for its actual status, not only
for the hint, and each prints the answer for the record:

- a requirement added on a file shows up in the file's QoS summary (polled: not assumed to be
  immediate), `get_qos_requirement` reads it back, the removal answers nothing, and the summary
  stops listing it (printed: how long each took);
- removing the same id again fails with a 400 or 404 and the hint that fits the body;
- an id that never existed fails the same way, and so does reading it;
- a real FILE id passed as the requirement id fails, and removes nothing.

Every summary read must carry a `requirements` mapping: a missing field is a failure, not "gone".
Cleanup removes ONLY the requirement ids this test created (it tracks them), never an id from a
summary, which may be inherited from the space root; each cleanup step is allowed to fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP

from onedata_mcp.api.files import create_file, delete_file, get_file_id
from onedata_mcp.api.qos import remove_qos_requirement
from onedata_mcp.modules import qos as qos_module

ENABLE = "ONEDATA_MCP_LIVE_QOS"
SPACE_ENV = "ONEDATA_MCP_LIVE_QOS_SPACE"
APPEAR_TIMEOUT_S = 60.0
GONE_TIMEOUT_S = 60.0
POLL_S = 1.0
# The swagger lists 400 and 404 for an id that names nothing; which one 25.0 gives is printed.
UNKNOWN_ID_STATUSES = {400, 404}

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE) != "1", reason=f"live test: set {ENABLE}=1 (local Compose only)"
)


# ---------------------------------------------------------------------------
# Locality: the hosts must be local by name AND resolve only to loopback addresses
# ---------------------------------------------------------------------------


class NotLoopback(Exception):
    """A configured host is not provably the local machine."""


def is_local_host(host: str | None) -> bool:
    return bool(host) and (host in {"127.0.0.1", "localhost"} or host.endswith(".datahub.test"))


def resolve_loopback_only(
    host: str, port: int, resolve: Callable[..., list] = socket.getaddrinfo
) -> tuple[str, ...]:
    """The addresses `host` resolves to, provided EVERY one is loopback; else NotLoopback.

    A name that does not resolve, or resolves to nothing, is refused too.
    """
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise NotLoopback(f"{host!r} does not resolve ({type(error).__name__})") from None
    addresses = sorted({str(info[4][0]).split("%", 1)[0] for info in infos})
    if not addresses:
        raise NotLoopback(f"{host!r} resolves to no address")
    others = []
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_loopback:
                others.append(address)
        except ValueError:
            others.append(address)
    if others:
        raise NotLoopback(f"{host!r} resolves to non-loopback address(es) {others}")
    return tuple(addresses)


def pin_resolution(monkeypatch: pytest.MonkeyPatch, pins: dict[str, tuple[str, ...]]) -> None:
    """Make `socket.getaddrinfo` answer for each pinned host name with the addresses that were
    checked, whatever DNS or /etc/hosts says by the time the connection is made. The URL keeps
    its host name, so TLS verifies against it."""
    real = socket.getaddrinfo

    def pinned(host, port, *args, **kwargs):  # noqa: ANN001, ANN202
        # anyio asks with the name as bytes
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        addresses = pins.get(name.lower()) if isinstance(name, str) else None
        if addresses is None:
            return real(host, port, *args, **kwargs)
        found: list = []
        for address in addresses:
            found.extend(real(address, port, *args, **kwargs))
        return found

    monkeypatch.setattr(socket, "getaddrinfo", pinned)


def enforce_local_federation(
    monkeypatch: pytest.MonkeyPatch,
    urls: dict[str, str],
    resolve: Callable[..., list] = socket.getaddrinfo,
) -> None:
    """Refuse (pytest.fail) unless every URL's host is local by name and loopback by address;
    then pin its resolution and ignore proxy variables."""
    pins: dict[str, tuple[str, ...]] = {}
    for name, url in urls.items():
        parsed = urlparse(url)
        host = parsed.hostname
        if not is_local_host(host):
            pytest.fail(
                "refusing to run: the live QoS test writes a file and QoS requirements, and runs "
                f"only against 127.0.0.1, localhost or *.datahub.test ({name} host: {host!r})",
                pytrace=False,
            )
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            pins[host.lower()] = resolve_loopback_only(host, port, resolve)
        except (ValueError, NotLoopback) as error:
            pytest.fail(
                f"refusing to run: {name} host is not loopback-only ({error})", pytrace=False
            )
    pin_resolution(monkeypatch, pins)
    # httpx trusts the environment's proxies; a proxy would carry the traffic elsewhere
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "*")


@pytest.fixture(autouse=True)
def _local_federation_only(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = {"ONEDATA_ONEPROVIDER_HOST": os.environ.get("ONEDATA_ONEPROVIDER_HOST", "")}
    if os.environ.get("ONEDATA_ONEZONE_HOST"):
        urls["ONEDATA_ONEZONE_HOST"] = os.environ["ONEDATA_ONEZONE_HOST"]
    enforce_local_federation(monkeypatch, urls)


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


@dataclass
class Probe:
    """A file this test made, and the requirement ids it created on it (the only ones cleanup
    may remove: a summary also lists ids inherited from ancestors, the space root included)."""

    path: str
    file_id: str
    created: list[str] = field(default_factory=list)


@pytest_asyncio.fixture
async def probe_file(space: str):
    """A small file written through the local provider; deleted afterwards, and the
    requirements this test created on it removed."""
    path = f"/{space}/qos-live-probe-{int(time.time())}-{secrets.token_hex(3)}.txt"
    await create_file(path, "qos probe\n")
    probe = Probe(path=path, file_id=await get_file_id(path))
    yield probe
    await cleanup_probe(probe)


async def cleanup_probe(probe: Probe) -> None:
    """Remove the requirements this test created on the probe (and only those), then the file.
    Cleanup must not hide the test's result, and one step failing must not skip the next."""
    for qos_id in reversed(probe.created):
        with contextlib.suppress(Exception):
            await remove_qos_requirement(qos_id)
    with contextlib.suppress(Exception):
        await delete_file(probe.path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(mcp: FastMCP, tool: str, arguments: dict):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    return result, text


def _payload(result, text: str) -> dict:
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(text)


async def _add_requirement(mcp: FastMCP, probe: Probe) -> str:
    result, text = await _call(
        mcp,
        "add_file_qos_requirement",
        {"file_id_or_path": probe.path, "expression": "anyStorage", "replicas_num": 1},
    )
    assert result.is_error is False, text
    qos_id = _payload(result, text).get("qosRequirementId")
    assert isinstance(qos_id, str) and qos_id, f"no qosRequirementId in the answer: {text}"
    probe.created.append(qos_id)  # tracked at once: cleanup removes exactly these
    return qos_id


async def _requirement_ids(mcp: FastMCP, path: str) -> set[str]:
    result, text = await _call(mcp, "get_file_qos_summary", {"file_id_or_path": path})
    assert result.is_error is False, text
    payload = _payload(result, text)
    requirements = payload.get("requirements")
    # a summary without the mapping is a failed read, never "no requirements"
    assert isinstance(requirements, dict), (
        f"the QoS summary has no `requirements` mapping: {payload}"
    )
    return set(requirements)


async def _wait_listed(
    mcp: FastMCP, path: str, qos_id: str, *, listed: bool, timeout: float
) -> float:
    """Poll the summary until `qos_id` is (or is no longer) in it; the seconds it took."""
    began = time.monotonic()
    while (qos_id in await _requirement_ids(mcp, path)) != listed:
        waited = time.monotonic() - began
        assert waited < timeout, (
            f"{qos_id} still {'not listed' if listed else 'listed'} after {waited:.0f} s"
        )
        await asyncio.sleep(POLL_S)
    return time.monotonic() - began


_STATUS_RE = re.compile(r"\(status=(\d{3})\)")
WHERE = "QoS requirement ids are the keys of `requirements` in its QoS summary"


def _status_of(text: str) -> int:
    found = _STATUS_RE.search(text)
    assert found, f"no HTTP status in the error: {text}"
    return int(found.group(1))


def _assert_failure_with_the_hint_that_fits(text: str, qos_id: str) -> int:
    """The failure of a removal that named no requirement: a documented 4xx status, and the hint
    that matches what Onedata's body says (a specific one only for a structured not-found; the
    generic one, which keeps the uncertainty, otherwise). Never the permission hint, which would
    say a retry cannot help. Returns the status."""
    status = _status_of(text)
    assert status in UNKNOWN_ID_STATUSES, f"unexpected status {status}: {text}"
    assert "Not allowed" not in text, text
    assert WHERE in text, text
    if '"notFound"' in text or '"enoent"' in text:
        assert f"No QoS requirement with id '{qos_id[:80]}" in text, text
        assert "removed already" in text, text
    else:
        assert f"Onedata answered {status} without saying the requirement is missing" in text, text
        assert "removed already" not in text, text
    return status


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_requirement_is_added_read_removed_and_a_second_removal_fails(
    qos_mcp: FastMCP, probe_file: Probe
) -> None:
    probe = probe_file
    qos_id = await _add_requirement(qos_mcp, probe)
    appeared = await _wait_listed(
        qos_mcp, probe.path, qos_id, listed=True, timeout=APPEAR_TIMEOUT_S
    )
    print(f"summary listed {qos_id} after {appeared:.1f} s")

    # get_qos_requirement, success: the record names this requirement and its file
    result, text = await _call(qos_mcp, "get_qos_requirement", {"qos_id": qos_id})
    assert result.is_error is False, text
    record = _payload(result, text)
    print(f"get_qos_requirement answered: {record}")
    assert record.get("qosRequirementId") == qos_id
    assert str(record.get("fileId", "")).lower() == probe.file_id.lower()
    assert record.get("replicasNum") == 1
    assert record.get("qosExpression") == "anyStorage"

    result, text = await _call(qos_mcp, "remove_qos_requirement", {"qos_id": qos_id})

    assert result.is_error is False, text
    assert text == ""  # a removal answers nothing (the description says so)
    took = await _wait_listed(qos_mcp, probe.path, qos_id, listed=False, timeout=GONE_TIMEOUT_S)
    print(f"summary stopped listing {qos_id} after {took:.1f} s")

    # the same id again: well-formed, and certainly gone
    result, text = await _call(qos_mcp, "remove_qos_requirement", {"qos_id": qos_id})

    assert result.is_error is True
    print(f"second removal answered: {text}")
    status = _assert_failure_with_the_hint_that_fits(text, qos_id)
    print(f"second removal: status {status}")


@pytest.mark.asyncio
async def test_an_id_that_never_existed_fails_with_a_documented_status_and_its_hint(
    qos_mcp: FastMCP,
) -> None:
    qos_id = secrets.token_hex(16)

    result, text = await _call(qos_mcp, "remove_qos_requirement", {"qos_id": qos_id})

    assert result.is_error is True
    print(f"unknown id answered: {text}")
    status = _assert_failure_with_the_hint_that_fits(text, qos_id)
    print(f"unknown id: status {status}")


@pytest.mark.asyncio
async def test_a_real_file_id_is_not_a_requirement_id_and_removes_nothing(
    qos_mcp: FastMCP, probe_file: Probe
) -> None:
    """A model that passes the file's id where the requirement's belongs."""
    probe = probe_file
    qos_id = await _add_requirement(qos_mcp, probe)
    await _wait_listed(qos_mcp, probe.path, qos_id, listed=True, timeout=APPEAR_TIMEOUT_S)

    result, text = await _call(qos_mcp, "remove_qos_requirement", {"qos_id": probe.file_id})

    assert result.is_error is True
    print(f"file id as requirement id answered: {text}")
    status = _assert_failure_with_the_hint_that_fits(text, probe.file_id)
    print(f"file id: status {status}")
    assert qos_id in await _requirement_ids(qos_mcp, probe.path)  # nothing was removed


@pytest.mark.asyncio
async def test_get_qos_requirement_for_an_id_that_never_existed_fails_with_a_documented_status(
    qos_mcp: FastMCP,
) -> None:
    qos_id = secrets.token_hex(16)

    result, text = await _call(qos_mcp, "get_qos_requirement", {"qos_id": qos_id})

    assert result.is_error is True
    print(f"get_qos_requirement unknown id answered: {text}")
    assert f"GET /qos_requirements/{qos_id}" in text
    assert _status_of(text) in UNKNOWN_ID_STATUSES, text
