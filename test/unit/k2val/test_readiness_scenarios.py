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

IDS = ("RC", "RCn", "RCo", "RCp", "RE", "REn", "REo", "REp")
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
    for group in (("V4", "V5", "RC", "RCn", "RCp"), ("RE", "REn", "REp"), ("V1", "RCo")):
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
    for sid in ("RC", "RCn", "RCp"):
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

    assert seen["answer"]["ready"] is True and seen["answer"]["files"] == 5
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
        assert answer["ready"] is True and answer["files"] == 3, answer
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
    assert "Call again to keep waiting." in answer["advice"]
    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is None


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


@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
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
        )
    return Snapshot(taken_at=1.0, files=files)


@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
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


@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
def test_review_rcp_a_check_of_the_whole_directory_is_not_a_check_of_the_three() -> None:
    """Codex 5, Gemini 6: RCp credited a check of all five (`want <= selected`); the answer
    'yes' then came from subtracting the two examples."""
    whole = _call(0, "check_readiness", {"extent": [{"dataset": DIR}]})

    ok, _ = REQUIRED["RCp"](_with_calls("RCp", whole))

    assert ok is False


@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
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
@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
def test_review_the_writing_reader_binds_counts_to_the_files_they_count(
    sid: str, answer: str, right: bool
) -> None:
    assert is_right(sid, read_answer(sid, answer)) is right, read_answer(sid, answer)


def test_review_both_with_the_two_names_is_the_right_count() -> None:
    """Gemini 2: the deterministic reader already counted "both" as #2 (not a defect there)."""
    answer = "Not ready: both log_20200102.dat and log_20200104.dat are still being written."
    assert is_right("RCn", read_answer("RCn", answer)) is True


@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
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
@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
def test_review_a_ready_claim_is_read_per_clause(sid: str, answer: str) -> None:
    assert read_answer(sid, answer)[0] == "SAYS_READY"


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
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
@pytest.mark.xfail(strict=True, reason="external review: reproduced here, fixed later")
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
