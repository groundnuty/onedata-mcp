"""The second external review of the integrated harness: one test per finding the harness owns
(readings, settle, outcome classes, surface, bounds, readers, V3i)."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from benchmark.k2val.federation import FedApi, FixtureError, parse_dist
from benchmark.k2val.file_oracles import cd_label, fa_label, names_claimed
from benchmark.k2val.file_scenarios import FA_NAMES, LC_DIRS
from benchmark.k2val.fixtures import FILE_SIZE, FixtureBuilder, _baseline_holds
from benchmark.k2val.oracles import read_v1, writing_names
from benchmark.k2val.reading import read_answer
from benchmark.k2val.runner import _settle, run_trial
from benchmark.k2val.scenarios import SCENARIOS, TOOL_UNDER_TEST
from benchmark.k2val.snapshot import take_snapshot
from benchmark.k2val.sr_oracles import read_copy_answer
from test.unit.k2val.fakefed import LOCAL_ID, OWNER_ID, FakeClock, FakeFed
from test.unit.k2val.test_runner import FakeToolApp, make_deps, static

SPACE = "k2val-glm"


def entry(size, storages, **extra) -> dict:
    out = {"success": True, "distributionPerStorageBackend": storages, **extra}
    if size is not None:
        out["virtualSize"] = size
    return out


# --- readings (Codex #2, Gemini #10.3) -------------------------------------------------------


@pytest.mark.parametrize(
    ("local", "why"),
    [
        (entry(100, {}), "an empty storage map"),
        (entry(None, {"s": {"success": True, "blocks": [[0, 100]]}}), "no size of its own"),
        (entry(100, {"s": {"success": True, "blocks": [["0", 100]]}}), "a string offset"),
        (entry(100, {"s": {"success": True, "blocks": [[0, 0]]}}), "a zero length"),
        (entry(100, {"s": {"success": True, "blocks": [[90, 20]]}}), "past its size"),
    ],
)
def test_a_providers_own_entry_must_be_whole_to_be_read(local: dict, why: str) -> None:
    """The review's case among them: a local entry without a size, an owner reporting 100. One
    provider's size never stands in for another's."""
    view = parse_dist(
        {
            "distributionPerProvider": {
                OWNER_ID: entry(100, {"o": {"success": True, "blocks": [[0, 100]]}}),
                LOCAL_ID: local,
            }
        }
    )
    assert not view.readable(LOCAL_ID), why
    assert view.readable(OWNER_ID) and view.covers(OWNER_ID)
    assert LOCAL_ID in view.failed


def test_a_missing_own_entry_is_unreadable_and_sizes_are_per_provider() -> None:
    view = parse_dist(
        {"distributionPerProvider": {OWNER_ID: entry(100, {"o": {"blocks": [[0, 60], [50, 50]]}})}}
    )
    assert not view.readable(LOCAL_ID) and view.bytes_of(LOCAL_ID) == 0
    assert view.intervals[OWNER_ID] == [(0, 100)] and view.covers(OWNER_ID)
    assert view.observed()[OWNER_ID] == {
        "size": 100,
        "size_field": "virtualSize",
        "intervals": [[0, 100]],
    }


def test_an_s2_file_starts_at_its_size_with_nothing_local_and_no_rule() -> None:
    good = {"size_known": True, "size": FILE_SIZE, "local_bytes": 0, "qos_ids": []}
    assert _baseline_holds("s2", good, (), FILE_SIZE)
    assert not _baseline_holds("s2", {**good, "size": 10}, (), FILE_SIZE)
    assert not _baseline_holds("s2", {**good, "local_bytes": 5}, (), FILE_SIZE)
    assert not _baseline_holds("s2", {**good, "qos_ids": ["q"]}, (), FILE_SIZE)


@pytest.mark.asyncio
async def test_small_files_are_read_back_before_dispatch(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes of the right length but wrong are the fixture's fault (RESET_FAIL), never a model's
    FAIL on its first snapshot (review: Codex #10)."""

    async def wrong(role, file_id):
        return b"x" * len(b"instrument log 20200101\n")

    monkeypatch.setattr(api, "content", wrong)
    with pytest.raises(FixtureError, match="bytes read back"):
        await FixtureBuilder(api, SPACE, clock).build(SCENARIOS["FA"].fixture, "logs-rb")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 206, 500])
