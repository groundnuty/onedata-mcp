"""What the oracles read must be usable, and a trial must touch only what it made.

- a snapshot never reads an unreadable QoS summary or block map as "none" or "0 bytes";
- the fixture waits for each file's expected start state at the local provider, and records it;
- teardown deletes the rules it made and rules set ON the trial's own entries, never a rule a
  file merely inherits (from the space root, say);
- the trial directory a brief shows is opaque: it does not name the scenario.
"""

from __future__ import annotations

import re

import httpx
import pytest

from benchmark.k2val.federation import FedApi, FixtureError
from benchmark.k2val.fixtures import FILE_SIZE, FixtureBuilder
from benchmark.k2val.runner import make_trial_id, trial_dir_name
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.snapshot import take_snapshot
from test.unit.k2val.fakefed import LOCAL_ID, OWNER_ID, FakeClock, FakeFed, error

SPACE = "k2val-glm"


def builder(api: FedApi, clock: FakeClock) -> FixtureBuilder:
    return FixtureBuilder(api, SPACE, clock)


def root_rule(fed: FakeFed) -> str:
    """A rule set on the space root: every file below inherits it in its effective summary."""
    qid = fed.new_id("qroot")
    fed.rules[qid] = {
        "fileId": fed.roots[SPACE],
        "expression": "anyStorage",
        "replicasNum": 1,
        "status": "fulfilled",
        "ticks": 0,
    }
    return qid


# --- snapshots ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_404_qos_summary_is_a_snapshot_error_not_no_requirements(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-a")
    monkeypatch.setattr(fed, "_qos_summary", lambda fid: error(404))

    snap = await take_snapshot(api, built, clock)

    (state,) = snap.files.values()
    assert state.error and "404" in state.error
    assert snap.errors()  # the oracles refuse to judge it


@pytest.mark.asyncio
async def test_a_qos_summary_without_requirements_is_a_snapshot_error(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-b")
    monkeypatch.setattr(fed, "_qos_summary", lambda fid: httpx.Response(200, json={}))

    snap = await take_snapshot(api, built, clock)

    assert "requirements" in (next(iter(snap.files.values())).error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"type": "REG"},  # no per-provider map at all
        {"distributionPerProvider": {LOCAL_ID: {"success": False}, OWNER_ID: {"success": False}}},
    ],
)
async def test_an_unreadable_block_map_is_a_snapshot_error_not_zero_bytes(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, body: dict
) -> None:
    built = await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-c")
    monkeypatch.setattr(fed, "_distribution", lambda provider, fid: httpx.Response(200, json=body))

    snap = await take_snapshot(api, built, clock)

    (state,) = snap.files.values()
    assert state.error and "distribution" in state.error
    assert state.local_bytes == 0 and not state.local_covered  # never judged on these


# --- the baseline ------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "kind", "local_bytes", "qos"),
    [("V2", "s0", 0, 0), ("V3n", "s0_local", FILE_SIZE, 0), ("V3", "s0_qos", FILE_SIZE, 1)],
)
async def test_the_start_state_of_each_file_is_waited_for_and_recorded(
    api: FedApi,
    fed: FakeFed,
    clock: FakeClock,
    scenario: str,
    kind: str,
    local_bytes: int,
    qos: int,
) -> None:
    built = await builder(api, clock).build(SCENARIOS[scenario].fixture, "logs-d")

    ((name, baseline),) = built.files_baseline.items()
    assert baseline["kind"] == kind
    assert baseline["size"] == FILE_SIZE and baseline["size_known"] is True
    assert baseline["local_bytes"] == local_bytes
    assert len(baseline["qos_ids"]) == qos
    assert baseline["qos_ids"] == list(built.qos_rules.values())


@pytest.mark.asyncio
async def test_a_file_that_does_not_start_as_built_is_a_fixture_error(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    """A rule left on the space root makes every new file's effective summary non-empty: the
    trial does not start on that state (a RESET_FAIL, a deployment artefact)."""
    root_rule(fed)

    with pytest.raises(FixtureError, match="as its kind s0 builds it"):
        await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-e")


@pytest.mark.asyncio
async def test_a_summary_that_is_not_readable_yet_is_waited_out(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fed._qos_summary
    misses = [3]

    def flaky(fid: str) -> httpx.Response:
        if misses[0] > 0:
            misses[0] -= 1
            return error(404)
        return original(fid)

    monkeypatch.setattr(fed, "_qos_summary", flaky)

    built = await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-f")

    assert built.files_baseline["log_20200105.dat"]["qos_ids"] == []
    assert misses == [0]


# --- teardown ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_never_deletes_a_rule_the_files_only_inherit(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    b = builder(api, clock)
    built = await b.build(SCENARIOS["V3"].fixture, "logs-g")  # one rule, made by the fixture
    (made,) = built.qos_rules.values()
    inherited = root_rule(fed)  # e.g. left by someone else, after the build
    (bf,) = built.files.values()
    assert set(await api.qos_ids("local", bf.file_id)) == {made, inherited}

    assert await b.teardown(built) == []

    assert made not in fed.rules
    assert inherited in fed.rules  # listed in the files' summaries, but not theirs
    assert fed.find(built.dir_path) is None


@pytest.mark.asyncio
async def test_teardown_deletes_a_rule_set_on_one_of_the_trials_own_files(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    """An agent's rule on the trial's file: its own `fileId` is the file, so it goes."""
    b = builder(api, clock)
    built = await b.build(SCENARIOS["V2"].fixture, "logs-h")
    (bf,) = built.files.values()
    agents = await api.add_qos("local", bf.file_id, f"providerId={LOCAL_ID}")

    assert await b.teardown(built) == []

    assert agents not in fed.rules


@pytest.mark.asyncio
async def test_the_sweep_also_leaves_inherited_rules_alone(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    b = builder(api, clock)
    left = await b.build(SCENARIOS["V3"].fixture, "logs-old")
    (made,) = left.qos_rules.values()
    inherited = root_rule(fed)

    assert await b.sweep_space() == []

    assert made not in fed.rules and inherited in fed.rules
    assert fed.find(left.dir_path) is None


# --- the directory a brief shows ---------------------------------------------------------------


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_the_trial_directory_does_not_name_the_scenario(sid: str) -> None:
    name = trial_dir_name(make_trial_id("20261120T000000Z", "glm", sid, 0))
    assert re.fullmatch(r"logs-[0-9a-f]{12}", name)
    assert sid.lower() not in name


def test_variants_of_one_trial_get_different_directories() -> None:
    names = {trial_dir_name(make_trial_id("r1", "glm", sid, 0)) for sid in ("V3", "V3n", "V3o")}
    assert len(names) == 3
