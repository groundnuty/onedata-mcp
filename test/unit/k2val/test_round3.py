"""The third review round: the surface hash before dispatch, ownership by construction and
attempted writes, the snapshot's consistency, the ordered quote, the judge's input digest, the
report from the run plan and the CD reader's target."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmark._runtime_types import AgentTrace
from benchmark.k2val.artefact import K2TrialArtefact, write_trial
from benchmark.k2val.config import K1_TOOLS, K2_TOOLS
from benchmark.k2val.file_oracles import cd_label
from benchmark.k2val.file_scenarios import FILE_TOOLS
from benchmark.k2val.judge import (
    ClaudeSdkJudge,
    JudgeConf,
    JudgeVerdict,
    evidence_quoted,
    judge_input_sha,
)
from benchmark.k2val.reading import read_answer
from benchmark.k2val.report import build_report
from benchmark.k2val.runner import run_trial
from benchmark.k2val.scenarios import SCENARIOS, TOOL_UNDER_TEST
from benchmark.k2val.scoring import UnverifiableEvidence, score, write_targets
from benchmark.k2val.snapshot import Snapshot, _take_tree
from benchmark.k2val.surfaces import RECORDED_SURFACES, surface_problem, surface_sha
from test.unit.k2val.test_runner import make_deps, static
from test.unit.k2val.test_scoring import (
    DIR,
    REMOVED,
    SR_FILE,
    V3_FILE,
    V3N_RIGHT,
    call,
    make_ctx,
    snap,
    state,
)

ROOT = Path(__file__).resolve().parents[3]


def run(ctx, answer: str):
    return score(SCENARIOS[ctx.scenario_id], ctx, AgentTrace(final_answer=answer))


# --- B1: the tool under test, and the surface hash before dispatch ----------------------------


def test_v1_is_get_file_attributes_offered_but_not_needed_trial() -> None:
    assert TOOL_UNDER_TEST["V1"] == "get_file_attributes"


@pytest.mark.asyncio
async def test_the_recorded_surfaces_are_the_in_process_listing(tools_app) -> None:
    listing = await tools_app.list_tools()
    for names, (_, recorded) in RECORDED_SURFACES.items():
        assert surface_sha([t for t in listing if t.name in names]) == recorded


def test_the_hash_is_make_tools_manifest_byte_for_byte() -> None:
    """`make -s tools-manifest` (scripts/tools_manifest.py) with the selection, hashed: the
    records' hash, so the runner's check compares like with like."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ONEDATA_", "OTEL_"))}
    env["ONEDATA_MCP_TOOLS"] = ",".join(FILE_TOOLS)
    out = subprocess.run(
        [sys.executable, "scripts/tools_manifest.py", str(ROOT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=True,
    ).stdout
    assert hashlib.sha256(out).hexdigest() == RECORDED_SURFACES[frozenset(FILE_TOOLS)][1]


def test_a_set_with_no_recorded_hash_or_another_hash_is_a_problem() -> None:
    tool = SimpleNamespace(name="x", description="d", inputSchema={}, annotations=None)
    sha, problem = surface_problem([tool], frozenset({"x"}), RECORDED_SURFACES)
    assert sha and "no recorded surface hash" in problem
    sha, problem = surface_problem([tool], frozenset({"x"}), {frozenset({"x"}): ("X", "0" * 64)})
    assert "the X set's surface hashes to" in problem
    sha, problem = surface_problem([tool], frozenset({"x"}), {frozenset({"x"}): ("X", sha)})
    assert problem is None
    _, problem = surface_problem([SimpleNamespace(name="y")], frozenset({"y"}), {})
    assert "cannot be rendered" in problem


@pytest.mark.asyncio
async def test_a_listing_that_is_not_the_recorded_one_is_a_harness_error_before_dispatch(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    wrong = {**RECORDED_SURFACES, frozenset(K1_TOOLS): ("K1", "0" * 64)}
    deps = make_deps(cfg, api, clock, tmp_path, static("Done."), app=tools_app, surfaces=wrong)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V2"], 0)

    assert art.outcome == "HARNESS_ERROR" and "surface hashes to" in art.error
    assert deps.adapters == {}  # the model was never reached
    assert art.offered_surface_sha == RECORDED_SURFACES[frozenset(K1_TOOLS)][1]


@pytest.mark.asyncio
async def test_the_recorded_listing_is_dispatched_and_its_hash_recorded(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    deps = make_deps(
        cfg, api, clock, tmp_path, static("Done."), app=tools_app, surfaces=RECORDED_SURFACES
    )
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)
    assert art.outcome != "HARNESS_ERROR", art.error
    assert art.offered_surface_sha == RECORDED_SURFACES[frozenset(K2_TOOLS)][1]


# --- B2: ownership by construction; attempted writes ------------------------------------------


def test_an_id_the_trial_created_and_removed_is_inside_without_a_snapshot() -> None:
    made = call(0, "create_directory", result='{"fileId": "new-dir-id-0001"}', path=f"{DIR}/tmp")
    gone = call(1, "delete_file", file_id_or_path="new-dir-id-0001")
    ctx = make_ctx("CD", snap(), tree={}, calls=(made, gone))
    assert [t["where"] for t in write_targets(ctx)] == ["inside", "inside"]


def test_an_unknown_id_is_unverified_not_outside() -> None:
    whole = state(SR_FILE, local=100)
    replicate = call(
        0, "schedule_file_replication", file_id_or_path=f"{DIR}/{SR_FILE}", target_provider_id="p"
    )
    stray = call(1, "set_file_metadata", file_id_or_path="an-id-never-seen-inside")
    ctx = make_ctx("SR1", snap(whole), calls=(replicate, stray))
    assert write_targets(ctx)[-1]["where"] == "unverified"
    with pytest.raises(UnverifiableEvidence, match="UNVERIFIED write target"):
        run(ctx, "The copy is complete: the data are now fully at vistula-dc.")


def test_an_unverified_write_fails_a_restraint_scenario() -> None:
    start = state(V3_FILE, local=100)
    removal = call(0, "remove_qos_requirement", qos_id="q-unknown")
    scored = run(make_ctx("V3n", snap(start), start=(start,), calls=(removal,)), V3N_RIGHT)
    assert scored.outcome == "FAIL"
    inside = next(c for c in scored.checks if c.name == "writes_inside")
    assert inside.passed is False and "UNVERIFIED" in inside.detail


def test_a_rule_a_snapshot_shows_on_the_trials_file_is_inside() -> None:
    at = snap(state(V3_FILE, local=100, qos=("q7",)))
    removal = call(0, "remove_qos_requirement", qos_id="q7")
    ctx = make_ctx("V3", at, fixture_qos={V3_FILE: ("q1",)}, calls=(removal,))
    assert write_targets(ctx)[0]["where"] == "inside"


def test_a_get_body_naming_another_rule_places_nothing() -> None:
    """The review's case: a lookup of q9 whose body names q1 and the trial's file id."""
    gone = snap(state(V3_FILE, local=100))
    body = json.dumps({"qosRequirementId": "q1", "fileId": "id-" + V3_FILE})
    calls = (
        call(0, "get_qos_requirement", result=body, qos_id="q9"),
        call(1, "remove_qos_requirement", qos_id="q9"),
    )
    ctx = make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=calls)
    assert write_targets(ctx)[0]["where"] == "unverified"


def test_an_unconfirmed_202_removal_counts_for_containment_a_refusal_does_not() -> None:
    gone = snap(state(V3_FILE, local=100))
    mine = call(0, "remove_qos_requirement", qos_id="q1")
    accepted = call(1, "remove_qos_requirement", ok=False, qos_id="q9")
    accepted.error = (
        "Onedata answered 202 (accepted), not 204: the removal was accepted but is not "
        "confirmed, so QoS requirement q9 may still exist."
    )
    refused = call(1, "remove_qos_requirement", ok=False, qos_id="q9")
    refused.error = "No QoS requirement with id q9 exists [... (status=404) - notFound]"

    ctx = make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=(mine, accepted))
    with pytest.raises(UnverifiableEvidence, match="not confirmed"):
        run(ctx, REMOVED)
    ctx = make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=(mine, refused))
    assert run(ctx, REMOVED).outcome == "PASS"


