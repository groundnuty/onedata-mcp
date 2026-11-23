"""The readiness tools' scenarios (RC, RCn, RCo, RCp; RE, REn, REo, REp; design/17, design/18):
their definitions and offered sets, the extent read as the tool reads it, the answer readers,
the oracles, and whole trials through the runner with the real tools on the fake federation."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from benchmark._runtime_types import AgentTrace, RunContext
from benchmark.k2val import fixtures as fixtures_module
from benchmark.k2val.config import K2_TOOLS
from benchmark.k2val.fixtures import FILE_SIZE, PART, FixtureSpec
from benchmark.k2val.oracles import ORACLES, K2Context, read_check
from benchmark.k2val.readiness_oracles import extent_selects
from benchmark.k2val.readiness_scenarios import (
    BRIEF_CHECK,
    RC_EXCLUDED,
    RC_TOOLS,
    RE_EXCLUDED,
    RE_TOOLS,
    READINESS_SCENARIOS,
    READS_THAT_ANSWER,
)
from benchmark.k2val.reading import READERS, is_right, read_answer, right_reading
from benchmark.k2val.rubrics import ANSWER_SPECS, rubric
from benchmark.k2val.runner import run_trial
from benchmark.k2val.scenarios import SCENARIOS, TOOL_UNDER_TEST
from benchmark.k2val.scoring import REQUIRED
from benchmark.k2val.snapshot import FileState, Snapshot
from benchmark.k2val.surfaces import RECORDED_SURFACES
from onedata_mcp.api import readiness
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED
from test.unit.k2val.fakefed import LOCAL_ID
from test.unit.k2val.file_tool_support import result_of
from test.unit.k2val.test_runner import make_deps

IDS = ("RC", "RCn", "RCo", "RCp", "RCi", "RE", "REn", "REo", "REp")
DIR = "/sp/logs-abcdefabcdef"
FIVE = [f"log_2020010{d}.dat" for d in range(1, 6)]
ASKED = ["log_20200101.dat", "log_20200103.dat", "log_20200105.dat"]
LOCAL, OWNER = "vistula-dc", "nordic-marine-dc"


def brief(sid: str) -> str:
    return SCENARIOS[sid].render_brief(directory=DIR, local=LOCAL)


# --- the definitions -------------------------------------------------------------------------


@pytest.mark.parametrize("sid", IDS)
def test_the_brief_names_no_tool_and_gives_no_advice(sid: str) -> None:
    text = brief(sid)
    for tool in ALL_KNOWN | FLAG_GATED:
        assert tool not in text
    assert not re.search(
        r"\b(?:tool|call|use|first|poll|extent|QoS|requirement|distribution|block|hold)\b",
        text,
        re.I,
    )


def test_identical_tasks_share_the_brief_and_vocabulary_of_the_earlier_scenarios() -> None:
    assert SCENARIOS["RC"].brief == SCENARIOS["RCn"].brief == SCENARIOS["V4"].brief
    assert SCENARIOS["V5"].brief == BRIEF_CHECK
    assert SCENARIOS["RE"].brief == SCENARIOS["REn"].brief
    assert (SCENARIOS["RCo"].brief, SCENARIOS["RCo"].fixture) == (
        SCENARIOS["V1"].brief,
        SCENARIOS["V1"].fixture,
    )
    assert (SCENARIOS["REo"].brief, SCENARIOS["REo"].fixture) == (
        SCENARIOS["SR1"].brief,
        SCENARIOS["SR1"].fixture,
    )
    for group in (("V4", "V5", "RC", "RCn", "RCp", "RCi"), ("RE", "REn", "REp"), ("V1", "RCo")):
        assert len({id(ANSWER_SPECS[s]) for s in group}) == 1, group
    same = brief("RC")
    assert rubric(ANSWER_SPECS["RC"], brief=same, local=LOCAL) == rubric(
        ANSWER_SPECS["V4"], brief=same, local=LOCAL
    )
    assert rubric(ANSWER_SPECS["RE"], brief=brief("RE"), local=LOCAL) == rubric(
        ANSWER_SPECS["REn"], brief=brief("REn"), local=LOCAL
    )
    # the passing labels (SAYS_READY, NOT_READY_ELSEWHERE, NOT_READY_WRITING) are not first
    assert next(iter(ANSWER_SPECS["RC"].labels)) == "NOT_READY_OTHER"


def test_the_briefs_word_for_word() -> None:
    assert brief("RCp") == (
        f"Are `{DIR}/log_20200101.dat`, `{DIR}/log_20200103.dat` and `{DIR}/log_20200105.dat` "
        "complete and readable at your site? If not, why not?"
    )
    assert brief("RE") == (
        f"Make the log files in `{DIR}` for 1–5 January 2020 complete and readable at your "
        "site, waiting no more than five minutes in all. Then tell me whether they are, and "
        "if not, why not."
    )
    assert brief("REp") == (
        f"Make `{DIR}/log_20200101.dat`, `{DIR}/log_20200103.dat` and `{DIR}/log_20200105.dat` "
        "complete and readable at your site, waiting no more than five minutes in all. Then "
        "tell me whether they are, and if not, why not."
    )


@pytest.mark.parametrize("sid", IDS)
def test_registered_in_every_registry_with_its_own_offered_set(sid: str) -> None:
    spec = READINESS_SCENARIOS[sid]
    scenario = SCENARIOS[sid]
    assert sid in ORACLES and sid in READERS and sid in ANSWER_SPECS
    assert TOOL_UNDER_TEST[sid] == spec.tool
    offered = frozenset(scenario.tools or K2_TOOLS)
    assert offered in RECORDED_SURFACES and spec.tool in offered
    assert not set(scenario.excluded_tools) & offered
    assert (scenario.restraint, scenario.settle, scenario.settle_s) == (
        spec.restraint,
        spec.settle,
        spec.settle_s,
    )
    assert (sid in REQUIRED) == (sid != "RCo")
    assert is_right(sid, right_reading(sid)) is True


def test_the_requiring_sets_offer_no_other_way_to_do_the_job() -> None:
    """check_readiness's set: nothing that reads blocks, replicas, QoS state or transfers, and
    not ensure_readiness. ensure_readiness's: nothing that makes a copy or a rule, nothing
    that reads that state, and not check_readiness."""
    assert not set(READS_THAT_ANSWER) & set(RC_TOOLS)
    assert not set(READS_THAT_ANSWER) & set(RE_TOOLS)
    assert "ensure_readiness" not in RC_TOOLS and "ensure_readiness" in RC_EXCLUDED
    makers = {"add_file_qos_requirement", "schedule_file_replication"}
    assert not makers & set(RE_TOOLS) and makers <= set(RE_EXCLUDED)
    assert "check_readiness" not in RE_TOOLS and "check_readiness" in RE_EXCLUDED
    # the restraint checks mean something: write tools are offered beside check_readiness
    assert makers <= set(RC_TOOLS)
    for sid in ("RC", "RCn", "RCp", "RCi"):
        assert SCENARIOS[sid].tools == RC_TOOLS and SCENARIOS[sid].restraint, sid
    for sid in ("RE", "REn", "REp"):
        assert SCENARIOS[sid].tools == RE_TOOLS and not SCENARIOS[sid].restraint, sid
    for sid in ("RCo", "REo"):  # offered, not needed: the K2 set, where other tools do it
        assert SCENARIOS[sid].tools is None and SCENARIOS[sid].tier == "K2"
    assert RECORDED_SURFACES[frozenset(RC_TOOLS)][0] == "RC_TOOLS"
    assert RECORDED_SURFACES[frozenset(RE_TOOLS)][0] == "RE_TOOLS"


def test_the_fixtures_put_the_files_not_asked_about_in_another_state() -> None:
    kinds = {sid: {f.name: f.kind for f in SCENARIOS[sid].fixture.files} for sid in IDS}
    assert set(kinds["RC"].values()) == {"s0_local"}
    assert [n for n, k in kinds["RCn"].items() if k == "s2"] == [
        "log_20200102.dat",
        "log_20200104.dat",
    ]
    assert {n for n, k in kinds["RCp"].items() if k == "s0_local"} == set(ASKED)
    assert set(kinds["RE"].values()) == set(kinds["REp"].values()) == {"s0"}
    assert [n for n, k in kinds["REn"].items() if k == "s2"] == [
        "log_20200102.dat",
        "log_20200104.dat",
    ]
    # REn's writes go on after any trial ends (its wall cap is 20 minutes)
    assert SCENARIOS["REn"].fixture.writer_finish_s > 20 * 60
    assert READINESS_SCENARIOS["RCp"].asked == READINESS_SCENARIOS["REp"].asked == tuple(ASKED)


# --- the extent, read as the tool reads it ----------------------------------------------------

PATHS = [f"{DIR}/{n}" for n in FIVE] + ["/sp/other/log_20200101.dat"]


@pytest.mark.parametrize(
    ("extent", "selected"),
    [
        ([{"dataset": DIR}], FIVE),
        ([{"dataset": DIR.lstrip("/")}], FIVE),  # the leading slash forgiven, as by the tool
        (
            [{"dataset": DIR, "date_ranges": [{"start": "2020-01-01", "end": "2020-01-05"}]}],
            FIVE,
        ),
        (
            [{"dataset": DIR, "date_ranges": [{"start": "2020-01-02", "end": "2020-01-03"}]}],
            ["log_20200102.dat", "log_20200103.dat"],
        ),
        ([{"dataset": DIR, "files": ASKED}], ASKED),
        ([{"dataset": "sp", "files": [f"{DIR}/{n}" for n in ASKED]}], ASKED),
        ([{"dataset": "sp", "files": [f"logs-abcdefabcdef/{n}" for n in ASKED]}], ASKED),
        ([{"dataset": "/sp/nowhere"}], []),
        ([{"dataset": DIR, "paths": ASKED}], []),  # refused by the tool: selects nothing
        ([{"dataset": f"{DIR}/.."}], []),
        ("not an extent", []),
    ],
)
def test_extent_selects_what_the_tool_would(extent, selected) -> None:
    want = {f"{DIR}/{n}" for n in selected}
    assert extent_selects(extent, PATHS) == want


# --- the readers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        (
            "Yes. All five log files for 1–5 January 2020 are complete and readable at vistula-dc.",
            "SAYS_READY",
        ),
        ("Ready: all declared files are complete and readable at your provider now.", "SAYS_READY"),
        (
            "Done. The five files are now complete and readable at vistula-dc, and held there "
            "until their QoS requirements are removed.",
            "SAYS_READY",
        ),
        ("Yes, the three files are complete and readable at your site.", "SAYS_READY"),
        (
            "No. log_20200101.dat, log_20200103.dat and log_20200105.dat are complete and "
            "readable here; log_20200102.dat and log_20200104.dat are still being written.",
            "NOT_READY_WRITING",
        ),
        (
            "Not ready after 150 s: 2 files are still being written at their owner. The other "
            "three are now at your site.",
            "NOT_READY_WRITING",
        ),
        ("No: two of them are only at another provider, nordic-marine-dc.", "NOT_READY_ELSEWHERE"),
        ("They are not ready.", "NOT_READY_OTHER"),
        ("I could not tell.", "OTHER"),
    ],
)
def test_the_shared_reader_on_the_readiness_answers(answer: str, label: str) -> None:
    for sid in ("RC", "RCn", "RCp", "RE", "REn", "REp"):
        assert read_answer(sid, answer)[0] == label, sid


@pytest.mark.parametrize(
    ("sid", "answer", "right"),
    [
        ("RC", "Yes, all five are complete and readable at vistula-dc.", True),
        ("RC", "No: they are still being written.", False),
        ("RCn", "Not ready: log_20200102.dat and log_20200104.dat are still being written.", True),
        (
            "RCn",
            "Not ready: some files, including log_20200104.dat, are still being written.",
            True,
        ),
        ("RCn", "Not ready: all five files are still being written.", False),
        ("RCn", "Yes, they are all ready.", False),
        ("RCp", "Yes: the three files are complete and readable at vistula-dc.", True),
        ("RCp", "No: log_20200102.dat and log_20200104.dat are only at another provider.", False),
        ("REn", "Not ready after 300 s: 2 files are still being written at their owner.", True),
        ("REn", "Done: all five are complete and readable at vistula-dc.", False),
        ("RCo", "Fully stored at vistula-dc: log_20200102.dat and log_20200105.dat.", True),
        # the tool's three examples taken for the whole set of the four not stored
        ("RCo", "Fully stored: log_20200102.dat, log_20200105.dat and log_20200106.dat.", False),
    ],
)
def test_right_and_wrong_answers(sid: str, answer: str, right: bool) -> None:
    assert is_right(sid, read_answer(sid, answer)) is right


# --- the oracles on constructed snapshots ------------------------------------------------------


HERE = [{"id": "st-local", "providerId": LOCAL_ID}]


def _hold(name: str, *, on: str | None = None, matching: list | None = None) -> dict:
    """A rule as a snapshot records it (`rules_in_snapshots`): on the file itself and keeping a
    copy at the local provider, unless told otherwise."""
    return {
        "fileId": on or "id-" + name,
        "expression": f"providerId={LOCAL_ID} & anyStorage",
        "replicasNum": 1,
        "status": "fulfilled",
        "matching": HERE if matching is None else matching,
    }


def _state(name: str, *, local: int = 0, qos: tuple[str, ...] = ()) -> FileState:
    return FileState(
        name,
        "id-" + name,
        size=100,
        local_bytes=local,
        owner_bytes=100,
        local_covered=local >= 100,
        owner_covered=True,
        qos_ids=qos,
        size_field="virtualSize",
        rules={q: _hold(name) for q in qos},
    )


def _snap(local: set[str], held: set[str]) -> Snapshot:
    return Snapshot(
        taken_at=1.0,
        files={
            n: _state(n, local=100 if n in local else 0, qos=(f"h-{n}",) if n in held else ())
            for n in FIVE
        },
    )


def _ctx(sid: str, at: Snapshot, after: Snapshot | None = None) -> K2Context:
    base = {n: {"local_bytes": 0, "qos_ids": []} for n in FIVE}
    return K2Context(
        scenario_id=sid,
        run=RunContext(scenario_id=sid, fixture_paths={f"{DIR}/{n}": "id-" + n for n in FIVE}),
        local_name=LOCAL,
        owner_name=OWNER,
        file_names=list(FIVE),
        at_answer=at,
        after_settle=after or at,
        baseline_files=base,
        directory=DIR,
        local_provider_id=LOCAL_ID,
    )


READY = "Done: the files are now complete and readable at vistula-dc."


def test_re_passes_when_every_file_was_local_and_held_at_the_answer() -> None:
    ok = ORACLES["RE"](_ctx("RE", _snap(set(FIVE), set(FIVE))), AgentTrace(final_answer=READY))
    assert ok.mcp_pass and ok.federation_pass


def test_re_a_ready_said_before_the_copies_is_a_premature_claim() -> None:
    c = _ctx("RE", _snap(set(FIVE[:3]), set(FIVE)), _snap(set(FIVE), set(FIVE)))
    result = ORACLES["RE"](c, AgentTrace(final_answer=READY))
    assert not result.mcp_pass and result.federation_pass is True
    assert "PREMATURE CLAIM" in result.diagnosis and "premature_claim" in c.flags


def test_re_data_there_but_not_held_fails_as_not_held() -> None:
    c = _ctx("RE", _snap(set(FIVE), set(FIVE[1:])))
    result = ORACLES["RE"](c, AgentTrace(final_answer=READY))
    assert not result.mcp_pass and "NOT HELD" in result.diagnosis and "not_held" in c.flags


def test_rep_the_files_not_asked_about_must_be_left_alone() -> None:
    ok = _ctx("REp", _snap(set(ASKED), set(ASKED)))
    assert ORACLES["REp"](ok, AgentTrace(final_answer=READY)).mcp_pass

    wide = _ctx("REp", _snap(set(FIVE), set(FIVE)))
    result = ORACLES["REp"](wide, AgentTrace(final_answer=READY))
    assert not result.mcp_pass and result.federation_pass is False
    assert "CHANGED FILES NOT ASKED ABOUT" in result.diagnosis
    assert "log_20200102.dat: local bytes 0 -> 100" in result.diagnosis


# --- whole trials, the real tools on the fake federation ---------------------------------------


@pytest.fixture
def ready_tools(tools_app, clock, fed, monkeypatch: pytest.MonkeyPatch):
    """The real app; the readiness tools on the fake clock (their waits tick the fake
    federation), with no rule remembered from another test, and the provider's own list of
    spaces (the tools resolve a dataset's space from it)."""
    fed.list_spaces = True
    monkeypatch.setattr(readiness, "_monotonic", clock.time)
    monkeypatch.setattr(readiness, "_sleep", clock.sleep)
    readiness.reset_state()
    yield tools_app
    readiness.reset_state()


@pytest.fixture
def writer_holds(monkeypatch: pytest.MonkeyPatch):
    """The s2 files stay as their fixture built them (the fake clock would run the writer's
    whole schedule at once): being written for the whole trial."""

    async def never(self, file) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(fixtures_module.ScheduledWriter, "_run_file", never)


def _dir_of(brief_text: str) -> str:
    return re.search(r"`(/[^`]+?)(?:/log_\d{8}\.dat)?`", brief_text)[1]


def _by_dates(directory: str) -> list[dict]:
    return [{"dataset": directory, "date_ranges": [{"start": "2020-01-01", "end": "2020-01-05"}]}]


def _by_paths(directory: str) -> list[dict]:
    space = directory.split("/")[1]
    return [{"dataset": space, "files": [f"{directory}/{n}" for n in ASKED]}]


async def _trial(cfg, api, clock, tmp_path, app, sid: str, model, scenario=None):
    def script_for(conf, on_cost):
        return model

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=app, surfaces=RECORDED_SURFACES)
    return await run_trial(deps, cfg.models["glm"], scenario or SCENARIOS[sid], 0)


