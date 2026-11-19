"""Live test of the readiness tools against the LOCAL Compose federation.

Skipped unless ONEDATA_MCP_LIVE_READINESS=1. With it set, the test REFUSES to run (it fails)
unless the Oneprovider host is `127.0.0.1`, `localhost` or ends in `.datahub.test`: it
creates and deletes a file and a QoS rule, and must never touch production.

    make test-readiness-live

Environment (the usual four, plus the space to work in):

    ONEDATA_ONEPROVIDER_HOST, ONEDATA_ONEPROVIDER_TOKEN
    ONEDATA_ONEZONE_HOST, ONEDATA_ONEZONE_TOKEN
    ONEDATA_MCP_LIVE_READINESS_SPACE   name of a space the local provider supports

What it settles (the points design/07-readiness-tools.md marks UNVERIFIED):

- whether Onedata accepts the hold expression `providerId=<local id> & anyStorage`
  (the fallback to the plain form is reported, not hidden);
- that a file written through this provider reads as ready, and is then held by a rule
  that shows up in the file's QoS summary.

Not covered here, on purpose: whether a rule keeps up with a file still being written, and
whether eviction is refused. Those need a second provider writing; run them by hand on the
two-provider Compose federation and decide whether `_nudge` needs a body.
"""

from __future__ import annotations

import contextlib
import os
import time
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from onedata_mcp.api import readiness
from onedata_mcp.api.files import create_file, delete_file
from onedata_mcp.api.qos import get_file_qos_summary, remove_qos_requirement

ENABLE = "ONEDATA_MCP_LIVE_READINESS"
SPACE_ENV = "ONEDATA_MCP_LIVE_READINESS_SPACE"

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE) != "1", reason=f"live test: set {ENABLE}=1 (local Compose only)"
)


def is_local_host(host: str | None) -> bool:
    return bool(host) and (host in {"127.0.0.1", "localhost"} or host.endswith(".datahub.test"))


@pytest.fixture(autouse=True)
def _local_federation_only() -> None:
    host = urlparse(os.environ.get("ONEDATA_ONEPROVIDER_HOST", "")).hostname
    if not is_local_host(host):
        pytest.fail(
            "refusing to run: the live readiness test writes files and QoS rules, and runs "
            "only against 127.0.0.1, localhost or *.datahub.test "
            f"(ONEDATA_ONEPROVIDER_HOST host: {host!r})",
            pytrace=False,
        )


@pytest.fixture
def space() -> str:
    name = os.environ.get(SPACE_ENV, "").strip()
    if not name:
        pytest.fail(f"{SPACE_ENV} must name a space the local provider supports", pytrace=False)
    return name


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    readiness.reset_state()
    yield
    readiness.reset_state()


@pytest_asyncio.fixture
async def probe_file(space: str):
    """A small file written through the local provider, deleted afterwards with its rules."""
    name = f"readiness-live-probe-{int(time.time())}.txt"
    path = f"/{space}/{name}"
    await create_file(path, "readiness probe\n")
    yield name, path
    for rule_id in list(readiness._rules.values()):
        # cleanup must not hide the test's result
        with contextlib.suppress(Exception):
            await remove_qos_requirement(rule_id)
    await delete_file(path)


@pytest.mark.asyncio
async def test_a_file_written_here_is_ready(space: str, probe_file: tuple[str, str]) -> None:
    name, _ = probe_file

    result = await readiness.check_readiness([{"dataset": space, "files": [name]}])

    assert result["ready"] is True, result
    assert result["files"] == 1


@pytest.mark.asyncio
async def test_the_hold_expression_is_accepted_and_the_file_is_held(
    space: str, probe_file: tuple[str, str]
) -> None:
    name, path = probe_file

    result = await readiness.ensure_readiness([{"dataset": space, "files": [name]}], 60)

    assert result["ready"] is True, result
    assert result["held"]["files"] == 1, result
    # Which form Onedata took. The primary form is `providerId=<id> & anyStorage`; if it was
    # rejected the plain form is in use, and this line says so.
    print(f"hold expression in use: {result['held']['rule']!r}")
    local_id = await readiness._local_id()
    assert result["held"]["rule"] in {
        f"providerId={local_id} & anyStorage",
        f"providerId={local_id}",
    }
    (rule_id,) = readiness._rules.values()
    assert rule_id in (await get_file_qos_summary(path))["requirements"]