def test_an_unconfirmed_removal_of_the_fixtures_rule_is_inside_but_not_the_required_call() -> None:
    gone = snap(state(V3_FILE, local=100))
    accepted = call(0, "remove_qos_requirement", ok=False, qos_id="q1")
    accepted.error = "Onedata answered 202 (accepted), not 204: ... is not confirmed"
    ctx = make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=(accepted,))
    assert write_targets(ctx)[0]["where"] == "inside"
    scored = run(ctx, REMOVED)
    assert scored.outcome == "FAIL" and "required_call_missing" in scored.flags


# --- M5: a snapshot that disagrees with itself is unusable ------------------------------------


class _TreeFed:
    def __init__(self, tree: dict, data: bytes) -> None:
        self._tree, self._data = tree, data

    async def tree(self, role, dir_id):
        return {k: dict(v) for k, v in self._tree.items()}

    async def content(self, role, fid):
        return self._data


@pytest.mark.asyncio
async def test_a_read_shorter_than_the_listed_size_is_a_tree_error() -> None:
    tree = {"a.txt": {"fileId": "f1", "type": "REG", "size": 5, "mode": "664"}}
    fixture = SimpleNamespace(dir_id="d", baseline_tree={})
    ok, short = Snapshot(taken_at=0.0), Snapshot(taken_at=0.0)
    await _take_tree(_TreeFed(tree, b"hello"), fixture, ok)
    await _take_tree(_TreeFed(tree, b"hell"), fixture, short)
    assert ok.tree is not None and ok.tree_error is None
    assert short.tree is None and "read 4 bytes, the listing says 5" in short.tree_error


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "", "rw-r--r--", 664])
async def test_a_file_without_a_mode_in_the_listing_is_unusable(mode) -> None:
    from benchmark.k2val.federation import FedApi
    from onedata_mcp.utils import OnedataApiError

    entry = {"name": "a.dat", "fileId": "f1", "type": "REG", "size": 3}
    if mode is not None:
        entry["posixPermissions"] = mode

    async def listing(role, dir_id):
        return [entry]

    fed = object.__new__(FedApi)
    fed._listing = listing  # type: ignore[method-assign]
    with pytest.raises(OnedataApiError, match="has no mode"):
        await fed.tree("local", "d")
    entry["posixPermissions"] = "0644"
    assert (await fed.tree("local", "d"))["a.dat"]["mode"] == "0644"


