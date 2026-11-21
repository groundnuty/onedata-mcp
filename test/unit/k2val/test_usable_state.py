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


def _entry(size: int, storages: object) -> dict:
    return {"success": True, "virtualSize": size, "distributionPerStorageBackend": storages}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local_storages", "what"),
    [
        ({"st": {"success": False, "error": {}}}, "own entry"),  # a storage failed
        (None, "own entry"),  # no storage map
        ({"st": {"success": True}}, "own entry"),  # blocks not a list
    ],
)
async def test_a_failed_storage_of_a_provider_is_a_snapshot_error_not_zero_bytes(
    api: FedApi,
    fed: FakeFed,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    local_storages: object,
    what: str,
) -> None:
    built = await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-s")
    body = {
        "distributionPerProvider": {
            OWNER_ID: _entry(FILE_SIZE, {"so": {"success": True, "blocks": [[0, FILE_SIZE]]}}),
            LOCAL_ID: _entry(FILE_SIZE, local_storages),
        }
    }
    monkeypatch.setattr(fed, "_distribution", lambda provider, fid: httpx.Response(200, json=body))

    snap = await take_snapshot(api, built, clock)

    (state,) = snap.files.values()
    assert state.error and what in state.error and "local" in state.error


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, FILE_SIZE - 1])
async def test_a_size_other_than_the_one_written_is_a_snapshot_error(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """A provider that answers size 0 with no blocks must not read as "covered" (0 >= 0)."""
    built = await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-z")
    body = {
        "distributionPerProvider": {
            OWNER_ID: _entry(size, {"so": {"success": True, "blocks": []}}),
            LOCAL_ID: _entry(size, {"sl": {"success": True, "blocks": []}}),
        }
    }
    monkeypatch.setattr(fed, "_distribution", lambda provider, fid: httpx.Response(200, json=body))

    snap = await take_snapshot(api, built, clock)

    (state,) = snap.files.values()
    assert state.error and f"size {size}, not the {FILE_SIZE} written" in state.error
    assert not state.local_covered


def test_a_zero_size_covers_nothing() -> None:
    from benchmark.k2val.federation import parse_dist

    view = parse_dist(
        {"distributionPerProvider": {LOCAL_ID: _entry(0, {"s": {"success": True, "blocks": []}})}}
    )
    assert view.size_known and not view.covers(LOCAL_ID)


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

    ((name, baseline),) = built.baseline()["files"].items()
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

    assert built.baseline()["files"]["log_20200105.dat"]["qos_ids"] == []
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
    # a rule attached to the space ROOT is debris every later file would inherit: it goes
    # (round 2); a rule attached anywhere else stays (below)
    assert inherited not in fed.rules
    assert fed.find(built.dir_path) is None


@pytest.mark.asyncio
async def test_teardown_leaves_a_rule_attached_to_another_directory(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    b = builder(api, clock)
    sibling = await b.build(SCENARIOS["V2"].fixture, "logs-sib")
    built = await b.build(SCENARIOS["V3"].fixture, "logs-g2")
    elsewhere = await api.add_qos("local", sibling.dir_id, f"providerId={LOCAL_ID}")

    assert await b.teardown(built) == []

    assert elsewhere in fed.rules  # attached to a sibling trial's directory


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
async def test_the_sweep_removes_the_roots_rules_and_the_old_trials(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    b = builder(api, clock)
    left = await b.build(SCENARIOS["V3"].fixture, "logs-old")
    (made,) = left.qos_rules.values()
    inherited = root_rule(fed)

    assert await b.sweep_space() == []

    assert made not in fed.rules and inherited not in fed.rules
    assert fed.find(left.dir_path) is None


# --- the directory a brief shows ---------------------------------------------------------------


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_the_trial_directory_does_not_name_the_scenario(sid: str) -> None:
    name = trial_dir_name(make_trial_id("20261120T000000Z", "glm", sid, 0))
    assert re.fullmatch(r"logs-[a-p]{12}", name)
    assert sid.lower() not in name


def test_variants_of_one_trial_get_different_directories() -> None:
    names = {trial_dir_name(make_trial_id("r1", "glm", sid, 0)) for sid in ("V3", "V3n", "V3o")}
    assert len(names) == 3


# --- V3n starts from a confirmed summary with no rule at all (the review's fourth blocker) -----


@pytest.mark.asyncio
async def test_v3n_with_an_inherited_rule_never_dispatches(
    cfg, api: FedApi, fed: FakeFed, clock: FakeClock, tmp_path
) -> None:
    from benchmark.k2val.runner import run_trial
    from test.unit.k2val.test_runner import make_deps

    inherited = root_rule(fed)
    dispatched = []

    def script_for(conf, on_cost):
        async def script(app, brief):
            dispatched.append(brief)
            return "There is no placement rule."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for)
    assert cfg.models["glm"].space == SPACE  # the space whose root carries the rule
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3n"], 0)

    assert art.outcome == "RESET_FAIL" and "QoS requirements []" in art.error
    assert dispatched == []  # the model never saw a V3n with a rule on it
    assert inherited not in fed.rules  # the failed build's cleanup removed the root's debris


def test_v3n_waits_for_an_empty_effective_summary() -> None:
    from benchmark.k2val.fixtures import _baseline_holds

    reading = {
        "size_known": True,
        "size": FILE_SIZE,
        "local_bytes": FILE_SIZE,
        "local_covered": True,
        "qos_ids": ["q-inherited"],
    }
    assert not _baseline_holds("s0_local", reading, (), FILE_SIZE)
    assert _baseline_holds("s0_local", {**reading, "qos_ids": []}, (), FILE_SIZE)
