"""Live test of `schedule_file_replication` against the LOCAL Compose federation.

Skipped unless ONEDATA_MCP_LIVE_TRANSFER=1. With it set, the test REFUSES to run (it fails)
unless every host it is given (the Oneprovider, the Onezone when one is set, and the source
provider) is `127.0.0.1`, `localhost` or ends in `.datahub.test` AND resolves only to loopback
addresses (a `*.datahub.test` name mapped to any other address is refused): it writes files and
schedules transfers, and must never touch production. Once the check passes, name resolution
for those hosts, the source provider's included, is pinned to the addresses that were checked
and proxy variables are ignored, so no connection can go anywhere else (the guard is shared
with the other live tests: test/live/local_federation.py).

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
  file and for the larger one SR1 uses. Each poll reads the transfer's status, then the block
  map, and the test prints what SR1's file size is calibrated from: the time to whole, how long
  the file was seen partly copied, the throughput, the first status that read completed, and
  whether a poll ever showed completed while the blocks did NOT yet cover the file;
- a second schedule of the same file, while the first runs (printed: Onedata's answer);
- an unknown target provider fails with the provider hint and copies nothing (printed: the
  status and Onedata's error, which the unit tests do not pin);
- the local provider's NAME passed as the id is answered "Pass its id ...".

The timings are of one transfer on an idle federation. SR1's file size is calibrated on the
first live run, under panel-like load (design/12, gate 3). Not run in the change that adds this
test: the federation was on hold. Its results go into
design/12-promotion-schedule-file-replication.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import time
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP

from benchmark.k2val.federation import FedApi, provider_config
from benchmark.k2val.fixtures import FILE_SIZE, LARGE_FILE_SIZE, PART
from onedata_mcp.modules import spaces as spaces_module
from onedata_mcp.modules import transfers as transfers_module
from test.live.local_federation import enforce_local_federation

ENABLE = "ONEDATA_MCP_LIVE_TRANSFER"
SPACE_ENV = "ONEDATA_MCP_LIVE_TRANSFER_SPACE"
SOURCE_HOST_ENV = "ONEDATA_MCP_LIVE_TRANSFER_SOURCE_HOST"
SOURCE_TOKEN_ENV = "ONEDATA_MCP_LIVE_TRANSFER_SOURCE_TOKEN"
TOOL = "schedule_file_replication"
WHAT = "the live transfer test writes files and schedules transfers"
# The panel's standard file and the larger one SR1 uses, so that a transfer is still running
# when a model answers (design/12, gate 4). Written in PART-sized parts at explicit offsets.
SIZES = (FILE_SIZE, LARGE_FILE_SIZE)
SEES_TIMEOUT_S = 120.0
WHOLE_TIMEOUT_S = 600.0
NOTHING_COPIED_WAIT_S = 20.0
POLL_S = 0.5
# Status fields of `GET /transfers/{id}`, in the order they are trusted: the 25.0 answer's
# `transferState` first (the API's own docstring), then the swagger's names.
STATUS_FIELDS = ("transferState", "transferStatus", "replicationStatus", "effectiveJobStatus")

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE) != "1", reason=f"live test: set {ENABLE}=1 (local Compose only)"
)


def federation_urls() -> dict[str, str]:
    """Every host URL the test connects to: the agent's provider, the Onezone when one is set,
    and the SOURCE provider, where the probe files are written."""
    urls = {
        "ONEDATA_ONEPROVIDER_HOST": os.environ.get("ONEDATA_ONEPROVIDER_HOST", ""),
        SOURCE_HOST_ENV: os.environ.get(SOURCE_HOST_ENV, ""),
    }
    if os.environ.get("ONEDATA_ONEZONE_HOST"):
        urls["ONEDATA_ONEZONE_HOST"] = os.environ["ONEDATA_ONEZONE_HOST"]
    return urls


def enforce_local(monkeypatch: pytest.MonkeyPatch, resolve=socket.getaddrinfo) -> None:  # noqa: ANN001
    """The shared guard over every host this test uses (`resolve` is for the unit tests)."""
    enforce_local_federation(monkeypatch, federation_urls(), resolve, what=WHAT)


@pytest.fixture(autouse=True)
def _local_federation_only(monkeypatch: pytest.MonkeyPatch) -> None:
    enforce_local(monkeypatch)


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
    spaces_module.register_module(mcp)
    return mcp


@pytest_asyncio.fixture
async def probes(fed: FedApi):
    """Files the test made, registered the moment each is created (before any write), and
    deleted afterwards through the source provider, even when a write or a wait failed."""
    made: list[str] = []
    yield made
    for file_id in made:
        with contextlib.suppress(Exception):  # cleanup must not hide the test's result
            await fed.delete("owner", file_id)


async def _write_at_source(
    fed: FedApi, space: str, size: int, probes: list[str]
) -> tuple[str, str]:
    """A file of `size` random bytes written through the source provider, in parts at explicit
    offsets; returned once the local provider resolves it at its size and holds none of it."""
    root = await fed.lookup("owner", f"/{space}")
    assert root is not None, f"space {space} not found at the source provider"
    name = f"transfer-live-probe-{int(time.time())}-{secrets.token_hex(3)}.dat"
    file_id = await fed.create_reg("owner", root, name)
    probes.append(file_id)  # before the writes, so a failed write leaves nothing behind
    for offset in range(0, size, PART):
        await fed.put("owner", file_id, secrets.token_bytes(min(PART, size - offset)), offset)
    path = f"/{space}/{name}"
    local_id = await fed.provider_id("local")

    async def local_sees_it_and_holds_none() -> bool:
        if await fed.lookup("local", path) != file_id:
            return False
        dist = await fed.distribution("local", file_id)
        return _local_at(dist, local_id, size) and dist.bytes_of(local_id) == 0

    await fed.wait_until(
        local_sees_it_and_holds_none,
        timeout_s=SEES_TIMEOUT_S,
        what=f"the local provider resolves {path} at {size} bytes and holds none of it",
    )
    return path, file_id


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
    for key in STATUS_FIELDS:
        if isinstance(transfer.get(key), str):
            return f"{key}={transfer[key]}"
    return "no status field"


def _local_at(dist, local_id: str, size: int) -> bool:
    """The local provider's OWN entry is readable and gives the expected size: only then do its
    bytes mean anything (an unreadable entry is not "0 bytes", another entry's size not its)."""
    return dist.readable(local_id) and dist.size_of(local_id) == size


@dataclass
class Poll:
    """One poll: the transfer's status, read first, then the local block map, read after it."""

    at: float  # seconds after the schedule, when the block map had been read
    status: str
    local_bytes: int
    covered: bool
    # the local entry was readable at the expected size; an unreadable poll is no evidence
    local_readable: bool = True


@dataclass
class Timeline:
    size: int
    polls: list[Poll] = field(default_factory=list)

    @property
    def readable(self) -> list[Poll]:
        """The polls that count as evidence: the local entry readable at the expected size."""
        return [p for p in self.polls if p.local_readable]

    @property
    def whole_at(self) -> float | None:
        return next((p.at for p in self.readable if p.covered), None)

    @property
    def partial(self) -> list[Poll]:
        return [p for p in self.readable if 0 < p.local_bytes < self.size]

    @property
    def first_completed(self) -> Poll | None:
        return next((p for p in self.polls if p.status.endswith("=completed")), None)

    @property
    def completed_before_whole(self) -> list[Poll]:
        """Polls whose status read completed while the block map, read AFTER that status on the
        same poll, did not yet cover the file: the status certainly came first."""
        return [p for p in self.readable if p.status.endswith("=completed") and not p.covered]

    def report(self) -> list[str]:
        whole = self.whole_at
        lines = [
            f"[{self.size} B] polls: {len(self.polls)}, every {POLL_S} s, "
            f"{len(self.polls) - len(self.readable)} unreadable (not counted)"
        ]
        if whole is not None:
            lines.append(
                f"[{self.size} B] whole at the local provider {whole:.2f} s after the schedule "
                f"(~{self.size / whole / 2**20:.1f} MiB/s including scheduling)"
            )
        if self.partial:
            first, last = self.partial[0], self.partial[-1]
            lines.append(
                f"[{self.size} B] seen partly copied on {len(self.partial)} polls, from "
                f"{first.at:.2f} s ({first.local_bytes} B) to {last.at:.2f} s "
                f"({last.local_bytes} B)"
            )
        else:
            lines.append(f"[{self.size} B] never seen partly copied: 0 bytes, then whole")
        done = self.first_completed
        lines.append(
            f"[{self.size} B] first status completed: "
            + (f"at {done.at:.2f} s" if done else "never seen")
        )
        early = self.completed_before_whole
        lines.append(
            f"[{self.size} B] completed while NOT yet whole on the same poll: "
            + (f"YES, on {len(early)} polls, first at {early[0].at:.2f} s" if early else "no")
        )
        return lines


async def _follow(
    mcp: FastMCP, fed: FedApi, file_id: str, transfer_id: str, size: int, began: float
) -> Timeline:
    """Poll until the local block map covers the file: on each poll the status first, then the
    block map, so that "completed while not whole" can only mean the status came first."""
    local_id = await fed.provider_id("local")
    timeline = Timeline(size)
    while True:
        result, text = await _call(mcp, "get_transfer", {"transfer_id": transfer_id})
        status = _status(_payload(result, text)) if result.is_error is False else "unreadable"
        dist = await fed.distribution("local", file_id)  # the judge: the local block map
        at = time.monotonic() - began
        readable = _local_at(dist, local_id, size)
        poll = Poll(
            at,
            status,
            dist.bytes_of(local_id) if readable else -1,
            readable and dist.covers(local_id),
            readable,
        )
        timeline.polls.append(poll)
        if poll.covered:
            return timeline
        assert poll.at < WHOLE_TIMEOUT_S, (
            f"not whole at the local provider after {WHOLE_TIMEOUT_S:.0f} s "
            f"({poll.local_bytes} of {size} bytes); last status {status}"
        )
        await asyncio.sleep(POLL_S)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s // 1024}KiB")
async def test_a_replication_to_the_local_provider_makes_the_file_whole_there(
    transfer_mcp: FastMCP, fed: FedApi, space: str, probes: list[str], size: int
) -> None:
    path, file_id = await _write_at_source(fed, space, size, probes)
    local_id = await fed.provider_id("local")

    began = time.monotonic()
    result, text = await _call(
        transfer_mcp, TOOL, {"file_id_or_path": path, "target_provider_id": local_id}
    )
    assert result.is_error is False, text
    transfer_id = _payload(result, text)["transferId"]

    timeline = await _follow(transfer_mcp, fed, file_id, transfer_id, size, began)

    for line in timeline.report():
        print(line)
    statuses = list(dict.fromkeys(p.status for p in timeline.polls))
    print(f"[{size} B] statuses seen, in order: {statuses}")
    assert timeline.whole_at is not None


@pytest.mark.asyncio
async def test_a_second_schedule_of_a_file_being_copied(
    transfer_mcp: FastMCP, fed: FedApi, space: str, probes: list[str]
) -> None:
    """Onedata is not expected to refuse it (the swagger lists no status for it); what it
    answers is printed, and the file must still end up whole."""
    path, file_id = await _write_at_source(fed, space, LARGE_FILE_SIZE, probes)
    local_id = await fed.provider_id("local")
    arguments = {"file_id_or_path": path, "target_provider_id": local_id}

    began = time.monotonic()
    first, first_text = await _call(transfer_mcp, TOOL, arguments)
    assert first.is_error is False, first_text
    again, again_text = await _call(transfer_mcp, TOOL, arguments)
    print(f"second schedule of a file being copied: error={again.is_error}: {again_text}")

    transfer_id = _payload(first, first_text)["transferId"]
    timeline = await _follow(transfer_mcp, fed, file_id, transfer_id, LARGE_FILE_SIZE, began)
    print(f"with a second schedule, whole after {timeline.whole_at:.2f} s")


@pytest.mark.asyncio
async def test_an_unknown_target_provider_fails_with_the_provider_hint_and_copies_nothing(
    transfer_mcp: FastMCP, fed: FedApi, space: str, probes: list[str]
) -> None:
    path, file_id = await _write_at_source(fed, space, FILE_SIZE, probes)
    unknown = secrets.token_hex(16)

    result, text = await _call(
        transfer_mcp, TOOL, {"file_id_or_path": path, "target_provider_id": unknown}
    )

    assert result.is_error is True
    print(f"unknown target provider answered: {text}")
    assert "Not allowed" not in text, text  # not the permission hint
    assert "No file is known" not in text, text  # the path resolved: not the file's fault
    assert any(
        needle in text
        for needle in (
            "no provider with that id supports",
            "is not a valid provider id",
            "so check the target",
            "Check both arguments",
        )
    ), text
    await asyncio.sleep(NOTHING_COPIED_WAIT_S)
    local_id = await fed.provider_id("local")
    dist = await fed.distribution("local", file_id)
    assert _local_at(dist, local_id, FILE_SIZE), "the local entry is unreadable: no evidence"
    assert dist.bytes_of(local_id) == 0


@pytest.mark.asyncio
async def test_the_local_providers_name_as_the_id_is_answered_with_its_id(
    transfer_mcp: FastMCP, fed: FedApi, space: str, probes: list[str]
) -> None:
    path, _ = await _write_at_source(fed, space, FILE_SIZE, probes)
    local_id = await fed.provider_id("local")
    # the name as the space's providers list it, the source the tool's hint reads too
    listed, listed_text = await _call(transfer_mcp, "list_space_providers", {"space_id": space})
    assert listed.is_error is False, listed_text
    names = {
        p.get("providerId"): p.get("providerName")
        for p in _payload(listed, listed_text).get("providers", [])
    }
    local_name = names.get(local_id)
    assert local_name, f"no name for the local provider {local_id} in {names}"

    result, text = await _call(
        transfer_mcp, TOOL, {"file_id_or_path": path, "target_provider_id": local_name}
    )

    assert result.is_error is True
    print(f"the local provider's name as the id answered: {text}")
    assert f"Pass its id {local_id!r}, not its name: {local_name!r}" in text, text