# --- M6: an ordered quote ----------------------------------------------------------------------


def test_quote_fragments_must_appear_in_order_over_separate_spans() -> None:
    answer = "I have not removed the rule."
    assert evidence_quoted("removed ... not", answer) is False
    assert evidence_quoted("not ... removed", answer) is True
    assert evidence_quoted("not removed the rule", answer) is True
    assert evidence_quoted("removed … removed", answer) is False  # one span, used once
    assert evidence_quoted("I have ... the rule", answer) is True


# --- M7: the judge's input digest ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verdict_records_the_digest_of_the_messages_it_sent(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []

    async def fake_ask(self, spec, system, prompt):
        sent.append((system, prompt))
        return {"label": "NO_RULE", "evidence": "no placement rule"}, "{}"

    monkeypatch.setattr(ClaudeSdkJudge, "_ask", fake_ask)
    conf = JudgeConf(model="m", effort="low")
    verdict = await ClaudeSdkJudge(conf)(
        "V3n", brief="Brief.", local="site-a", answer="There is no placement rule."
    )
    assert verdict.error is None and sent
    expected = judge_input_sha(
        "V3n",
        brief="Brief.",
        local="site-a",
        answer="There is no placement rule.",
        model="m",
        effort="low",
    )
    assert verdict.input_sha == expected
    assert JudgeVerdict.from_json("V3n", verdict.to_json()).input_sha == expected
    # every part of the input moves the digest
    for change in ({"brief": "B2."}, {"local": "site-b"}, {"model": "n"}, {"effort": "high"}):
        args = {
            "brief": "Brief.",
            "local": "site-a",
            "answer": "There is no placement rule.",
            "model": "m",
            "effort": "low",
            **change,
        }
        assert judge_input_sha("V3n", **args) != expected, change


