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

Since the promotion package (design/17, design/18; not yet run, marked TODO(first live run)):

- a failure path of each tool: a declared file that is not there is an unresolved item with its
  hint, and a dataset above its space is refused before any request;
- the answer of ensure_readiness lists the ids of the holds it placed, each is the file's QoS
  requirement, and removing it by that id releases the hold (the cleanup uses those ids);
- since the external review: an ensure call that fails (a file that is not there, an empty
  selector) writes nothing: no rule is placed. It is aimed at a directory the test makes, never
  across the space (a name missing there might exist elsewhere in it).

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
from onedata_mcp.api.files import create_directory, create_file, delete_file
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
    assert result["resolved_files"] == 1


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


@pytest.mark.asyncio
async def test_a_file_that_is_not_there_is_unresolved_and_says_what_to_give(
    space: str, probe_file: tuple[str, str]
) -> None:
    """A failure path (gate 3). TODO(first live run): the text as 25.0 leads to it."""
    missing = "readiness-live-no-such-file.txt"

    result = await readiness.check_readiness([{"dataset": space, "files": [missing]}])

    print(f"unresolved: {result['unresolved']!r}")
    assert result["ready"] is False and result["resolved_files"] == 0
    assert result["unresolved"] == [
        f"file '{missing}' in '{space}' not found: give the exact file name, or its absolute path."
    ]


@pytest.mark.asyncio
async def test_a_dataset_above_its_space_is_refused_before_any_request(space: str) -> None:
    with pytest.raises(ValueError, match=r"empty, '\.' or '\.\.' component"):
        await readiness.check_readiness([{"dataset": f"/{space}/.."}])


@pytest.mark.asyncio
async def test_the_answer_lists_the_hold_ids_and_removing_one_releases_its_hold(
    space: str, probe_file: tuple[str, str]
) -> None:
    """TODO(first live run): the hold's id as the answer gives it, and its removal."""
    name, path = probe_file

    result = await readiness.ensure_readiness([{"dataset": space, "files": [name]}], 60)

    print(f"held: {result['held']!r}")
    (hold_id,) = result["held"]["qos_requirement_ids"]
    assert hold_id in (await get_file_qos_summary(path))["requirements"]
    await remove_qos_requirement(hold_id)
    readiness.reset_state()  # nothing left for the fixture's cleanup
    assert hold_id not in (await get_file_qos_summary(path))["requirements"]


@pytest_asyncio.fixture
async def owned_dir(space: str):
    """A directory this test makes, with one file in it, deleted afterwards with its rules: a
    failing ensure call is aimed here, never across the space (a name missing here might exist
    elsewhere in it, and would then be copied and held)."""
    directory = f"/{space}/readiness-live-dir-{int(time.time())}"
    present = f"{directory}/present.txt"
    await create_directory(directory)
    await create_file(present, "readiness probe\n")
    yield directory, present
    for rule_id in list(readiness._rules.values()):
        with contextlib.suppress(Exception):  # cleanup must not hide the test's result
            await remove_qos_requirement(rule_id)
    with contextlib.suppress(Exception):
        await delete_file(present)
    await delete_file(directory)


@pytest.mark.asyncio
async def test_an_ensure_that_resolves_nothing_writes_nothing(owned_dir: tuple[str, str]) -> None:
    """A failure path of ensure_readiness (gate 3; the external review, Codex 14; the
    verification round: inside a directory the test owns). TODO(first live run): the answer as
    25.0 leads to it, and no rule on the directory's file."""
    directory, present = owned_dir
    before = (await get_file_qos_summary(present))["requirements"]
    missing = "readiness-live-no-such-file.txt"

    result = await readiness.ensure_readiness([{"dataset": directory, "files": [missing]}], 30)

    print(f"answer: {result!r}")
    assert result["ready"] is False and result["data_ready"] is False
    assert result["resolved_files"] == 0
    assert result["held"] == {
        "files": 0,
        "rule": result["held"]["rule"],
        "qos_requirement_ids": [],
    }
    assert readiness._rules == {}
    assert (await get_file_qos_summary(present))["requirements"] == before

    with pytest.raises(ValueError, match=r"extent\[0\]\.files is empty"):
        await readiness.ensure_readiness([{"dataset": directory, "files": []}], 30)
    assert (await get_file_qos_summary(present))["requirements"] == before
