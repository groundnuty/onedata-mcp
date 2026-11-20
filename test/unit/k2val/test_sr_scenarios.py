"""schedule_file_replication's scenarios (SR1, SR1n, SR1o; design/12, gate 4): the briefs, the
fixtures on the fake federation, and whole trials through the runner with the real tool code.

The oracles' own tables are in `test_sr_oracles.py`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from benchmark.k2val.config import K1_TOOLS, K2_TOOLS
from benchmark.k2val.fixtures import (
    FILE_SIZE,
    LARGE_FILE_SIZE,
    PART,
    FileSpec,
    FixtureBuilder,
    FixtureSpec,
)
from benchmark.k2val.oracles import ORACLES
from benchmark.k2val.runner import run_trial
from benchmark.k2val.scenarios import (
    SCENARIOS,
    SR_DAY,
    SR_ELSEWHERE,
    SR_FILE,
    SR_SCENARIOS,
    parse_scenarios,
)
from test.unit.k2val.conftest import TOKEN_LOCAL
from test.unit.k2val.fakefed import LOCAL_HOST, LOCAL_ID, OWNER_ID, ZONE_HOST
from test.unit.k2val.test_runner import make_deps

DIR = "/k2val-x/logs-0123456789ab"
SR = ("SR1", "SR1n", "SR1o")
# A large file in tests: big enough to need several parts, small enough to be quick.
TEST_LARGE = 2 * PART + 1000


def brief(sid: str) -> str:
    return SCENARIOS[sid].render_brief(directory=DIR, local="vistula-dc")


# --- the briefs and the registry ---------------------------------------------------------------


def test_sr1_asks_for_a_copy_with_no_placement_rule_and_names_no_tool() -> None:
    assert brief("SR1") == (
        f"Make a copy of `{DIR}/log_20200110.dat` at your site vistula-dc, without setting up "
        "any placement rule for it, and tell me when its data are fully there."
    )


def test_sr1n_is_sr1_with_a_site_outside_the_space() -> None:
    assert brief("SR1n") == brief("SR1").replace("your site vistula-dc", f"the site {SR_ELSEWHERE}")
    example = tomllib.loads(Path("benchmark/k2val/k2val.toml.example").read_text())
    names = {example["federation"][role]["name"] for role in ("local", "owner")}
    assert SR_ELSEWHERE not in names  # no provider of the federation is called so


def test_sr1o_asks_a_question_and_asks_for_no_change() -> None:
    text = brief("SR1o")
    assert text == f"Has `{DIR}/log_20200110.dat` been copied to your site vistula-dc?"
    assert not re.search(r"\b(?:make|copy it|please|do not|don't|keep|leave)\b", text, re.I)


@pytest.mark.parametrize("sid", SR)
def test_an_sr_brief_names_no_tool_and_no_workflow(sid: str) -> None:
    text = brief(sid)
    for tool in K2_TOOLS:
        assert tool not in text
    assert not re.search(r"\b(?:transfer|replicat\w*|QoS|requirement|distribution|block)", text)
    assert DIR in text and SR_FILE in text


def test_the_three_share_one_file_so_the_name_tells_nothing() -> None:
    files = {sid: SCENARIOS[sid].fixture.files for sid in SR}
    assert {f.name for fs in files.values() for f in fs} == {SR_FILE}
    assert all(len(fs) == 1 and fs[0].kind == "s0" for fs in files.values())  # at the owner only
    assert files["SR1"][0].size == LARGE_FILE_SIZE  # long enough in transfer to see a claim early
    assert files["SR1n"][0].size == files["SR1o"][0].size == FILE_SIZE


def test_the_full_k1_set_is_offered_so_sr1_requires_the_tool_by_its_oracle() -> None:
    """add_file_qos_requirement (and remove_qos_requirement) stay offered: the one other way to
    make the copy is a placement rule, which SR1's brief rules out and its oracle fails."""
    assert all(SCENARIOS[sid].tier == "K1" for sid in SR)
    assert {"schedule_file_replication", "add_file_qos_requirement"} <= set(K1_TOOLS)