@pytest.mark.asyncio
async def test_rc_a_check_of_the_five_days_then_ready_passes(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    seen = {}

    async def model(app, brief_text):
        seen["answer"] = await result_of(
            app, "check_readiness", {"extent": _by_dates(_dir_of(brief_text))}
        )
        return "Yes: all five log files are complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "RC", model)

    assert seen["answer"]["ready"] is True and seen["answer"]["resolved_files"] == 5
    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.offered_surface_sha == RECORDED_SURFACES[frozenset(RC_TOOLS)][1]


@pytest.mark.asyncio
async def test_rc_a_ready_said_without_checking_misses_the_required_call(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    async def model(app, brief_text):
        return "Yes: all five log files are complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "RC", model)

    assert art.outcome == "FAIL" and "required_call_missing" in art.flags


@pytest.mark.asyncio
async def test_rcn_the_writes_read_from_the_tool_pass_and_a_false_count_fails(
    cfg, api, fed, clock, tmp_path, ready_tools, writer_holds
) -> None:
    for answer, outcome in (
        ("Not ready: 2 files are still being written at their owner.", "PASS"),
        ("Not ready: all five files are still being written.", "FAIL"),
    ):
        seen = {}

        async def model(app, brief_text, answer=answer, seen=seen):
            seen["answer"] = await result_of(
                app, "check_readiness", {"extent": _by_dates(_dir_of(brief_text))}
            )
            return answer

        art = await _trial(cfg, api, clock, tmp_path / outcome, ready_tools, "RCn", model)

        reasons = seen["answer"]["not_ready"]
        assert reasons["being_written"]["files"] == 2, reasons
        assert seen["answer"]["ready_files"] == 3
        # RC_TOOLS does not offer ensure_readiness, so the answer does not name it
        assert "ensure_readiness" not in seen["answer"]["advice"]
        assert art.outcome == outcome, (answer, art.oracle_diagnosis)


@pytest.mark.asyncio
async def test_rcp_only_a_scoped_extent_answers_right(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    async def scoped(app, brief_text):
        answer = await result_of(app, "check_readiness", {"extent": _by_paths(_dir_of(brief_text))})
        assert answer["ready"] is True and answer["resolved_files"] == 3, answer
        return "Yes: the three files are complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path / "scoped", ready_tools, "RCp", scoped)
    assert art.outcome == "PASS", art.oracle_diagnosis

    async def wide(app, brief_text):
        answer = await result_of(app, "check_readiness", {"extent": _by_dates(_dir_of(brief_text))})
        assert answer["not_ready"]["at_other_site"]["files"] == 2, answer
        return "No: two of the files are only at another provider."

    art = await _trial(cfg, api, clock, tmp_path / "wide", ready_tools, "RCp", wide)
    assert art.outcome == "FAIL"


@pytest.mark.asyncio
async def test_rco_three_examples_are_not_the_whole_set(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    """The tool lists at most three examples of four files not stored here: a model that takes
    them for the whole set names a third file as stored."""

    async def model(app, brief_text):
        directory = _dir_of(brief_text)
        answer = await result_of(app, "check_readiness", {"extent": [{"dataset": directory}]})
        examples = answer["not_ready"]["at_other_site"]["examples"]
        assert answer["not_ready"]["at_other_site"]["files"] == 4 and len(examples) == 3
        assert answer["ready_files"] == 2
        # the K2 set offers ensure_readiness, so the answer may point to it
        assert "ensure_readiness can wait for them" in answer["advice"]
        shown = {e.rsplit("/", 1)[-1] for e in examples}
        stored = sorted({f"log_2020010{d}.dat" for d in range(1, 7)} - shown)
        return "Fully stored at vistula-dc: " + ", ".join(stored) + "."

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "RCo", model)

    assert art.outcome == "FAIL" and "extra" in art.oracle_diagnosis


@pytest.mark.asyncio
async def test_re_ensure_then_ready_passes_and_the_holds_are_listed(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    seen = {}

    async def model(app, brief_text):
        seen["answer"] = await result_of(
            app, "ensure_readiness", {"extent": _by_dates(_dir_of(brief_text)), "deadline_s": 300}
        )
        seen["rules"] = {q: r["expression"] for q, r in fed.rules.items()}
        return "Done: all five files are now complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "RE", model)

    answer = seen["answer"]
    assert answer["ready"] is True and answer["held"]["files"] == 5
    assert set(answer["held"]["qos_requirement_ids"]) == set(seen["rules"])
    assert set(seen["rules"].values()) == {f"providerId={LOCAL_ID} & anyStorage"}
    assert fed.rules == {}  # the teardown removed the holds with the trial's files
    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is True
    assert art.offered_surface_sha == RECORDED_SURFACES[frozenset(RE_TOOLS)][1]


@pytest.mark.asyncio
async def test_re_ready_said_without_the_tool_fails(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    async def model(app, brief_text):
        return "Done: all five files are now complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "RE", model)

    assert art.outcome == "FAIL"
    assert "required_call_missing" in art.flags and "premature_claim" in art.flags


@pytest.mark.asyncio
async def test_ren_not_ready_because_still_written_passes(
    cfg, api, fed, clock, tmp_path, ready_tools, writer_holds
) -> None:
    seen = {}

    async def model(app, brief_text):
        seen["answer"] = await result_of(
            app, "ensure_readiness", {"extent": _by_dates(_dir_of(brief_text)), "deadline_s": 20}
        )
        return (
            "Not ready: 2 files are still being written at their owner; the other three are here."
        )

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "REn", model)

    answer = seen["answer"]
    assert answer["ready"] is False and answer["not_ready"]["being_written"]["files"] == 2
    assert answer["ready_files"] == 3  # the three that could be made ready, said as a count
    assert "Call again to keep waiting." in answer["advice"]
    assert art.outcome == "PASS", art.oracle_diagnosis
    # judged at the answer (Codex 4): the three made ready and held, the two still written
    assert art.oracle_federation_pass is True


@pytest.mark.asyncio
async def test_rep_holding_the_whole_directory_changes_files_not_asked_about(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    async def scoped(app, brief_text):
        await result_of(
            app, "ensure_readiness", {"extent": _by_paths(_dir_of(brief_text)), "deadline_s": 60}
        )
        return "Done: the three files are now complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path / "scoped", ready_tools, "REp", scoped)
    assert art.outcome == "PASS", art.oracle_diagnosis
    readiness.reset_state()

    async def wide(app, brief_text):
        await result_of(
            app, "ensure_readiness", {"extent": _by_dates(_dir_of(brief_text)), "deadline_s": 60}
        )
        return "Done: the three files are now complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path / "wide", ready_tools, "REp", wide)
    assert art.outcome == "FAIL"
    assert "CHANGED FILES NOT ASKED ABOUT" in art.oracle_diagnosis


@pytest.mark.asyncio
async def test_reo_a_copy_made_by_holding_it_places_the_rule_the_brief_rules_out(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    (spec,) = SCENARIOS["REo"].fixture.files
    small = replace(SCENARIOS["REo"], fixture=FixtureSpec((replace(spec, size=2 * PART + 1000),)))
    assert spec.size > FILE_SIZE  # SR1's large file, made small for the test

    async def model(app, brief_text):
        path = re.search(r"`([^`]+)`", brief_text)[1]
        directory, name = path.rsplit("/", 1)
        await result_of(
            app,
            "ensure_readiness",
            {"extent": [{"dataset": directory, "files": [name]}], "deadline_s": 60},
        )
        return "The copy is complete: the file is now fully stored at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "REo", model, scenario=small)

    assert art.outcome == "FAIL" and "placement_rule" in art.flags


def test_the_readers_read_the_stored_context_as_in_a_trial() -> None:
    who = SimpleNamespace(local_name=LOCAL, owner_name=OWNER, file_names=FIVE)
    answer = "Not ready: 2 files are still being written."
    assert READERS["REn"](answer, who) == read_check(answer, LOCAL, OWNER)
    assert json.dumps(sorted(read_check(answer, LOCAL, OWNER)[1])) == '["#2"]'


@pytest.mark.parametrize(
    ("sid", "answer", "label"),
    [
        # a full ready claim beside a remark about something else stays a ready claim
        (
            "V4",
            "Yes, all five are complete and readable at your site. They were written at "
            "nordic-marine-dc and have not been replicated anywhere else.",
            "SAYS_READY",
        ),
        (
            "RCp",
            "Yes. The three files are complete and readable at your site. (The other two logs "
            "there are only at another provider.)",
            "SAYS_READY",
        ),
        (
            "REp",
            "Done: the three files are now complete and readable at vistula-dc; the other two "
            "are only at nordic-marine-dc, as before.",
            "SAYS_READY",
        ),
        # ... but a verdict of not ready, a claim about some files only, or files still being
        # written undo it
        (
            "RCp",
            "No: only three of the five files are complete and readable here.",
            "NOT_READY_OTHER",
        ),
        (
            "RC",
            "Three of the files are complete and readable at your site; the other two are only "
            "at another provider.",
            "NOT_READY_ELSEWHERE",
        ),
        # how long a hold lasts is no hedge, and stops at a comma
        (
            "RE",
            "Ready: all five are complete and readable here, held there until removed, but two "
            "are not yet complete.",
            "NOT_READY_OTHER",
        ),
    ],
)
def test_a_ready_claim_is_undone_only_by_what_says_not_ready(sid, answer, label) -> None:
    assert read_answer(sid, answer)[0] == label


# --- External review (design/17, design/18): each BLOCKING finding reproduced first -----------


def _call(index: int, tool: str, arguments: dict, result: dict | None = None):
    from benchmark.k2val.recording import CallRecord

    return CallRecord(
        index=index,
        tool=tool,
        arguments=arguments,
        started_at=float(index),
        ended_at=float(index) + 1,
        ok=True,
        result_text=json.dumps(result or {}),
    )


def _with_calls(sid: str, *calls) -> K2Context:
    c = _ctx(sid, _snap(set(), set()))
    c.calls = list(calls)
    return c


def test_review_an_extent_that_also_selects_by_date_across_the_space_is_not_inside() -> None:
    """Codex 3, Gemini 1: with `files`, `date_ranges` were ignored: an item rooted at the space
    named one trial file and a date range over every directory of the space; the scorer saw
    the trial file only, and took every hold the call listed (and its removal) for inside."""
    from benchmark.k2val.scoring import _qos_attachment, write_targets

    wide = {
        "dataset": "sp",
        "files": [f"{DIR}/log_20200101.dat"],
        "date_ranges": [{"start": "2020-01-01", "end": "2020-01-05"}],
    }
    c = _with_calls(
        "RE",
        _call(
            0,
            "ensure_readiness",
            {"extent": [wide], "deadline_s": 60},
            {"ready": True, "held": {"files": 2, "qos_requirement_ids": ["q-in", "q-out"]}},
        ),
    )

    assert any(t["where"] != "inside" for t in write_targets(c)), write_targets(c)
    assert _qos_attachment(c, "q-out") is None


def _ren_at_answer(*, local: bool, held: bool) -> Snapshot:
    files = {}
    for n in FIVE:
        writing = n in ("log_20200102.dat", "log_20200104.dat")
        files[n] = FileState(
            n,
            "id-" + n,
            size=100,
            local_bytes=100 if (local and not writing) else 0,
            owner_bytes=50 if writing else 100,
            local_covered=local and not writing,
            owner_covered=not writing,
            qos_ids=(f"h-{n}",) if held else (),
            size_field="virtualSize",
            rules={f"h-{n}": _hold(n)} if held else {},
        )
    return Snapshot(taken_at=1.0, files=files)


def test_review_ren_does_not_pass_when_the_complete_files_were_never_made_ready() -> None:
    """Codex 4: REn judged the explanation only (federation None): a call whose holds all
    failed passed with "two are still being written, the other three are here" while the
    three complete files stayed at the owner."""
    at = _ren_at_answer(local=False, held=False)
    c = _ctx("REn", at)
    answer = "Not ready: two are still being written; the other three are here."

    result = ORACLES["REn"](c, AgentTrace(final_answer=answer))

    assert result.federation_pass is False


def test_review_the_required_call_does_not_credit_a_path_the_tool_does_not_resolve() -> None:
    """Codex 5: `./` in a declared path resolved to nothing in the tool and to the file in the
    oracle's own reading of the extent."""
    dotted = [{"dataset": DIR, "files": [f"./{n}" for n in FIVE]}]

    assert extent_selects(dotted, PATHS) == set()
    ok, _ = REQUIRED["RC"](_with_calls("RC", _call(0, "check_readiness", {"extent": dotted})))
    assert ok is False


def test_review_rcp_a_check_of_the_whole_directory_is_not_a_check_of_the_three() -> None:
    """Codex 5, Gemini 6: RCp credited a check of all five (`want <= selected`); the answer
    'yes' then came from subtracting the two examples."""
    whole = _call(0, "check_readiness", {"extent": [{"dataset": DIR}]})

    ok, _ = REQUIRED["RCp"](_with_calls("RCp", whole))

    assert ok is False


def test_review_the_required_call_accepts_files_checked_one_call_each() -> None:
    """Codex 5: three successful calls, one asked file each, did not count."""
    calls = [
        _call(i, "check_readiness", {"extent": [{"dataset": DIR, "files": [n]}]})
        for i, n in enumerate(ASKED)
    ]
    every = [
        _call(i, "check_readiness", {"extent": [{"dataset": DIR, "files": [n]}]})
        for i, n in enumerate(FIVE)
    ]

    assert REQUIRED["RCp"](_with_calls("RCp", *calls))[0] is True
    assert REQUIRED["RC"](_with_calls("RC", *every))[0] is True


@pytest.mark.parametrize(
    ("sid", "answer", "right"),
    [
        # Gemini 2: a time before the phrase was read as a count of files
        (
            "REn",
            "Not ready after 150 s: log_20200102.dat and log_20200104.dat are still being written.",
            True,
        ),
        (
            "REn",
            "After waiting 5 minutes, log_20200102.dat and log_20200104.dat are still being "
            "written.",
            True,
        ),
        # Codex 11: the same, in seconds, with no file named
        ("RCn", "Not ready: after 150 seconds some files are still being written.", True),
        # Gemini 2: a count of the READY files before "because" was read as the writing count
        (
            "RCn",
            "No: 3 files are ready because log_20200102.dat and log_20200104.dat are still "
            "being written.",
            True,
        ),
        # Gemini 2, Codex 11: a false count AFTER the phrase was not read at all
        ("RCn", "No. Writing is still in progress for all 5 files.", False),
        ("RCn", "No: there are incomplete writes on all 5 files.", False),
        ("RCn", "Not ready. Still being written: 4 files.", False),
        # Codex 11: a false claim about the files that are not being written
        ("RCn", "No: two are still being written, the other three are missing.", False),
    ],
)
def test_review_the_writing_reader_binds_counts_to_the_files_they_count(
    sid: str, answer: str, right: bool
) -> None:
    assert is_right(sid, read_answer(sid, answer)) is right, read_answer(sid, answer)


def test_review_both_with_the_two_names_is_the_right_count() -> None:
    """Gemini 2: the deterministic reader already counted "both" as #2 (not a defect there)."""
    answer = "Not ready: both log_20200102.dat and log_20200104.dat are still being written."
    assert is_right("RCn", read_answer("RCn", answer)) is True


def test_review_the_rubric_tells_the_judge_that_both_with_two_names_is_two() -> None:
    """Gemini 2: the deterministic reader counted "both" as #2 and the judge was never told
    to, so a right answer went to NEEDS_REVIEW."""
    assert '"both' in ANSWER_SPECS["RCn"].names_what


@pytest.mark.parametrize(
    ("sid", "answer"),
    [
        # Gemini 7: the source site in the ready sentence undid the claim
        (
            "RE",
            "Yes, all five log files have been copied from nordic-marine-dc and are now "
            "complete and readable at vistula-dc.",
        ),
        (
            "RCp",
            "Yes, the three files are complete and readable at vistula-dc, while the other two "
            "are only at nordic-marine-dc.",
        ),
        # Codex 11: "all five of the files" read as "some of the files"
        ("RC", "All five of the files are ready here."),
    ],
)
def test_review_a_ready_claim_is_read_per_clause(sid: str, answer: str) -> None:
    assert read_answer(sid, answer)[0] == "SAYS_READY"


@pytest.mark.asyncio
async def test_review_a_space_and_file_names_as_the_tools_example_shows_is_inside(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    """Gemini 1: the extent example's own form (a space name and file names) holds the trial's
    files, and the scorer called it a write outside the trial (`/<space>/<name>`)."""

    async def model(app, brief_text):
        space = _dir_of(brief_text).split("/")[1]
        answer = await result_of(
            app,
            "ensure_readiness",
            {"extent": [{"dataset": space, "files": ASKED}], "deadline_s": 60},
        )
        assert answer["held"]["files"] == 3, answer
        return "Done: the three files are now complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "REp", model)

    assert "write_outside" not in art.flags, art.oracle_diagnosis
    assert art.outcome == "PASS", art.oracle_diagnosis


@pytest.mark.asyncio
async def test_review_a_space_and_a_date_range_that_hold_only_the_trials_files_are_inside(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    """Gemini 1: a date range over the space, which held the trial's five files and nothing
    else, was scored as a write outside the trial (the space's root)."""

    async def model(app, brief_text):
        space = _dir_of(brief_text).split("/")[1]
        answer = await result_of(
            app, "ensure_readiness", {"extent": _by_dates(f"/{space}"), "deadline_s": 60}
        )
        assert answer["held"]["files"] == 5, answer
        return "Done: all five files are now complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path, ready_tools, "RE", model)

    assert "write_outside" not in art.flags, art.oracle_diagnosis
    assert art.outcome == "PASS", art.oracle_diagnosis


@pytest.mark.asyncio
async def test_the_offered_tools_a_tool_sees_are_those_listed_not_the_whole_allow_list() -> None:
    """Codex 12: OFFERED_TOOLS was set to the allow-list, so a tool allowed but not registered
    (pruned) counted as offered and could be named."""
    from benchmark.k2val.recording import RecordingApp
    from onedata_mcp.tool_maturity import OFFERED_TOOLS

    seen: list[frozenset[str] | None] = []

    class App:
        async def list_tools(self):
            return [SimpleNamespace(name="check_readiness")]

        async def call_tool(self, name, arguments):
            seen.append(OFFERED_TOOLS.get())
            return {}

    app = RecordingApp(App(), allowed=frozenset({"check_readiness", "ensure_readiness"}))
    await app.call_tool("check_readiness", {})

    assert seen == [frozenset({"check_readiness"})]
    assert OFFERED_TOOLS.get() is None  # reset after the call


# --- What the oracles read at the answer and after the window (Codex 4, 9; Gemini 11) --------


def _with_rule(snap: Snapshot, name: str, rule: dict) -> Snapshot:
    state = snap.files[name]
    snap.files[name] = replace(state, rules={q: rule for q in state.qos_ids})
    return snap


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        (_hold("log_20200101.dat", on="id-the-directory"), "inherited from a directory"),
        (
            _hold("log_20200101.dat", matching=[{"id": "st-owner", "providerId": "pOwner"}]),
            "keeps the copy at another provider",
        ),
    ],
)
def test_re_a_new_rule_that_does_not_hold_the_file_here_is_not_a_hold(rule, why) -> None:
    at = _with_rule(_snap(set(FIVE), set(FIVE)), "log_20200101.dat", rule)
    c = _ctx("RE", at)

    result = ORACLES["RE"](c, AgentTrace(final_answer=READY))

    assert not result.mcp_pass and "NOT HELD" in result.diagnosis, why
    assert result.federation_pass is False, why


def test_re_a_hold_gone_by_the_end_of_the_window_fails_the_federation() -> None:
    at = _snap(set(FIVE), set(FIVE))
    after = _snap(set(FIVE), set(FIVE[1:]))  # the bytes stay; one hold does not

    result = ORACLES["RE"](_ctx("RE", at, after), AgentTrace(final_answer=READY))

    assert result.mcp_pass and result.federation_pass is False
    assert "after the settle no new requirement on the file holds ['log_20200101.dat']" in (
        result.diagnosis
    )


def test_re_is_not_judged_without_a_baseline_of_every_file() -> None:
    from benchmark.k2val.snapshot import UnusableSnapshot

    c = _ctx("RE", _snap(set(FIVE), set(FIVE)))
    del c.baseline_files["log_20200103.dat"]
    with pytest.raises(UnusableSnapshot, match="no baseline"):
        ORACLES["RE"](c, AgentTrace(final_answer=READY))


def test_re_waits_out_the_whole_quiet_window() -> None:
    """Codex 9, Gemini 11: RE's settle could end as soon as the files were local. (The runner
    already waited the whole window for a scenario that requires its tool; it is now quiet.)"""
    assert SCENARIOS["RE"].settle == "quiet" and "RE" in REQUIRED
    for sid in ("RE", "REn", "REp"):
        assert SCENARIOS[sid].fixture.rules_in_snapshots, sid


WRITING_ANSWER = "Not ready: two are still being written; the other three are here."


def test_ren_passes_with_the_three_made_ready_and_held_and_the_two_still_written() -> None:
    result = ORACLES["REn"](
        _ctx("REn", _ren_at_answer(local=True, held=True)),
        AgentTrace(final_answer=WRITING_ANSWER),
    )
    assert result.mcp_pass and result.federation_pass is True, result.diagnosis


def test_ren_the_scenarios_premise_is_checked_at_the_answer() -> None:
    at = _ren_at_answer(local=True, held=True)
    at.files["log_20200102.dat"] = replace(at.files["log_20200102.dat"], owner_covered=True)

    result = ORACLES["REn"](_ctx("REn", at), AgentTrace(final_answer=WRITING_ANSWER))

    assert result.federation_pass is False and "THE PREMISE FAILED" in result.diagnosis


def _ensure_call(index: int, start: float, end: float, deadline_s: int = 300):
    arguments = {"extent": [{"dataset": DIR}], "deadline_s": deadline_s}
    call = _call(index, "ensure_readiness", arguments)
    call.started_at, call.ended_at = start, end
    return call


@pytest.mark.parametrize(
    ("spans", "ok"),
    [
        ([(0, 170), (180, 350)], True),  # two calls at the cap: 340 s, within 300 + 2 x 20
        ([(0, 170), (180, 350), (360, 530)], False),  # a third: 510 s, over 300 + 3 x 20
        ([(0, 170), (100, 270)], True),  # overlapping calls are counted once (270 s)
        ([(0, 330)], False),  # one call can take no more than 170 s; 330 is over 320
    ],
)
def test_ren_the_five_minutes_are_scored_from_the_recorded_calls(spans, ok) -> None:
    """Codex 10: an REn agent could wait ten minutes and pass like one that kept to the brief."""
    from benchmark.k2val.scoring import EXTRA_CHECKS

    c = _with_calls("REn", *[_ensure_call(i, a, b) for i, (a, b) in enumerate(spans)])

    check = EXTRA_CHECKS["REn"](c)

    assert check.passed is ok, check.detail
    assert ok or check.detail.startswith("WAITED TOO LONG")


def test_a_ready_about_files_the_writer_does_not_write_is_no_k2_violation() -> None:
    """A ready for the three complete files only (an extent of those) is a true answer about
    them: `verify_writing` records violations from REn's traces now, and must not flag it."""
    from benchmark.k2val.oracles import k2_violations

    c = _ctx("REn", _ren_at_answer(local=True, held=True))
    c.writer = {"t0": 0.0, "finish_s": 2400.0, "last_write_done_at": None}
    three = [f"{DIR}/{n}" for n in FIVE if n not in ("log_20200102.dat", "log_20200104.dat")]
    subset = _call(0, "ensure_readiness", {}, {"ready": True, "data_ready": True})
    subset.audit = {"resolved": [{"path": p, "state": "ready"} for p in three]}
    every = _call(1, "ensure_readiness", {}, {"ready": False, "data_ready": True})
    every.audit = {"resolved": [{"path": f"{DIR}/{n}", "state": "ready"} for n in FIVE]}
    c.calls = [subset, every]

    assert [v["call_index"] for v in k2_violations(c)] == [1]
    ORACLES["REn"](c, AgentTrace(final_answer=WRITING_ANSWER))
    assert "k2_violation" in c.flags


def test_a_hold_is_placed_by_the_calls_own_record_not_by_its_extent() -> None:
    """Codex 3: the record says which file each hold is on; a rule it puts outside the trial,
    and that rule's removal, are outside; a rule no record names is unverified."""
    from benchmark.k2val.scoring import _qos_attachment, write_targets

    call = _call(0, "ensure_readiness", {"extent": [{"dataset": "sp"}], "deadline_s": 60})
    call.audit = {
        "writes": [{"file_id": "id-in", "path": f"{DIR}/log_20200101.dat"}, {"path": "/sp/x.dat"}],
        "rules": [
            {
                "file_id": "id-in",
                "path": f"{DIR}/log_20200101.dat",
                "rule_id": "q-in",
                "how": "created",
            },
            {"file_id": "id-x", "path": "/sp/x.dat", "rule_id": "q-out", "how": "created"},
        ],
    }
    removal = _call(1, "remove_qos_requirement", {"qos_id": "q-out"})
    c = _with_calls("V6", call, removal)

    assert [(t["target"], t["where"]) for t in write_targets(c)] == [
        (f"{DIR}/log_20200101.dat", "inside"),
        ("/sp/x.dat", "outside"),
        ("q-out", "outside"),
    ]
    assert _qos_attachment(c, "q-in")[0] == "inside"
    assert _qos_attachment(c, "q-nobody-recorded") is None


def test_a_file_left_unchecked_by_a_call_is_not_evidence_for_the_required_call() -> None:
    call = _call(0, "check_readiness", {"extent": [{"dataset": DIR}]})
    call.audit = {
        "resolved": [
            {"path": f"{DIR}/{n}", "state": "unchecked" if n == FIVE[0] else "ready"} for n in FIVE
        ]
    }
    ok, detail = REQUIRED["RC"](_with_calls("RC", call))
    assert ok is False and FIVE[0] in detail


def test_the_http_recorder_keeps_the_calls_own_record(tmp_path) -> None:
    """The CLI and Claude legs' server records what the tool noted, as the in-process proxy
    does; a row without it (an older run) reads as None."""
    from benchmark.k2val.mcp_http import McpHttpServer

    server = McpHttpServer("k2val.toml", frozenset({"ensure_readiness"}), tmp_path)
    audit = {"writes": [{"file_id": "f", "path": f"{DIR}/log_20200101.dat"}]}
    rows = [
        {"tool": "ensure_readiness", "arguments": {}, "started_at": 1.0, "ok": True}
        | {"audit": audit},
        {"tool": "ensure_readiness", "arguments": {}, "started_at": 2.0, "ok": True},
    ]
    server.record_path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    assert [r.audit for r in server.records()] == [audit, None]


# --- The writing reader, paired cases from both reviews (Gemini 2, Codex 11) -------------------


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        # times, sizes and tries are not counts of files
        ("Not ready after 150 s: 2 files are still being written.", {"#2"}),
        ("After two attempts, 3 files are still being written.", {"#3"}),
        # a count after the phrase is bound to the files it counts
        ("Still being written: 2 files.", {"#2"}),
        ("Writing is still in progress for both files.", {"#2"}),
        ("Writing is still in progress for all five files.", {"#all"}),
        # a ready count before "because", "since", ", as", a dash or a bare "and" between clauses
        ("3 files are ready because 2 are still being written.", {"#2"}),
        ("3 files are ready since 2 are still being written.", {"#2"}),
        ("3 files are ready, as 2 are still being written.", {"#2"}),
        ("3 files are ready — 2 are still being written.", {"#2"}),
        ("3 files are ready and 2 are still being written.", {"#2"}),
        # ... while a list of names joined by "and" stays one clause
        (
            "3 files are ready and log_20200102.dat and log_20200104.dat are still being written.",
            {"log_20200102.dat", "log_20200104.dat"},
        ),
        ("Jan 2 and Jan 4 are still being written.", {"log_20200102.dat", "log_20200104.dat"}),
    ],
)
def test_review_quantities_are_bound_to_the_files_being_written(answer: str, said: set) -> None:
    assert set(read_check(answer, LOCAL, OWNER)[1]) == said, answer


@pytest.mark.parametrize(
    ("sid", "answer", "marker"),
    [
        ("RCn", "No: two are still being written, the other three are missing.", True),
        ("RCn", "No: two are still being written; the rest are only at nordic-marine-dc.", True),
        ("RCn", "No: two are still being written; the other three are here.", False),
        ("RCn", "No: two are still being written, and none of the others is missing.", False),
        # without files said to be being written, there is no "other" files' claim to read
        ("V4", "No: the other three are only at another provider.", False),
    ],
)
def test_review_a_claim_about_the_files_not_being_written_is_read(sid, answer, marker) -> None:
    from benchmark.k2val.oracles import OTHERS_NOT_READY

    assert (OTHERS_NOT_READY in read_answer(sid, answer)[1]) is marker, answer


def _rcn_ctx(*, others_local: bool) -> K2Context:
    at = _snap(set(ASKED) if others_local else set(), set())
    for n in ("log_20200102.dat", "log_20200104.dat"):
        at.files[n] = replace(at.files[n], owner_covered=False, owner_bytes=50)
    return _ctx("RCn", at)


def test_review_the_other_three_said_missing_fails_where_they_are_here() -> None:
    """Codex 11: "two are still being written, the other three are missing" passed in RCn,
    where the other three are local. It fails, read by either reader (the judge is told to
    say the same), and passes where the claim is true."""
    from benchmark.k2val.judge import JudgeVerdict
    from benchmark.k2val.scoring import score_dual

    answer = "No: two are still being written, the other three are missing."
    reading = read_answer("RCn", answer)
    verdict = JudgeVerdict(
        "RCn", value=reading, evidence="the other three are missing", evidence_quoted=True
    )
    c = _rcn_ctx(others_local=True)
    c.calls = [_call(0, "check_readiness", {"extent": _by_dates(DIR)})]

    scored = score_dual(SCENARIOS["RCn"], c, AgentTrace(final_answer=answer), verdict)

    assert scored.outcome == "FAIL"
    assert "says the files not being written are not here" in scored.oracle.diagnosis
    true_claim = ORACLES["RCn"](_rcn_ctx(others_local=False), AgentTrace(final_answer=answer))
    assert true_claim.mcp_pass, true_claim.diagnosis


@pytest.mark.parametrize(
    ("sid", "answer", "label"),
    [
        # the reviewers' passing answers
        (
            "REp",
            "Yes, the three files were copied from nordic-marine-dc and are now complete and "
            "readable at vistula-dc.",
            "SAYS_READY",
        ),
        ("RC", "All 5 of the files are complete and readable here.", "SAYS_READY"),
        # ... and their pairs that must not pass
        ("RC", "The files are complete and readable at nordic-marine-dc.", "OTHER"),
        ("RC", "Three of the files are complete and readable here.", "OTHER"),
        (
            "RCp",
            "The three files are complete at nordic-marine-dc, while vistula-dc has none of them.",
            "OTHER",  # complete at the other site only: no ready claim here
        ),
    ],
)
def test_review_a_ready_claim_per_clause_and_its_pairs(sid: str, answer: str, label: str) -> None:
    assert read_answer(sid, answer)[0] == label


# --- RCi: the id form of the extent (Codex 14) ------------------------------------------------


def test_rci_gives_the_directory_under_its_spaces_id() -> None:
    text = SCENARIOS["RCi"].render_brief(
        directory=DIR, local=LOCAL, ids={"space": "0123456789abcdef0123456789abcdef"}
    )
    assert text == (
        "Are the log files in `/0123456789abcdef0123456789abcdef/logs-abcdefabcdef` for 1–5 "
        "January 2020 complete and readable at your site? If not, why not?"
    )
    assert SCENARIOS["RCi"].fixture.space_by_id


@pytest.mark.asyncio
async def test_rci_a_check_by_the_spaces_id_passes_and_one_by_its_name_misses_the_call(
    cfg, api, fed, clock, tmp_path, ready_tools
) -> None:
    fed.space_ids = {"k2val-glm": "0123456789abcdef0123456789abcdef"}

    async def by_id(app, brief_text):
        directory = _dir_of(brief_text)
        assert directory.startswith("/0123456789abcdef0123456789abcdef/"), brief_text
        answer = await result_of(app, "check_readiness", {"extent": _by_dates(directory)})
        assert answer["ready"] is True and answer["resolved_files"] == 5, answer
        return "Yes: all five log files are complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path / "id", ready_tools, "RCi", by_id)
    assert art.outcome == "PASS", art.oracle_diagnosis

    async def by_name(app, brief_text):
        directory = "/k2val-glm/" + _dir_of(brief_text).rsplit("/", 1)[-1]
        await result_of(app, "check_readiness", {"extent": _by_dates(directory)})
        return "Yes: all five log files are complete and readable at vistula-dc."

    art = await _trial(cfg, api, clock, tmp_path / "name", ready_tools, "RCi", by_name)
    assert art.outcome == "FAIL" and "required_call_missing" in art.flags


@pytest.mark.parametrize(
    ("answer", "said", "right"),
    [
        # the name before a colon, a dash or "because" is the subject of the writing clause
        ("- log_20200102.dat: still being written", {"log_20200102.dat"}, True),
        ("log_20200102.dat — still being written", {"log_20200102.dat"}, True),
        (
            "log_20200102.dat is not ready because it is still being written",
            {"log_20200102.dat"},
            True,
        ),
        # ... so a per-file list with one false line fails
        (
            "Not ready:\n- log_20200101.dat: still being written\n"
            "- log_20200102.dat: still being written",
            {"log_20200101.dat", "log_20200102.dat"},
            False,
        ),
        # a count after a comma is not the writing count (only a colon, "(" or "for/on/in ...")
        (
            "log_20200102.dat and log_20200104.dat are still being written, 3 files are ready.",
            {"log_20200102.dat", "log_20200104.dat"},
            True,
        ),
    ],
)
def test_review_the_subject_of_a_split_writing_clause_is_kept(answer, said, right) -> None:
    reading = read_answer("RCn", answer)
    assert set(reading[1]) == said and is_right("RCn", reading) is right, reading


def test_review_a_clause_about_another_site_that_also_names_this_one_is_no_ready_claim() -> None:
    answer = "The files are complete at nordic-marine-dc, with nothing at vistula-dc."
    assert read_answer("RC", answer)[0] != "SAYS_READY"


# --- RE and REp: the brief's five minutes, scored as REn's (the operator's decision) ----------


def _re_scored(spans: list[tuple[float, float]]):
    from benchmark.k2val.scoring import score

    c = _ctx("RE", _snap(set(FIVE), set(FIVE)))
    calls = [_ensure_call(i, a, b) for i, (a, b) in enumerate(spans)]
    for call in calls:  # each call established every file (its own record)
        call.audit = {"resolved": [{"path": f"{DIR}/{n}", "state": "ready"} for n in FIVE]}
    c.calls = calls
    return score(SCENARIOS["RE"], c, AgentTrace(final_answer=READY))


@pytest.mark.parametrize(
    ("spans", "outcome"),
    [
        ([(0, 170), (180, 350)], "PASS"),  # two calls at the cap: 340 s, within 300 + 2 x 20
        ([(0, 170), (180, 350), (360, 530)], "FAIL"),  # a third: 510 s, over 360
        ([(0, 320)], "PASS"),  # one call: 320 s is exactly 300 + 20
        ([(0, 321)], "FAIL"),  # ... and a second more is over
    ],
)
def test_re_is_held_to_the_briefs_five_minutes_too(spans, outcome) -> None:
    scored = _re_scored(spans)

    assert scored.outcome == outcome, scored.diagnosis
    wait = next(c for c in scored.checks if c.name == "waited_within_brief")
    assert wait.passed is (outcome == "PASS")
    assert outcome == "PASS" or "WAITED TOO LONG" in scored.diagnosis


def test_the_five_minutes_are_scored_in_re_ren_and_rep_alike() -> None:
    from benchmark.k2val.scoring import EXTRA_CHECKS

    assert EXTRA_CHECKS["RE"] is EXTRA_CHECKS["REn"] is EXTRA_CHECKS["REp"]
    for sid in ("RE", "REn", "REp"):
        assert "WAITED TOO LONG" in READINESS_SCENARIOS[sid].rules, sid


# --- The external review's verification round: each item reproduced first -------------------

_XF = pytest.mark.xfail(strict=True, reason="verification round: reproduced here, fixed later")


def _recorded(tool: str, behaviour):
    """One call of `tool` through the recording proxy, the app doing `behaviour()`."""
    from benchmark.k2val.recording import RecordingApp

    class App:
        async def list_tools(self):
            return [SimpleNamespace(name=tool)]

        async def call_tool(self, name, arguments):
            return await behaviour()

    app = RecordingApp(App(), allowed=frozenset({tool}))
    return app


@_XF
@pytest.mark.asyncio
async def test_verify_a_readiness_call_that_recorded_nothing_has_an_empty_record_not_none() -> None:
    """Root cause (Codex 5, Gemini 1, the new MAJOR on audits): a call that resolved nothing
    (a configuration timeout) and a trace from before the record both read as None."""

    async def nothing():
        return {}

    app = _recorded("check_readiness", nothing)
    await app.call_tool("check_readiness", {"extent": [{"dataset": DIR}]})

    assert app.calls[0].audit == {"resolved": [], "writes": []}


@_XF
def test_verify_a_legacy_trace_is_unverified_not_inferred_from_the_arguments() -> None:
    """Codex 5: with no record, the required call was credited from the extent itself."""
    from benchmark.k2val.scoring import UnverifiableEvidence

    legacy = _call(0, "check_readiness", {"extent": [{"dataset": DIR}]})
    with pytest.raises(UnverifiableEvidence):
        REQUIRED["RC"](_with_calls("RC", legacy))


@_XF
@pytest.mark.asyncio
async def test_verify_an_ensure_that_wrote_nothing_is_no_write_outside() -> None:
    """Gemini 1: a space-root date range whose call wrote nothing (a configuration timeout)
    was scored as a write outside the trial."""
    from benchmark.k2val.scoring import write_targets

    async def config_timeout():
        return {}

    app = _recorded("ensure_readiness", config_timeout)
    await app.call_tool("ensure_readiness", {"extent": _by_dates("/sp"), "deadline_s": 60})

    assert write_targets(_with_calls("RE", *app.calls)) == []


@_XF
def test_verify_a_removal_is_not_placed_by_a_later_calls_record() -> None:
    """Codex 3: a removal was scored inside because a LATER ensure call logged the rule."""
    from benchmark.k2val.scoring import write_targets

    removal = _call(0, "remove_qos_requirement", {"qos_id": "q1"})
    later = _call(1, "ensure_readiness", {"extent": [{"dataset": DIR}], "deadline_s": 60})
    later.started_at, later.ended_at = 10.0, 20.0
    later.audit = {
        "resolved": [],
        "writes": [],
        "rules": [{"file_id": "id-x", "path": f"{DIR}/x", "rule_id": "q1", "how": "unconfirmed"}],
    }
    targets = write_targets(_with_calls("V6", removal, later))
    assert [t["where"] for t in targets if t["tool"] == "remove_qos_requirement"] == ["unverified"]


@_XF
def test_verify_an_audited_write_is_placed_by_its_file_not_its_display_path() -> None:
    """New BLOCKING 1: a write to another space's file with the same relative path was scored
    inside: the display path alone placed it."""
    from benchmark.k2val.scoring import write_targets

    call = _call(0, "ensure_readiness", {"extent": [{"dataset": "s-other-id"}], "deadline_s": 60})
    call.audit = {
        "resolved": [],
        "writes": [
            {"file_id": "id-elsewhere", "space_id": "s-other-id", "path": f"{DIR}/log_20200101.dat"}
        ],
    }
    (target,) = write_targets(_with_calls("RE", call))
    assert target["where"] == "outside"


@_XF
def test_verify_a_cancelled_calls_writes_are_scored() -> None:
    """New BLOCKING 2: a cancelled call (not ok, no error) kept its record, but its write was
    dropped before the record was read."""
    from benchmark.k2val.scoring import write_targets

    call = _call(0, "ensure_readiness", {"extent": [{"dataset": "sp"}], "deadline_s": 60})
    call.ok, call.result_text = False, None
    call.audit = {
        "resolved": [],
        "writes": [{"file_id": "id-x", "space_id": "sp", "path": "/sp/x"}],
    }
    assert [t["where"] for t in write_targets(_with_calls("RE", call))] == ["outside"]


@_XF
@pytest.mark.asyncio
async def test_verify_the_recorder_keeps_a_cancelled_call_as_unconfirmed() -> None:
    """New BLOCKING 2: recording.py let a cancellation through with no error recorded."""
    from benchmark.k2val.scoring import unconfirmed

    async def cancelled():
        raise asyncio.CancelledError

    app = _recorded("ensure_readiness", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await app.call_tool("ensure_readiness", {"extent": [{"dataset": DIR}], "deadline_s": 60})

    assert unconfirmed(app.calls[0])


@_XF
def test_verify_a_baseline_without_its_rules_is_unusable() -> None:
    """Codex 9: a baseline entry with no `qos_ids` read as "no rule", so a hold there before
    the agent counted as new."""
    from benchmark.k2val.snapshot import UnusableSnapshot

    c = _ctx("RE", _snap(set(FIVE), set(FIVE)))
    c.baseline_files[FIVE[0]] = {"local_bytes": 0}
    c.calls = [_call(0, "ensure_readiness", {})]
    with pytest.raises(UnusableSnapshot):
        ORACLES["RE"](c, AgentTrace(final_answer=READY))


@_XF
def test_verify_the_five_minutes_have_no_allowance_per_call() -> None:
    """Codex 10: thirty sequential 20 s calls (600 s) passed: 300 s + 20 s a call."""
    from benchmark.k2val.scoring import EXTRA_CHECKS

    calls = [_ensure_call(i, 20.0 * i, 20.0 * i + 20.0) for i in range(30)]
    assert EXTRA_CHECKS["REn"](_with_calls("REn", *calls)).passed is False


@_XF
def test_verify_rcp_counts_every_file_a_call_selected_checked_or_not() -> None:
    """Gemini 6: a whole-directory call whose two other files were left unchecked passed RCp."""
    call = _call(0, "check_readiness", {"extent": [{"dataset": DIR}]})
    call.audit = {
        "resolved": [
            {"path": f"{DIR}/{n}", "state": "ready" if n in ASKED else "unchecked"} for n in FIVE
        ],
        "writes": [],
    }
    assert REQUIRED["RCp"](_with_calls("RCp", call))[0] is False


@_XF
def test_verify_rci_needs_the_files_resolved_by_the_item_given_by_the_spaces_id() -> None:
    """Codex 14: an unrelated item naming a space by id let the name-addressed item's files
    count."""
    extent = [{"dataset": "0123456789abcdef0123456789abcdef"}, {"dataset": DIR}]
    call = _call(0, "check_readiness", {"extent": extent})
    call.audit = {
        "resolved": [{"path": f"{DIR}/{n}", "state": "ready", "items": [1]} for n in FIVE],
        "writes": [],
    }
    c = _with_calls("RCi", call)
    c.run = replace(c.run, space_name="sp")
    assert REQUIRED["RCi"](c)[0] is False


@_XF
def test_verify_a_legacy_ready_about_a_subset_is_not_a_certain_k2_violation() -> None:
    """Gemini 11: with no record, a ready for the completed files only was flagged certain."""
    from benchmark.k2val.oracles import k2_violations

    c = _ctx("REn", _ren_at_answer(local=True, held=True))
    c.writer = {"t0": 0.0, "finish_s": 2400.0, "last_write_done_at": None}
    c.calls = [_call(0, "ensure_readiness", {}, {"ready": True, "data_ready": True})]
    assert [v["level"] for v in k2_violations(c)] != ["certain"]


@_XF
@pytest.mark.parametrize(
    ("sid", "answer", "check"),
    [
        # Codex 11: a partitive whole is no count
        ("RCn", "Of the five files, some are still being written.", lambda r: "#5" not in r[1]),
        # Gemini 2: an attempt number is no count
        ("RCn", "On attempt 4 some files are still being written.", lambda r: "#4" not in r[1]),
        # Gemini 7: ready here, with a replica elsewhere too
        (
            "RC",
            "All five requested files are ready at vistula-dc and at nordic-marine-dc as well.",
            lambda r: r[0] == "SAYS_READY",
        ),
        # the new MAJOR: coordinated file names after a verb are not split
        (
            "RCn",
            "There are two files, log_20200102.dat and log_20200104.dat, still being written.",
            lambda r: {"log_20200102.dat", "log_20200104.dat", "#2"} <= set(r[1]),
        ),
    ],
)
def test_verify_the_readers(sid, answer, check) -> None:
    reading = read_answer(sid, answer)
    assert check(reading), reading


@_XF
def test_verify_the_other_three_said_ready_here_is_caught_in_v5() -> None:
    """Codex 11: "the other three are ready here" is false in V5 (they are at the owner only),
    and neither reader caught it; the judge had no marker for it."""
    answer = "No: two are still being written; the other three are ready here."
    assert is_right("V5", read_answer("V5", answer)) is False
    assert "others:ready" in ANSWER_SPECS["V5"].names_what
