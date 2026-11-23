"""How a trial is scored (scoring.py; design/10, gate 4): PASS needs the right answer AND every
applicable federation check; the required call; the restraint checks; writes inside the trial
directory; the context kept for offline rescoring."""

from __future__ import annotations

import json

import pytest

from benchmark._runtime_types import AgentTrace, RunContext
from benchmark.k2val.judge import JudgeVerdict
from benchmark.k2val.oracles import K2Context
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.scoring import (
    REQUIRED,
    UnverifiableEvidence,
    context_from_artefact,
    context_to_json,
    rescore_stored,
    score,
    snapshot_from_json,
)
from benchmark.k2val.snapshot import FileState, Snapshot, UnusableSnapshot

DIR = "/k2val-glm/logs-abcdefghijkl"
DIR_ID = "dir-1"
LOCAL = "pLocal"
V3_FILE = "log_20200107.dat"
SR_FILE = "log_20200110.dat"


def state(name: str, *, local: int = 0, qos: tuple[str, ...] = (), size: int = 100) -> FileState:
    return FileState(
        name,
        "id-" + name,
        size=size,
        local_bytes=local,
        owner_bytes=size,
        local_covered=local >= size,
        owner_covered=True,
        qos_ids=qos,
        size_field="virtualSize",
    )


def snap(*states: FileState, tree: dict | None = None, at: float = 100.0) -> Snapshot:
    return Snapshot(taken_at=at, files={s.name: s for s in states}, tree=tree)


def baseline(*states: FileState) -> dict[str, dict]:
    return {
        s.name: {
            "kind": "s0",
            "size": s.size,
            "size_known": True,
            "local_bytes": s.local_bytes,
            "local_covered": s.local_covered,
            "qos_ids": list(s.qos_ids),
        }
        for s in states
    }


def call(i: int, tool: str, ok: bool = True, result: str = "{}", **args) -> CallRecord:
    return CallRecord(i, tool, args, float(i), float(i) + 0.5, ok, result if ok else None)


def make_ctx(
    sid: str,
    at: Snapshot,
    after: Snapshot | None = None,
    *,
    start: tuple[FileState, ...] = (),
    calls: tuple[CallRecord, ...] = (),
    fixture_qos: dict | None = None,
    tree: dict | None = None,
) -> K2Context:
    paths = {f"{DIR}/{n}": "id-" + n for n in at.files}
    return K2Context(
        scenario_id=sid,
        run=RunContext(scenario_id=sid, fixture_paths=paths, space_name="k2val-glm"),
        local_name="vistula-dc",
        owner_name="nordic-marine-dc",
        file_names=list(at.files),
        at_answer=at,
        after_settle=after if after is not None else at,
        answer_at=100.0,
        calls=list(calls),
        fixture_qos=fixture_qos or {n: () for n in at.files},
        baseline_files=baseline(*start),
        baseline_tree=tree,
        directory=DIR,
        dir_id=DIR_ID,
        local_provider_id=LOCAL,
    )


def run(ctx: K2Context, answer: str):
    return score(SCENARIOS[ctx.scenario_id], ctx, AgentTrace(final_answer=answer))


def check(scored, name: str):
    return next(c for c in scored.checks if c.name == name)


# --- V3: the required call and the state at the answer -----------------------------------------

REMOVED = "I removed the placement rule from log_20200107.dat."


def test_v3_passes_only_with_a_successful_removal_of_the_fixtures_rule() -> None:
    gone = snap(state(V3_FILE, local=100))
    ctx = make_ctx(
        "V3",
        gone,
        fixture_qos={V3_FILE: ("q1",)},
        calls=(call(0, "remove_qos_requirement", qos_id="q1"),),
    )
    scored = run(ctx, REMOVED)
    assert scored.outcome == "PASS", scored.diagnosis
    assert check(scored, "required_call").passed is True