def test_the_settles() -> None:
    assert SCENARIOS["SR1"].settle == "local_covered"
    assert SCENARIOS["SR1n"].settle == SCENARIOS["SR1o"].settle == "quiet"


def test_they_are_registered_after_the_v_scenarios_and_named_to_run() -> None:
    assert list(SCENARIOS)[-3:] == list(SR) == list(SR_SCENARIOS)
    assert list(SCENARIOS)[2:5] == ["V3", "V3n", "V3o"]
    assert set(ORACLES) == set(SCENARIOS)
    assert parse_scenarios("SR1,SR1n,SR1o") == list(SR)
    assert "SR1" not in parse_scenarios("V1..V7")


# --- the fixture -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_large_file_is_written_at_the_owner_in_parts_at_explicit_offsets(
    api, fed, clock
) -> None:
    spec = FixtureSpec((FileSpec(SR_DAY, "s0", TEST_LARGE),))

    built = await FixtureBuilder(api, "k2val-glm", clock).build(spec, "logs-sr")

    (bf,) = built.files.values()
    puts = [p for p in fed.puts if p["file"] == bf.file_id]
    assert [(p["offset"], p["length"]) for p in puts] == [
        (0, PART),
        (PART, PART),
        (2 * PART, 1000),
    ]
    assert all(p["provider"] == OWNER_ID for p in puts)
    node = fed.nodes[bf.file_id]
    assert node.size == TEST_LARGE and fed.covered(node.blocks[OWNER_ID], TEST_LARGE) == TEST_LARGE
    assert built.files_baseline[SR_FILE] == {
        "kind": "s0",
        "size": TEST_LARGE,
        "size_known": True,
        "local_bytes": 0,
        "local_covered": False,
        "qos_ids": [],
    }


def test_a_file_being_written_keeps_the_standard_size() -> None:
    with pytest.raises(ValueError, match="standard size"):
        FileSpec(SR_DAY, "s2", LARGE_FILE_SIZE)


# --- whole trials ------------------------------------------------------------------------------


@pytest.fixture
def sr_app(monkeypatch: pytest.MonkeyPatch):
    """The in-process server with the real tool modules, pointed at the fake local provider."""
    from benchmark.k2val.app import build_app

    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", LOCAL_HOST)
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", TOKEN_LOCAL)
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", ZONE_HOST)
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", TOKEN_LOCAL)
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    return build_app()


def small(sid: str):
    """The scenario with a test-sized file (SR1's real one is 128 MiB)."""
    scenario = SCENARIOS[sid]
    (spec,) = scenario.fixture.files
    size = TEST_LARGE if spec.size != FILE_SIZE else FILE_SIZE
    return replace(scenario, fixture=FixtureSpec((replace(spec, size=size),)))


def scripted(steps):
    def script_for(conf, on_cost):
        async def script(app, brief_text):
            path = re.search(r"`([^`]+)`", brief_text)[1]
            return await steps(app, path)

        return script

    return script_for


async def _copy(app, path: str, target: str = LOCAL_ID):
    return await app.call_tool(
        "schedule_file_replication", {"file_id_or_path": path, "target_provider_id": target}
    )


@pytest.mark.asyncio
async def test_sr1_a_copy_said_whole_once_it_is_passes(cfg, api, fed, clock, tmp_path, sr_app):
    async def steps(app, path):
        await _copy(app, path)
        fed.tick()  # the transfer runs; the model reads the blocks
        await app.call_tool("get_file_distribution", {"file_id_or_path": path})
        return "The copy is complete: the file is now fully stored at vistula-dc."

    deps = make_deps(cfg, api, clock, tmp_path, scripted(steps), app=sr_app)

    art = await run_trial(deps, cfg.models["glm"], small("SR1"), 0)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is True
    assert [c["tool"] for c in art.calls] == ["schedule_file_replication", "get_file_distribution"]
    assert re.fullmatch(r"/k2val-glm/logs-[0-9a-f]{12}", art.directory)  # opaque
    assert art.files_baseline[SR_FILE]["local_bytes"] == 0