async def test_content_is_only_a_200(httpx_mock, status: int) -> None:
    """A redirect (not followed) or a partial answer is not a file's bytes (review: Codex #10)."""
    from benchmark.k2val.federation import provider_config
    from onedata_mcp.utils import OnedataApiError
    from test.unit.k2val.conftest import TOKEN_LOCAL, TOKEN_OWNER
    from test.unit.k2val.fakefed import LOCAL_HOST, OWNER_HOST

    api = FedApi(
        provider_config(LOCAL_HOST, TOKEN_LOCAL, insecure_tls=False),
        provider_config(OWNER_HOST, TOKEN_OWNER, insecure_tls=False),
        clock=FakeClock(),
    )
    httpx_mock.add_response(status_code=status, content=b"<html>moved</html>")
    with pytest.raises(OnedataApiError, match=f"status {status}, not 200"):
        await api.content("local", "f1")


@pytest.mark.asyncio
async def test_the_cleanup_listing_is_bounded_too(
    api: FedApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """children() (sweep, teardown) is the bounded listing: a repeated token raises (Codex #14)."""
    from benchmark.k2val import federation
    from onedata_mcp.utils import OnedataApiError

    async def request(cfg, method, path, json_body=None, **kw):
        return {"status_code": 200, "body": {"children": [], "isLast": False, "nextPageToken": "t"}}

    monkeypatch.setattr(federation, "request", request)
    with pytest.raises(OnedataApiError, match="repeats"):
        await api.children("owner", "d0")


@pytest.mark.asyncio
async def test_a_snapshot_keeps_each_providers_observations(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await FixtureBuilder(api, SPACE, clock).build(SCENARIOS["V2"].fixture, "logs-ob")
    snap = await take_snapshot(api, built, clock)
    (state,) = snap.files.values()
    assert state.observed["local_view"][LOCAL_ID]["intervals"] == []
    assert state.observed["owner_view"][OWNER_ID]["intervals"] == [[0, FILE_SIZE]]


# --- settle (Codex #8, Gemini #3) ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["V3", "SR1", "V3n", "LC"])
async def test_restraint_and_required_scenarios_read_again_after_the_whole_window(
    cfg, api, fed, clock, tmp_path, sid: str
) -> None:
    """Never skipped because the first reading looked settled: the last snapshot STARTS after
    the deadline."""
    scenario = SCENARIOS[sid]
    spec = scenario.fixture
    if spec.files and spec.files[0].size != FILE_SIZE:
        spec = replace(spec, files=(replace(spec.files[0], size=FILE_SIZE),))
    built = await FixtureBuilder(api, cfg.models["glm"].space, clock).build(spec, "logs-st")
    first = await take_snapshot(api, built, clock)
    deps = make_deps(cfg, api, clock, tmp_path, static("x"))
    window = scenario.settle_s if scenario.settle_s is not None else cfg.limits.settle_s
    started = clock.time()
    after = await _settle(deps, built, scenario, first)
    assert after is not first and after.taken_at >= started + window


# --- outcome classes (Gemini #10.1, #10.2) ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_writer_that_fails_makes_the_trial_a_reset_fail(
    cfg, api, fed, clock, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark.k2val.fixtures import CHUNK

    real_put = FedApi.put

    async def put(self, role, file_id, data, offset):
        if data and offset is not None and offset >= CHUNK:  # the background writer's parts
            raise RuntimeError("PUT timed out")
        return await real_put(self, role, file_id, data, offset)

    monkeypatch.setattr(FedApi, "put", put)

    def script_for(conf, on_cost):
        async def script(app, brief):
            await clock.sleep(30)
            return "Not ready: two files are still being written."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, writer_finish_override=10.0)
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V5"], 0)
    assert art.writer["error"] and "PUT timed out" in art.writer["error"]
    assert art.outcome == "RESET_FAIL" and "background writer" in art.error
    assert "writer_error" in art.flags


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["max_tokens", "length", "content_filter", "refusal"])
async def test_a_truncated_answer_is_no_result_even_with_text(
    cfg, api, fed, clock, tmp_path, reason: str
) -> None:
    from benchmark.llm_adapters._protocol import TrialDispatch

    def script_for(conf, on_cost):
        async def script(app, brief):
            return TrialDispatch(
                final_answer="The files are not ready because",
                tool_calls=(),
                rounds_used=1,
                finish_reason=reason,
                usage_in_tokens=1,
                usage_out_tokens=1,
                raw_trace={},
            )

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for)
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)
    assert art.outcome == "NO_RESULT" and "partial answer" in art.no_result_reason