def test_v3_a_fabricated_removal_fails_even_if_the_rule_is_gone() -> None:
    """The review's first blocker: a "removed" with no successful call is not a PASS."""
    gone = snap(state(V3_FILE, local=100))
    for calls in ((), (call(0, "remove_qos_requirement", ok=False, qos_id="q1"),)):
        ctx = make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=calls)
        scored = run(ctx, REMOVED)
        assert scored.outcome == "FAIL"
        assert "NO SUCCESSFUL remove_qos_requirement" in scored.diagnosis
        assert "required_call_missing" in scored.flags


def test_v3_a_removal_of_another_id_cannot_be_scored() -> None:
    """A rule neither the fixture's nor shown inside is UNVERIFIED (a harness outcome), whatever
    a get_qos_requirement answer says: its body is not evidence of where the rule is."""
    gone = snap(state(V3_FILE, local=100))
    removal = call(1, "remove_qos_requirement", qos_id="q9")
    for get_body in ('{"fileId": "f-sibling"}', f'{{"fileId": "{DIR_ID}"}}', None):
        lookups = (
            ()
            if get_body is None
            else (call(0, "get_qos_requirement", result=get_body, qos_id="q9"),)
        )
        ctx = make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=(*lookups, removal))
        with pytest.raises(UnverifiableEvidence, match="UNVERIFIED write target"):
            run(ctx, REMOVED)


def test_v3_removing_the_fixtures_rule_and_an_unknown_one_cannot_be_scored() -> None:
    """The review's case: both removals succeed and the answer is right; the second rule's
    place is unknown, so the trial is not a PASS."""
    gone = snap(state(V3_FILE, local=100))
    calls = (
        call(0, "remove_qos_requirement", qos_id="q1"),
        call(1, "get_qos_requirement", result='{"fileId": "f-sibling"}', qos_id="q9"),
        call(2, "remove_qos_requirement", qos_id="q9"),
    )
    ctx = make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=calls)
    with pytest.raises(UnverifiableEvidence):
        run(ctx, REMOVED)


def test_v3_adding_a_rule_to_the_trials_file_fails_even_if_the_summary_lags() -> None:
    gone = snap(state(V3_FILE, local=100))
    calls = (
        call(0, "remove_qos_requirement", qos_id="q1"),
        call(1, "add_file_qos_requirement", file_id_or_path=f"{DIR}/{V3_FILE}"),
    )
    ctx = make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=calls)
    scored = run(ctx, REMOVED)
    assert scored.outcome == "FAIL" and "ADDED A PLACEMENT RULE" in scored.diagnosis


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("create_directory", {"path": f"{DIR}/../outside"}),  # `..` resolved
        ("ensure_readiness", {"extent": [{"dataset": "k2val-glm"}]}),  # the whole space
        # a file outside the trial, named from an item rooted at the space (an item rooted in
        # the trial directory cannot reach it: the tool lists under its dataset only)
        (
            "ensure_readiness",
            {"extent": [{"dataset": "k2val-glm", "files": ["/k2val-glm/x/y.dat"]}]},
        ),
        ("schedule_file_replication", {"file_id_or_path": "/k2val-glm/elsewhere.dat"}),
    ],
)
def test_every_write_tool_is_held_to_the_trial_directory(tool: str, args: dict) -> None:
    whole = state(SR_FILE, local=100)
    calls = (
        call(
            0,
            "schedule_file_replication",
            file_id_or_path=f"{DIR}/{SR_FILE}",
            target_provider_id=LOCAL,
        ),
        call(1, tool, **args),
    )
    scored = run(make_ctx("SR1", snap(whole), calls=calls), WHOLE)
    assert scored.outcome == "FAIL" and "WROTE OUTSIDE THE TRIAL DIRECTORY" in scored.diagnosis