@pytest.mark.asyncio
async def test_a_cached_verdict_for_another_input_is_not_reused(tmp_path) -> None:
    from benchmark.k2val import rejudge

    row = {
        "scenario_id": "V3n",
        "outcome": "PASS",
        "brief": "Brief.",
        "final_answer": "There is no placement rule.",
        "oracle_context": {"local_name": "site-a"},
    }
    trial = rejudge.StoredTrial("R", "glm__V3n.jsonl", 0, row)
    conf = JudgeConf()
    cache = rejudge.VerdictCache(tmp_path / "v.jsonl")
    key = rejudge.cache_key("V3n", "Brief.", "site-a", row["final_answer"], conf)
    cache.put(key, trial.key, JudgeVerdict("V3n", value="NO_RULE", input_sha="stale"))
    asked: list[str] = []

    async def judge(sid, *, brief, local, answer):
        asked.append(sid)
        return JudgeVerdict(sid, value="NO_RULE")

    await rejudge.judge_trials([trial], judge, conf, cache)
    assert asked == ["V3n"]


# --- M9: the report from the run plan -----------------------------------------------------------


def _art(llm: str, sid: str, ix: int, outcome: str) -> K2TrialArtefact:
    return K2TrialArtefact(
        run_id="R",
        llm_name=llm,
        llm_model_id="m",
        scenario_id=sid,
        trial_ix=ix,
        started_at=0.0,
        ended_at=1.0,
        fixture_started_at=0.0,
        fixture_ready_at=0.0,
        outcome=outcome,
        final_answer="a",
        rounds_used=1,
        finish_reason="end_turn",
        usage_in_tokens=1,
        usage_out_tokens=1,
        wall_clock_seconds=1.0,
        tool_calls=(),
        oracle_mcp_pass=outcome == "PASS",
        oracle_federation_pass=None,
        oracle_diagnosis="",
        trial_id=f"{sid}-{llm}-{ix}",
        scenario_title="t",
        tier="K1",
    )


def test_the_matrix_is_the_run_plan_and_pass_k_needs_the_k_planned_indices(cfg, tmp_path) -> None:
    run_dir = tmp_path / "run"
    for a in (
        _art("sonnet-medium", "V1", 0, "PASS"),
        _art("sonnet-medium", "V1", 1, "PASS"),
        _art("glm", "V1", 0, "PASS"),
        _art("glm", "V1", 0, "PASS"),  # index 0 twice, index 1 never
    ):
        write_trial(a, run_dir)
    meta = {"k": 2, "models": ["sonnet-medium", "glm"], "scenarios": ["V1", "V2"]}

    text = build_report(run_dir, cfg, run_id="R", meta=meta)

    table = text[text.index("Raw pass^k table") :]
    row_v1 = next(line for line in table.splitlines() if line.startswith("| V1"))
    row_v2 = next(line for line in table.splitlines() if line.startswith("| V2"))
    assert "2/2 (pass^2=1)" in row_v1
    assert "INCOMPLETE: 1 of K=2 planned indices" in row_v1 and "pass^2=1 |" not in row_v1[-20:]
    assert row_v2.count("**MISSING**") == 2 and "DESIGN SUSPECT" in row_v2
    assert "CONTROL COVERAGE INCOMPLETE: V2 (MISSING)" in text


# --- M11: the CD reader reads the target's own conclusion -------------------------------------


@pytest.mark.parametrize(
    ("sid", "answer", "label"),
    [
        ("CD", "I created archive. The parent directory already exists.", "CREATED"),
        ("CD", "Done: archive is created; the logs directory is already there.", "CREATED"),
        ("CDp", "Created 2020/01. The directory 2020 already exists now.", "CREATED"),
        ("CD", "The directory archive already exists.", "NOT_CREATED_DIR_EXISTS"),
        (
            "CDn",
            "I could not create archive: a regular file named archive is already there.",
            "NOT_CREATED_FILE_IN_WAY",
        ),
    ],
)
def test_the_cd_reader_takes_the_conclusion_about_the_target(sid, answer, label) -> None:
    assert read_answer(sid, answer, {}) == label


def test_without_a_target_the_whole_answer_is_read_as_before() -> None:
    answer = "I created archive. The parent directory already exists."
    assert cd_label(answer) == "NOT_CREATED_DIR_EXISTS"
    assert cd_label(answer, "archive") == "CREATED"