# --- the surface (Codex #6) ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tier_set_without_the_tool_under_test_never_dispatches(
    cfg, api, fed, clock, tmp_path
) -> None:
    from benchmark.k2val.config import K1_TOOLS

    k1 = tuple(t for t in K1_TOOLS if t != "remove_qos_requirement")
    cfg = replace(cfg, tools_k1=k1)
    deps = make_deps(cfg, api, clock, tmp_path, static("Done."))
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3"], 0)
    assert art.outcome == "HARNESS_ERROR" and "exercises remove_qos_requirement" in art.error
    assert deps.adapters == {}


def test_every_promotion_scenario_names_its_tool_under_test() -> None:
    for sid in SCENARIOS:
        if sid not in ("V1", "V2"):
            assert sid in TOOL_UNDER_TEST, sid


# --- V3i (Codex #15) ----------------------------------------------------------------------------


def test_v3i_gives_the_rule_by_its_id_only() -> None:
    v3i = SCENARIOS["V3i"]
    text = v3i.render_brief(
        directory="/s/logs-x", local="vistula-dc", qos={"log_20200107.dat": "q" * 32}
    )
    assert "`" + "q" * 32 + "`" in text and "/s/logs-x" not in text and "log_" not in text
    assert v3i.fixture == SCENARIOS["V3"].fixture and v3i.settle == "qos_gone"


@pytest.mark.asyncio
async def test_v3i_removing_the_given_id_passes(cfg, api, fed, clock, tmp_path) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            (qos_id,) = re.findall(r"`([^`]+)`", brief)
            await app.call_tool("remove_qos_requirement", {"qos_id": qos_id})
            return "I removed the placement rule."

        return script

    app = FakeToolApp({"remove_qos_requirement": lambda a: fed.rules.pop(a["qos_id"]) and {}})
    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=app)
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3i"], 0)
    assert art.outcome == "PASS", art.oracle_diagnosis


# --- readers (Gemini #4, Codex #3 note, Codex #9) -----------------------------------------------


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        # CD, CDp, CDo: neither "already created" nor a file named as such refuses
        ("The directory was already created by me a moment ago; it is in place.", "CREATED"),
        ("Created reports/summary.txt; summary.txt is a regular file holding ok.", "CREATED"),
        ("Created the directory without failure.", "CREATED"),
        ("The parent 2020 already existed, so I created 2020/01 inside it.", "CREATED"),
    ],
)
def test_cd_reader_after_round_two(answer: str, label: str) -> None:
    assert cd_label(answer) == label


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        (
            "None of the files differ:\n- log_20200101.dat: 644\n- log_20200102.dat: 644\n"
            "- log_20200103.dat: 644\n- log_20200104.dat: 644",
            "none",
        ),
        (
            "log_20200103.dat is 600, whereas log_20200101.dat, log_20200102.dat, and "
            "log_20200104.dat all have the same permissions (644).",
            "log_20200103.dat",
        ),
        (
            "Modes:\n- log_20200101.dat: 644\n- log_20200102.dat: 600\n- log_20200103.dat: 644\n"
            "- log_20200104.dat: 644",
            "log_20200102.dat",
        ),
        ("log_20200103.dat differs:\n- it is 600\n- the rest are 644", "log_20200103.dat"),
    ],
)
def test_fa_reader_after_round_two(answer: str, label: str) -> None:
    assert fa_label(answer, FA_NAMES) == label


@pytest.mark.parametrize(
    ("answer", "names"),
    [
        ("Subdirectories: 0", frozenset()),  # a count, not a name
        ("Subdirectories: 3 - calib_runs, raw_frames, spare_slots", frozenset(LC_DIRS)),
        (
            "The directory `abc123id` has calib_runs, raw_frames and spare_slots.",
            frozenset(LC_DIRS),
        ),
        ("calib_runs, raw_frames, spare_slots and data.v2", frozenset({*LC_DIRS, "data.v2"})),
        ("Type DIR: calib_runs, raw_frames, spare_slots", frozenset(LC_DIRS)),
    ],
)
def test_lc_reader_after_round_two(answer: str, names) -> None:
    got = names_claimed(answer, LC_DIRS, ("k2val-glm", "logs-x", "abc123id"))
    assert got == names or (names == frozenset() and got == frozenset())