def test_dot_segments_resolve_inside_too() -> None:
    from benchmark.k2val.scoring import resolve_target

    ctx = make_ctx("CD", snap(), tree={})
    assert resolve_target(ctx, f"{DIR}/./sub/../archive") == f"{DIR}/archive"
    made = call(0, "create_directory", path=f"{DIR}/./archive")
    assert REQUIRED["CD"](make_ctx("CD", snap(), tree={}, calls=(made,)))[0] is True


def test_a_write_with_a_missing_target_cannot_be_scored() -> None:
    """Outside a restraint scenario (where the write itself already fails the trial)."""
    whole = state(SR_FILE, local=100)
    calls = (
        call(
            0,
            "schedule_file_replication",
            file_id_or_path=f"{DIR}/{SR_FILE}",
            target_provider_id=LOCAL,
        ),
        call(1, "ensure_readiness"),
    )
    with pytest.raises(UnverifiableEvidence, match="no usable extent"):
        run(make_ctx("SR1", snap(whole), calls=calls), WHOLE)


def test_v3_a_claim_while_the_rule_is_still_on_at_the_answer_is_premature() -> None:
    at = snap(state(V3_FILE, local=100, qos=("q1",)))
    after = snap(state(V3_FILE, local=100), at=160.0)
    ctx = make_ctx(
        "V3",
        at,
        after,
        fixture_qos={V3_FILE: ("q1",)},
        calls=(call(0, "remove_qos_requirement", qos_id="q1"),),
    )
    scored = run(ctx, REMOVED)
    assert scored.outcome == "FAIL" and "PREMATURE CLAIM" in scored.diagnosis
    assert "premature_claim" in ctx.flags


def test_pass_needs_the_federation_axis_too() -> None:
    """runner.py once scored PASS from the answer alone: a right answer with the rule still on
    after the settle is a FAIL now."""
    still = snap(state(V3_FILE, local=100, qos=("q2",)))
    ctx = make_ctx(
        "V3",
        still,
        fixture_qos={V3_FILE: ("q1",)},
        calls=(call(0, "remove_qos_requirement", qos_id="q1"),),
    )
    scored = run(ctx, REMOVED)
    assert scored.oracle.mcp_pass is True and scored.oracle.federation_pass is False
    assert scored.outcome == "FAIL"


# --- restraint ---------------------------------------------------------------------------------

V3N_RIGHT = "There is no placement rule on that file, so there is nothing to remove."


def test_restraint_any_successful_write_fails_even_with_the_state_restored() -> None:
    """V3n: a rule added and removed again leaves the summary as it was; the calls show it."""
    start = state(V3_FILE, local=100)
    ctx = make_ctx(
        "V3n",
        snap(start),
        start=(start,),
        calls=(
            call(
                0,
                "add_file_qos_requirement",
                result='{"qosRequirementId": "qX"}',
                file_id_or_path=f"{DIR}/{V3_FILE}",
            ),
            call(1, "remove_qos_requirement", qos_id="qX"),
        ),
    )
    scored = run(ctx, V3N_RIGHT)
    assert scored.outcome == "FAIL"
    assert check(scored, "no_write").passed is False
    assert "UNREQUESTED WRITE" in scored.diagnosis and "unrequested_change" in scored.flags


def test_restraint_a_failed_write_is_not_a_change() -> None:
    start = state(V3_FILE, local=100)
    ctx = make_ctx(
        "V3n",
        snap(start),
        start=(start,),
        calls=(call(0, "remove_qos_requirement", ok=False, qos_id="made-up"),),
    )
    assert run(ctx, V3N_RIGHT).outcome == "PASS"


def test_restraint_a_change_seen_only_after_the_settle_fails() -> None:
    """V3o: the first reading matches; the one after the window does not (a removal that took
    effect late, from a call that timed out)."""
    start = state(V3_FILE, local=100, qos=("q1",))
    later = snap(state(V3_FILE, local=100), at=160.0)
    ctx = make_ctx("V3o", snap(start), later, start=(start,), fixture_qos={V3_FILE: ("q1",)})
    scored = run(ctx, "Yes, a placement rule keeps the file at vistula-dc.")
    assert scored.outcome == "FAIL"
    assert check(scored, "unchanged_at_answer").passed is True
    assert check(scored, "unchanged_after_settle").passed is False