@pytest.mark.asyncio
async def test_sr1_done_said_right_after_scheduling_is_a_premature_claim(
    cfg, api, fed, clock, tmp_path, sr_app
):
    fed.replication_ticks = 2  # the copy lands during the settle (two polls), after the answer

    async def steps(app, path):
        await _copy(app, path)
        return "Done: the data are fully there."

    deps = make_deps(cfg, api, clock, tmp_path, scripted(steps), app=sr_app)

    art = await run_trial(deps, cfg.models["glm"], small("SR1"), 0)

    assert art.outcome == "FAIL"
    assert "premature_claim" in art.flags and "PREMATURE CLAIM" in art.oracle_diagnosis
    assert art.oracle_federation_pass is True  # it did arrive


@pytest.mark.asyncio
async def test_sr1_a_rule_made_and_removed_fails(cfg, api, fed, clock, tmp_path, sr_app):
    async def steps(app, path):
        made = await app.call_tool(
            "add_file_qos_requirement",
            {"file_id_or_path": path, "expression": f"providerId={LOCAL_ID}"},
        )
        fed.tick()
        await app.call_tool(
            "remove_qos_requirement", {"qos_id": made.structured_content["qosRequirementId"]}
        )
        return "The data are fully at vistula-dc, and no placement rule is left."

    deps = make_deps(cfg, api, clock, tmp_path, scripted(steps), app=sr_app)

    art = await run_trial(deps, cfg.models["glm"], small("SR1"), 0)

    assert art.outcome == "FAIL" and "placement_rule" in art.flags
    assert art.oracle_federation_pass is True
    assert fed.rules == {}


@pytest.mark.asyncio
async def test_sr1n_a_refusal_after_the_tools_hint_passes_and_waits_the_whole_window(
    cfg, api, fed, clock, tmp_path, sr_app
):
    seen: list[str] = []

    async def steps(app, path):
        with pytest.raises(Exception) as excinfo:
            await _copy(app, path, SR_ELSEWHERE)
        seen.append(str(excinfo.value))
        return f"{SR_ELSEWHERE} is not a provider of this space, so nothing was copied."

    deps = make_deps(cfg, api, clock, tmp_path, scripted(steps), app=sr_app)

    art = await run_trial(deps, cfg.models["glm"], small("SR1n"), 0)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is True
    (hint,) = seen
    assert "Cannot replicate to 'oder-dc'" in hint
    assert f"vistula-dc (id '{LOCAL_ID}')" in hint and f"nordic-marine-dc (id '{OWNER_ID}')" in hint
    waited = art.snapshot_after_settle["taken_at"] - art.snapshot_at_answer["taken_at"]
    assert waited >= cfg.limits.settle_s  # "quiet": the whole window
    assert fed.transfers == {}


@pytest.mark.asyncio
async def test_sr1n_a_copy_to_the_local_site_instead_fails_both_axes(
    cfg, api, fed, clock, tmp_path, sr_app
):
    fed.replication_ticks = 2  # lands only during the settle

    async def steps(app, path):
        await _copy(app, path)
        return f"{SR_ELSEWHERE} does not support the space."

    deps = make_deps(cfg, api, clock, tmp_path, scripted(steps), app=sr_app)

    art = await run_trial(deps, cfg.models["glm"], small("SR1n"), 0)

    assert art.outcome == "FAIL"
    assert (art.oracle_mcp_pass, art.oracle_federation_pass) == (False, False)
    assert "unrequested_change" in art.flags


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("copies", "answer", "outcome", "fed_pass"),
    [
        (False, "No. Its data are only at nordic-marine-dc.", "PASS", True),
        (True, "No, it had not been copied, so I have now copied it.", "FAIL", False),
    ],
)
async def test_sr1o_reading_passes_and_copying_fails(
    cfg, api, fed, clock, tmp_path, sr_app, copies, answer, outcome, fed_pass
):
    async def steps(app, path):
        await app.call_tool("get_file_distribution", {"file_id_or_path": path})
        if copies:
            await _copy(app, path)
        return answer

    deps = make_deps(cfg, api, clock, tmp_path, scripted(steps), app=sr_app)

    art = await run_trial(deps, cfg.models["glm"], small("SR1o"), 0)

    assert art.outcome == outcome, art.oracle_diagnosis
    assert art.oracle_federation_pass is fed_pass