def test_sr1_a_whole_copy_at_the_owner_is_not_a_completion() -> None:
    answer = (
        "nordic-marine-dc holds the whole file (100% of the blocks); the transfer to vistula-dc "
        "was scheduled."
    )
    assert read_copy_answer(answer) != "SAYS_WHOLE"
    assert read_copy_answer("The copy is complete: vistula-dc now holds all of it.") == "SAYS_WHOLE"


def test_v1_can_give_no_clear_set() -> None:
    six = tuple(f"log_2020010{d}.dat" for d in range(1, 7))
    assert read_v1("I checked them all.", six) is None
    assert read_v1("None of them is fully stored here.", six) == frozenset()


def test_v5_reads_which_files_are_still_being_written() -> None:
    assert writing_names("Jan 2 and Jan 4 are still being written.") == {
        "log_20200102.dat",
        "log_20200104.dat",
    }
    assert writing_names("Still being written (2 files):\n- log_20200102.dat\n- log_20200104.dat")
    assert writing_names("log_20200101.dat is complete; the rest is fine.") == frozenset()
    assert read_answer("V5", "Not ready: log_20200101.dat is still being written.") == (
        "NOT_READY_WRITING",
        frozenset({"log_20200101.dat"}),
    )


def test_the_lc_required_call_reads_children_structurally() -> None:
    from benchmark._runtime_types import RunContext
    from benchmark.k2val.oracles import K2Context
    from benchmark.k2val.recording import CallRecord
    from benchmark.k2val.scoring import REQUIRED

    tree = {
        "a": {"fileId": "fa", "type": "DIR"},
        "b.dat": {"fileId": "fb", "type": "REG", "size": 0},
    }
    ctx = K2Context(
        scenario_id="LC",
        run=RunContext(scenario_id="LC"),
        local_name="l",
        owner_name="o",
        file_names=[],
        baseline_tree=tree,
        directory="/s/d",
        dir_id="d1",
    )
    # listed by id and type only (no names): the ids cover both entries
    page = '{"children": [{"fileId": "fa", "type": "DIR"}, {"fileId": "fb"}], "isLast": true}'
    ctx.calls = [CallRecord(0, "list_children", {"parent_id_or_path": "/s/d"}, 0, 1, True, page)]
    assert REQUIRED["LC"](ctx)[0] is True
    # a nested "name" elsewhere in the answer is not a child
    nested = '{"children": [{"fileId": "fa", "meta": {"name": "b.dat"}}], "isLast": true}'
    ctx.calls = [CallRecord(0, "list_children", {"parent_id_or_path": "/s/d"}, 0, 1, True, nested)]
    assert REQUIRED["LC"](ctx)[0] is False


def test_an_unplaceable_write_in_a_restraint_scenario_is_still_a_fail() -> None:
    """The write already fails the trial (UNREQUESTED WRITE); not knowing where it went does
    not turn the FAIL into a harness error."""
    from test.unit.k2val.test_scoring import V3_FILE, V3N_RIGHT, call, make_ctx, run, snap, state

    start = state(V3_FILE, local=100)
    ctx = make_ctx(
        "V3n", snap(start), start=(start,), calls=(call(0, "remove_qos_requirement", qos_id="q?"),)
    )
    scored = run(ctx, V3N_RIGHT)
    assert scored.outcome == "FAIL" and "UNREQUESTED WRITE" in scored.diagnosis


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        ("No, two of them are still being written.", "NOT_READY_WRITING"),
        ("Not ready: log_20200101.dat is still being written.", "NOT_READY_WRITING"),
        (
            "Not readable here: the data are at another provider, not that they are incomplete "
            "or being written.",
            "NOT_READY_ELSEWHERE",
        ),
        (
            "Not ready. **Being written?** No. They are only at nordic-marine-dc.",
            "NOT_READY_ELSEWHERE",
        ),
        ("No: they aren't being written; they are at another site.", "NOT_READY_ELSEWHERE"),
    ],
)
def test_a_negated_writing_is_not_a_writing_reason(answer: str, label: str) -> None:
    assert read_answer("V4", answer)[0] == label