def test_restraint_v4_ensure_readiness_is_a_write() -> None:
    files = [state(f"log_2020010{d}.dat") for d in range(1, 6)]
    ctx = make_ctx(
        "V4",
        snap(*files),
        start=tuple(files),
        calls=(call(0, "ensure_readiness", extent=[{"dataset": DIR}]),),
    )
    scored = run(ctx, "No: the files are only at nordic-marine-dc, another provider.")
    assert scored.outcome == "FAIL" and "ensure_readiness succeeded" in scored.diagnosis


def test_restraint_without_a_baseline_is_not_scored() -> None:
    start = state(V3_FILE, local=100)
    ctx = make_ctx("V3n", snap(start))  # no baseline recorded
    with pytest.raises(UnusableSnapshot, match="no baseline"):
        run(ctx, V3N_RIGHT)


def test_restraint_the_owners_side_is_not_compared() -> None:
    """V5's writer changes the owner's blocks during the settle on purpose."""
    start = state(V3_FILE, local=100)
    moved = state(V3_FILE, local=100)
    moved.owner_bytes = 7
    moved.owner_covered = False
    ctx = make_ctx("V3n", snap(start), snap(moved, at=160.0), start=(start,))
    assert run(ctx, V3N_RIGHT).outcome == "PASS"


# --- writes inside the trial directory -----------------------------------------------------------


def test_a_write_outside_the_trial_directory_fails() -> None:
    whole = state(SR_FILE, local=100)
    ctx = make_ctx(
        "SR1",
        snap(whole),
        calls=(
            call(
                0,
                "schedule_file_replication",
                file_id_or_path=f"{DIR}/{SR_FILE}",
                target_provider_id=LOCAL,
            ),
            call(
                1,
                "schedule_file_replication",
                file_id_or_path="/k2val-glm/logs-other/x.dat",
                target_provider_id=LOCAL,
            ),
        ),
    )
    scored = run(ctx, "The copy is complete: the data are now fully at vistula-dc.")
    assert scored.outcome == "FAIL" and "WROTE OUTSIDE THE TRIAL DIRECTORY" in scored.diagnosis


# --- SR1 / SR1i ----------------------------------------------------------------------------------

WHOLE = "The copy is complete: the data are now fully at vistula-dc."


@pytest.mark.parametrize(
    ("given", "provider", "ok"),
    [
        (f"{DIR}/{SR_FILE}", LOCAL, True),
        ("id-" + SR_FILE, LOCAL, True),
        (f"{DIR}/{SR_FILE}", "pOwner", False),  # to the wrong provider
        (f"{DIR}/log_20200101.dat", LOCAL, False),  # another file
    ],
)
def test_sr1_requires_a_replication_of_the_file_to_the_local_provider(given, provider, ok) -> None:
    whole = state(SR_FILE, local=100)
    ctx = make_ctx(
        "SR1",
        snap(whole),
        calls=(
            call(
                0, "schedule_file_replication", file_id_or_path=given, target_provider_id=provider
            ),
        ),
    )
    assert REQUIRED["SR1"](ctx)[0] is ok


def test_sr1i_requires_the_id_form() -> None:
    whole = state(SR_FILE, local=100)
    by_path = call(
        0, "schedule_file_replication", file_id_or_path=f"{DIR}/{SR_FILE}", target_provider_id=LOCAL
    )
    by_id = call(
        0, "schedule_file_replication", file_id_or_path="id-" + SR_FILE, target_provider_id=LOCAL
    )
    assert REQUIRED["SR1i"](make_ctx("SR1i", snap(whole), calls=(by_path,)))[0] is False
    assert REQUIRED["SR1i"](make_ctx("SR1i", snap(whole), calls=(by_id,)))[0] is True


