"""Live test of `schedule_file_replication` against the LOCAL Compose federation.

Skipped unless ONEDATA_MCP_LIVE_TRANSFER=1. With it set, the test REFUSES to run (it fails)
unless every host it is given (the Oneprovider, the Onezone when one is set, and the source
provider) is `127.0.0.1`, `localhost` or ends in `.datahub.test`: it writes files and schedules
transfers, and must never touch production.

    make test-transfer-live      the variables below, set by hand
    make valfed-transfer-live    against the project's own federation, in its space omcp-live

Environment:

    ONEDATA_ONEPROVIDER_HOST, ONEDATA_ONEPROVIDER_TOKEN   the agent's site (the MCP server's)
    ONEDATA_ONEZONE_HOST, ONEDATA_ONEZONE_TOKEN
    ONEDATA_MCP_LIVE_TRANSFER_SPACE          a space both providers support
    ONEDATA_MCP_LIVE_TRANSFER_SOURCE_HOST    ANOTHER provider of that space, where the probe
    ONEDATA_MCP_LIVE_TRANSFER_SOURCE_TOKEN   files are written (a replication to the agent's
                                             site of a file written there would copy nothing)

The tools are called through an in-process MCP client, so what is checked is what a model gets.
Whether the copy is whole is judged by the local provider's own block map of the file, NEVER by
the transfer's status. What it settles (unit tests use fakes; the swagger gives only statuses):

- a replication to the local provider makes the file whole there, for the panel's standard
  file and for the larger one the panel's copy scenarios use (printed: the time from schedule
  to whole, and when the transfer first read completed: before or after the blocks were whole);
- a second schedule of the same file, while the first runs (printed: Onedata's answer);
- an unknown target provider fails with the provider hint and copies nothing (printed: the
  status and Onedata's error, which the unit tests do not pin);
- the local provider's NAME passed as the id is answered with its id.

Not run in the change that adds it: the federation was on hold. Its results go into
design/12-promotion-schedule-file-replication.md.
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

from benchmark.k2val.federation import FedApi, provider_config
from benchmark.k2val.fixtures import FILE_SIZE, LARGE_FILE_SIZE, PART
from onedata_mcp.modules import files as files_module
from onedata_mcp.modules import transfers as transfers_module

ENABLE = "ONEDATA_MCP_LIVE_TRANSFER"
SPACE_ENV = "ONEDATA_MCP_LIVE_TRANSFER_SPACE"
SOURCE_HOST_ENV = "ONEDATA_MCP_LIVE_TRANSFER_SOURCE_HOST"
SOURCE_TOKEN_ENV = "ONEDATA_MCP_LIVE_TRANSFER_SOURCE_TOKEN"
TOOL = "schedule_file_replication"
# The panel's standard file and the larger one SR1 uses, so that a transfer is still running
# when a model answers (design/12, gate 4). Written in PART-sized parts at explicit offsets.
SIZES = (FILE_SIZE, LARGE_FILE_SIZE)
SEES_TIMEOUT_S = 120.0
WHOLE_TIMEOUT_S = 600.0
NOTHING_COPIED_WAIT_S = 20.0
POLL_S = 0.5

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE) != "1", reason=f"live test: set {ENABLE}=1 (local Compose only)"
)


def is_local_host(host: str | None) -> bool:
    return bool(host) and (host in {"127.0.0.1", "localhost"} or host.endswith(".datahub.test"))


@pytest.fixture(autouse=True)
def _local_federation_only() -> None:
    hosts = {
        "ONEDATA_ONEPROVIDER_HOST": os.environ.get("ONEDATA_ONEPROVIDER_HOST", ""),
        SOURCE_HOST_ENV: os.environ.get(SOURCE_HOST_ENV, ""),
    }
    if os.environ.get("ONEDATA_ONEZONE_HOST"):
        hosts["ONEDATA_ONEZONE_HOST"] = os.environ["ONEDATA_ONEZONE_HOST"]
    for name, url in hosts.items():
        host = urlparse(url).hostname
        if not is_local_host(host):
            pytest.fail(
                "refusing to run: the live transfer test writes files and schedules transfers, "
                f"and runs only against 127.0.0.1, localhost or *.datahub.test ({name} host: "
                f"{host!r})",
                pytrace=False,
            )


@pytest.fixture
def space() -> str:
    name = os.environ.get(SPACE_ENV, "").strip()
    if not name:
        pytest.fail(f"{SPACE_ENV} must name a space both providers support", pytrace=False)
    return name


@pytest.fixture
def fed() -> FedApi:
    """`local` is the agent's site (the MCP server's provider); `owner` is the source provider."""
    token = os.environ.get(SOURCE_TOKEN_ENV, "").strip()
    if not token:
        pytest.fail(f"{SOURCE_TOKEN_ENV} must hold a token for the source provider", pytrace=False)
    return FedApi(
        provider_config(
            os.environ["ONEDATA_ONEPROVIDER_HOST"],
            os.environ["ONEDATA_ONEPROVIDER_TOKEN"],
            insecure_tls=False,
        ),
        provider_config(os.environ[SOURCE_HOST_ENV], token, insecure_tls=False),
        poll_s=POLL_S,
    )


@pytest.fixture
def transfer_mcp() -> FastMCP:
    mcp = FastMCP(name="onedata-mcp-live-transfer")
    transfers_module.register_module(mcp)
    files_module.register_module(mcp)
    return mcp


async def _write_at_source(fed: FedApi, space: str, size: int) -> tuple[str, str]:
    """A file of `size` random bytes written through the source provider, in parts at explicit
    offsets; returned once the local provider resolves it at its size and holds none of it."""
    root = await fed.lookup("owner", f"/{space}")
    assert root is not None, f"space {space} not found at the source provider"
    name = f"transfer-live-probe-{int(time.time())}-{secrets.token_hex(3)}.dat"
    file_id = await fed.create_reg("owner", root, name)
    for offset in range(0, size, PART):
        await fed.put("owner", file_id, secrets.token_bytes(min(PART, size - offset)), offset)
    path = f"/{space}/{name}"

    async def local_sees_it_and_holds_none() -> bool:
        if await fed.lookup("local", path) != file_id:
            return False
        dist = await fed.distribution("local", file_id)
        return dist.size == size and dist.size_known and dist.bytes_of(await _local(fed)) == 0

    await fed.wait_until(
        local_sees_it_and_holds_none,
        timeout_s=SEES_TIMEOUT_S,
        what=f"the local provider resolves {path} at {size} bytes and holds none of it",
    )
    return path, file_id


async def _local(fed: FedApi) -> str:
    return await fed.provider_id("local")


@pytest_asyncio.fixture
async def probes(fed: FedApi):
    """Files written by the test, deleted afterwards through the source provider."""
    made: list[str] = []
    yield made
    for file_id in made:
        with contextlib.suppress(Exception):  # cleanup must not hide the test's result
            await fed.delete("owner", file_id)


async def _call(mcp: FastMCP, tool: str, arguments: dict):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    return result, text


def _payload(result, text: str) -> dict:
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(text)


def _status(transfer: dict) -> str:
    for key in ("transferStatus", "replicationStatus", "transferState", "effectiveJobStatus"):
        if isinstance(transfer.get(key), str):
            return f"{key}={transfer[key]}"
    return "no status field"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s // 1024}KiB")
async def test_a_replication_to_the_local_provider_makes_the_file_whole_there(
    transfer_mcp: FastMCP, fed: FedApi, space: str, probes: list[str], size: int
) -> None:
    path, file_id = await _write_at_source(fed, space, size)
    probes.append(file_id)
    local_id = await _local(fed)

    began = time.monotonic()
    result, text = await _call(
        transfer_mcp, TOOL, {"file_id_or_path": path, "target_provider_id": local_id}
    )
    assert result.is_error is False, text
    transfer_id = _payload(result, text)["transferId"]

    # a second schedule of the same file while the first runs: whatever Onedata answers
    again, again_text = await _call(
        transfer_mcp, TOOL, {"file_id_or_path": path, "target_provider_id": local_id}
    )
    print(f"[{size} B] second schedule of the same file: error={again.is_error}: {again_text}")

    completed_at: float | None = None
    statuses: list[str] = []
    while True:
        elapsed = time.monotonic() - began
        transfer_result, transfer_text = await _call(
            transfer_mcp, "get_transfer", {"transfer_id": transfer_id}
        )
        if transfer_result.is_error is False:
            status = _status(_payload(transfer_result, transfer_text))
            if not statuses or statuses[-1] != status:
                statuses.append(status)
                print(f"[{size} B] {elapsed:6.2f} s transfer {status}")
            if completed_at is None and status.endswith("=completed"):
                completed_at = elapsed
        dist = await fed.distribution("local", file_id)  # the judge: the local block map
        if dist.covers(local_id):
            whole_at = time.monotonic() - began
            break
        assert elapsed < WHOLE_TIMEOUT_S, (
            f"not whole at the local provider after {WHOLE_TIMEOUT_S:.0f} s "
            f"({dist.bytes_of(local_id)} of {size} bytes); transfer statuses {statuses}"
        )
        await asyncio.sleep(POLL_S)

    order = (
        "never read completed before the blocks were whole"
        if completed_at is None
        else f"read completed at {completed_at:.2f} s, "
        + ("BEFORE" if completed_at < whole_at else "not before")
        + " the blocks were whole"
    )
    print(f"[{size} B] whole at the local provider after {whole_at:.2f} s; the transfer {order}")
    print(f"[{size} B] statuses seen: {statuses}")


@pytest.mark.asyncio
async def test_an_unknown_target_provider_fails_with_the_provider_hint_and_copies_nothing(
    transfer_mcp: FastMCP, fed: FedApi, space: str, probes: list[str]
) -> None:
    path, file_id = await _write_at_source(fed, space, SIZES[0])
    probes.append(file_id)
    unknown = secrets.token_hex(16)

    result, text = await _call(
        transfer_mcp, TOOL, {"file_id_or_path": path, "target_provider_id": unknown}
    )

    assert result.is_error is True
    print(f"unknown target provider answered: {text}")
    assert "Not allowed" not in text, text  # not the permission hint
    assert "Cannot replicate to" in text or "Check both arguments" in text, text
    await asyncio.sleep(NOTHING_COPIED_WAIT_S)
    assert (await fed.distribution("local", file_id)).bytes_of(await _local(fed)) == 0


@pytest.mark.asyncio
async def test_the_local_providers_name_as_the_id_is_answered_with_its_id(
    transfer_mcp: FastMCP, fed: FedApi, space: str, probes: list[str]
) -> None:
    path, file_id = await _write_at_source(fed, space, SIZES[0])
    probes.append(file_id)
    local_id = await _local(fed)
    providers = await _call(transfer_mcp, "get_file_distribution", {"file_id_or_path": path})
    names = {
        pid: entry.get("providerName")
        for pid, entry in _payload(*providers).get("distributionPerProvider", {}).items()
    }
    local_name = names.get(local_id)
    assert local_name, f"no name for the local provider in {names}"

    result, text = await _call(
        transfer_mcp, TOOL, {"file_id_or_path": path, "target_provider_id": local_name}
    )

    assert result.is_error is True
    print(f"the local provider's name as the id answered: {text}")
    assert f"{local_name!r} is a provider's name; its id is {local_id!r}" in text, text