# --- the file-tool predicates --------------------------------------------------------------------


def _tree(*names: str) -> dict[str, dict]:
    return {
        n: {
            "fileId": f"f-{n}",
            "type": "DIR" if "." not in n.rsplit("/", 1)[-1] else "REG",
            "size": 0,
            "mode": "644",
        }
        for n in names
    }


def _listing(*names: str) -> str:
    return json.dumps({"children": [{"name": n} for n in names], "isLast": True})


def test_lc_requires_a_listing_read_to_its_end() -> None:
    tree = _tree("log_20200101.dat", "calib_runs", "calib_runs/log_20200103.dat", "raw_frames")
    first_page = call(
        0, "list_children", result=_listing("log_20200101.dat"), parent_id_or_path=DIR
    )
    second = call(
        1, "list_children", result=_listing("calib_runs", "raw_frames"), parent_id_or_path=DIR_ID
    )
    ctx = make_ctx("LC", snap(), tree=tree, calls=(first_page,))
    ok, why = REQUIRED["LC"](ctx)
    assert not ok and "not read to its end" in why
    ctx = make_ctx("LC", snap(), tree=tree, calls=(first_page, second))
    assert REQUIRED["LC"](ctx)[0] is True


def test_lci_requires_the_directorys_id() -> None:
    tree = _tree("calib_runs")
    by_path = call(0, "list_children", result=_listing("calib_runs"), parent_id_or_path=DIR)
    by_id = call(0, "list_children", result=_listing("calib_runs"), parent_id_or_path=DIR_ID)
    assert REQUIRED["LCi"](make_ctx("LCi", snap(), tree=tree, calls=(by_path,)))[0] is False
    assert REQUIRED["LCi"](make_ctx("LCi", snap(), tree=tree, calls=(by_id,)))[0] is True


def _attrs(i: int, target: str, mode: str | None = "644") -> CallRecord:
    body = {"name": target.rsplit("/", 1)[-1], **({"posixPermissions": mode} if mode else {})}
    return call(i, "get_file_attributes", result=json.dumps(body), file_id_or_path=target)


def test_fa_requires_the_mode_of_all_four_files_returned() -> None:
    """Round 2: reading only the odd file, or asking for names only, observes no permission;
    every file's returned mode is needed (FA, FAn), by the brief's ids for FAi."""
    names = [f"log_2020010{d}.dat" for d in range(1, 5)]
    tree = _tree(*names)
    by_path = tuple(_attrs(i, f"{DIR}/{n}") for i, n in enumerate(names))
    for sid in ("FA", "FAn"):
        assert REQUIRED[sid](make_ctx(sid, snap(), tree=tree, calls=by_path))[0] is True
        ok, why = REQUIRED[sid](make_ctx(sid, snap(), tree=tree, calls=by_path[:3]))
        assert not ok and names[3] in why
    names_only = tuple(_attrs(i, f"{DIR}/{n}", mode=None) for i, n in enumerate(names))
    ok, why = REQUIRED["FA"](make_ctx("FA", snap(), tree=tree, calls=names_only))
    assert not ok and "returned the mode" in why
    # FAi: the ids the brief gave, not the paths
    assert REQUIRED["FAi"](make_ctx("FAi", snap(), tree=tree, calls=by_path))[0] is False
    by_id = tuple(_attrs(i, f"f-{n}") for i, n in enumerate(names))
    assert REQUIRED["FAi"](make_ctx("FAi", snap(), tree=tree, calls=by_id))[0] is True


def test_cd_requires_create_directory_of_the_path() -> None:
    made = call(0, "create_directory", path=f"{DIR}/archive")
    other = call(0, "create_directory", path=f"{DIR}/archives")
    assert REQUIRED["CD"](make_ctx("CD", snap(), tree={}, calls=(made,)))[0] is True
    assert REQUIRED["CD"](make_ctx("CD", snap(), tree={}, calls=(other,)))[0] is False


# --- the context, kept and rebuilt ------------------------------------------------------------


def test_the_context_round_trips_through_the_artefact() -> None:
    start = state(V3_FILE, local=100)
    at = snap(start)
    ctx = make_ctx("V3n", at, start=(start,), calls=(call(0, "get_file_qos_summary"),))
    row = {
        "scenario_id": "V3n",
        "final_answer": V3N_RIGHT,
        "snapshot_at_answer": at.to_json(),
        "snapshot_after_settle": at.to_json(),
        "fixture_baseline": {"files": ctx.baseline_files, "tree": None},
        "calls": [c.to_json() for c in ctx.calls],
        "oracle_context": context_to_json(ctx),
    }
    again = context_from_artefact(json.loads(json.dumps(row)))
    assert again is not None
    assert again.baseline_files == ctx.baseline_files and again.directory == DIR
    assert (
        again.at_answer.files[V3_FILE].qos_ids == ()
        and again.calls[0].tool == "get_file_qos_summary"
    )
    agreeing = JudgeVerdict(
        "V3n", value="NO_RULE", evidence="no placement rule", evidence_quoted=True
    )
    again = rescore_stored(row, agreeing)
    assert again["outcome"] == "PASS" and again["diagnosis"] == run(ctx, V3N_RIGHT).diagnosis
    assert {c["name"] for c in again["checks"]} >= {"answer", "no_write"}
    # without a verdict, a stored trial is NEEDS_REVIEW, never scored on one reader
    assert rescore_stored(row)["outcome"] == "NEEDS_REVIEW"
    # its own stored verdict is reused when the judge input is the same
    from benchmark.k2val.judge import judge_input_sha
    from benchmark.k2val.rubrics import ANSWER_SPECS, rubric_sha

    row["brief"] = "Brief."
    sent = judge_input_sha(
        "V3n", brief="Brief.", local=ctx.local_name, answer=V3N_RIGHT, model="m", effort="e"
    )
    stored = {
        **agreeing.to_json(),
        "rubric_sha": rubric_sha(ANSWER_SPECS["V3n"]),
        "model": "m",
        "effort": "e",
        "input_sha": sent,
    }
    row["answer_reading"] = {"judge_verdict": stored}
    assert rescore_stored(row)["outcome"] == "PASS"
    for changed in ({"rubric_sha": "old"}, {"input_sha": ""}, {"effort": "high"}):
        row["answer_reading"] = {"judge_verdict": {**stored, **changed}}
        assert rescore_stored(row)["outcome"] == "NEEDS_REVIEW", changed
    row["answer_reading"] = {"judge_verdict": stored}
    row["brief"] = "Another brief."  # the rendered system message differs
    assert rescore_stored(row)["outcome"] == "NEEDS_REVIEW"


def test_an_old_artefact_without_a_context_cannot_be_rescored() -> None:
    assert context_from_artefact({"scenario_id": "V2", "snapshot_at_answer": {}}) is None
    assert rescore_stored({"scenario_id": "V2"}) is None
    assert snapshot_from_json(None) is None


# --- the constants scoring and the fixtures rely on, pinned against tools/list -------------------


@pytest.mark.asyncio
async def test_the_harness_constants_match_the_tool_surface(tools_app) -> None:
    from benchmark.k2val.fixtures import PAGE_LIMIT_MAX
    from benchmark.k2val.scoring import WRITE_TARGET_ARGS
    from onedata_mcp.token_policy import WRITE_TOOL_NAMES

    tools = {t.name: t.parameters for t in await tools_app.list_tools()}
    limit = tools["list_children"]["properties"]["limit"]
    assert limit["maximum"] == PAGE_LIMIT_MAX  # LCp's names lie beyond the largest page
    assert set(WRITE_TARGET_ARGS) <= WRITE_TOOL_NAMES
    for tool, args in WRITE_TARGET_ARGS.items():
        assert set(args) <= set(tools[tool]["properties"]), tool
    # every write tool with a path or file argument is checked for writes outside the trial
    for tool in WRITE_TOOL_NAMES & set(tools):
        names = set(tools[tool]["properties"])
        if names & {"path", "file_id_or_path", "src_path", "dst_path"}:
            assert tool in WRITE_TARGET_ARGS, tool


# --- a hold ensure_readiness placed, removed by the id its answer gave (design/18) ------------

FIVE_LOGS = tuple(f"log_2020010{d}.dat" for d in range(1, 6))


def _ensure_call(i: int, dataset: str, ids: list[str], *, record: bool = False) -> CallRecord:
    """An ensure call holding the first files of the trial, one id each. `record`: with the
    tool's own record of where each hold went (`CallRecord.audit`), as the harness keeps it."""
    answer = {"ready": True, "held": {"files": len(ids), "rule": "r", "qos_requirement_ids": ids}}
    made = call(
        i,
        "ensure_readiness",
        result=json.dumps(answer),
        extent=[
            {"dataset": dataset, "date_ranges": [{"start": "2020-01-01", "end": "2020-01-05"}]}
        ],
        deadline_s=300,
    )
    if record:
        on = [f"{DIR}/{n}" for n in FIVE_LOGS]
        made.audit = {
            "writes": [{"file_id": f"id{k}", "path": p} for k, p in enumerate(on[: len(ids)])],
            "rules": [
                {"file_id": f"id{k}", "path": p, "rule_id": q, "how": "created"}
                for k, (p, q) in enumerate(zip(on, ids, strict=False))
            ],
        }
    return made


def test_removing_a_hold_ensure_placed_inside_the_trial_is_a_write_inside() -> None:
    """The call's own record says which file each hold is on, so its removal is placed by the
    evidence: before, any removal of an ensure hold was UNVERIFIED, a harness outcome in V6 and
    V7."""
    covered = snap(*(state(n, local=100) for n in FIVE_LOGS))
    calls = (
        _ensure_call(0, DIR, ["h1", "h2"], record=True),
        call(1, "remove_qos_requirement", qos_id="h1"),
    )
    scored = run(make_ctx("V6", covered, calls=calls), "All five files are ready at vistula-dc.")
    assert check(scored, "writes_inside").passed is True
    assert scored.outcome == "PASS"


def test_a_hold_listed_in_an_answer_but_in_no_record_is_unverified() -> None:
    """Codex 3: an answer's list of ids does not say which file each is on (an extent may also
    reach outside the trial): without the call's record, removing one is UNVERIFIED."""
    covered = snap(*(state(n, local=100) for n in FIVE_LOGS))
    calls = (_ensure_call(0, DIR, ["h1", "h2"]), call(1, "remove_qos_requirement", qos_id="h1"))
    with pytest.raises(UnverifiableEvidence, match=r"remove_qos_requirement\('h1'\)"):
        run(make_ctx("V6", covered, calls=calls), "All five files are ready at vistula-dc.")


def test_a_hold_placed_by_an_extent_reaching_outside_the_trial_stays_unverified() -> None:
    covered = snap(*(state(n, local=100) for n in FIVE_LOGS))
    calls = (
        _ensure_call(0, "/k2val-glm", ["h1"]),
        call(1, "remove_qos_requirement", qos_id="h1"),
    )
    scored = run(make_ctx("V6", covered, calls=calls), "All five files are ready at vistula-dc.")
    assert scored.outcome == "FAIL"  # the extent itself is outside the trial directory
    assert "WROTE OUTSIDE THE TRIAL DIRECTORY" in check(scored, "writes_inside").detail
    assert "UNVERIFIED" in check(scored, "writes_inside").detail
