"""The dual reading of every final answer (design/10, gate 4; scoring.score_dual, judge.py):
with FAKE judges only. The real judge is never called by a unit test.

Agree: that reading decides. Disagree: NEEDS_REVIEW, both readings and the evidence kept, with
the outcome each would give. A judge failure (timeout, rate limit, malformed output after one
retry): NEEDS_REVIEW with judge_error, never a fallback to the deterministic reader."""

from __future__ import annotations

import asyncio
import json

import pytest

from benchmark._runtime_types import AgentTrace, RunContext
from benchmark.k2val.judge import (
    ClaudeSdkJudge,
    JudgeConf,
    JudgeVerdict,
    MalformedVerdict,
    evidence_quoted,
    parse_verdict,
)
from benchmark.k2val.oracles import K2Context
from benchmark.k2val.reading import READERS, read_answer, right_reading
from benchmark.k2val.rubrics import ANSWER_SPECS, output_schema, rubric, rubric_sha
from benchmark.k2val.runner import run_trial
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.scoring import score_dual
from benchmark.k2val.snapshot import FileState, Snapshot
from test.unit.k2val.test_runner import make_deps, static

DIR = "/k2val-glm/logs-abcdefghijkl"
FILE = "log_20200107.dat"
RIGHT = "There is no placement rule on that file, so there is nothing to remove."


def v3n_ctx() -> K2Context:
    state = FileState(
        FILE,
        "id-f",
        size=100,
        local_bytes=100,
        owner_bytes=100,
        local_covered=True,
        owner_covered=True,
        size_field="virtualSize",
    )
    snap = Snapshot(taken_at=1.0, files={FILE: state})
    return K2Context(
        scenario_id="V3n",
        run=RunContext(scenario_id="V3n", fixture_paths={f"{DIR}/{FILE}": "id-f"}),
        local_name="vistula-dc",
        owner_name="nordic-marine-dc",
        file_names=[FILE],
        at_answer=snap,
        after_settle=snap,
        fixture_qos={FILE: ()},
        baseline_files={FILE: {"local_bytes": 100, "qos_ids": [], "size": 100}},
        directory=DIR,
        dir_id="d1",
        local_provider_id="pLocal",
    )


def verdict(value, error=None) -> JudgeVerdict:
    return JudgeVerdict("V3n", value=value, evidence="nothing to remove", error=error, model="fake")


def test_agreement_decides() -> None:
    scored = score_dual(
        SCENARIOS["V3n"], v3n_ctx(), AgentTrace(final_answer=RIGHT), verdict("SAYS_NO_RULE")
    )
    assert scored.outcome == "PASS"
    assert scored.answer_reading["agree"] is True
    assert scored.answer_reading["decided"] == "SAYS_NO_RULE"


def test_a_disagreement_is_needs_review_with_both_readings_and_both_outcomes() -> None:
    scored = score_dual(
        SCENARIOS["V3n"], v3n_ctx(), AgentTrace(final_answer=RIGHT), verdict("CLAIMS_REMOVAL")
    )
    assert scored.outcome == "NEEDS_REVIEW" and "readers_disagree" in scored.flags
    reading = scored.answer_reading
    assert (reading["deterministic"], reading["judge"]) == ("SAYS_NO_RULE", "CLAIMS_REMOVAL")
    assert reading["outcome_if"] == {"deterministic": "PASS", "judge": "FAIL"}
    assert reading["judge_verdict"]["evidence"] == "nothing to remove"
    assert reading["decided"] is None


@pytest.mark.parametrize(
    "error",
    [
        "timeout after 180s",
        "RuntimeError: rate limit",
        "malformed verdict after 2 attempt(s): not JSON",
    ],
)
def test_a_judge_error_is_needs_review_never_a_fallback(error: str) -> None:
    scored = score_dual(
        SCENARIOS["V3n"], v3n_ctx(), AgentTrace(final_answer=RIGHT), verdict(None, error)
    )
    assert scored.outcome == "NEEDS_REVIEW" and "judge_error" in scored.flags
    assert scored.answer_reading["judge_error"] == error
    assert scored.answer_reading["outcome_if"] == {"deterministic": "PASS"}


def test_no_judge_at_all_is_needs_review() -> None:
    scored = score_dual(SCENARIOS["V3n"], v3n_ctx(), AgentTrace(final_answer=RIGHT), None)
    assert scored.outcome == "NEEDS_REVIEW" and scored.answer_reading["judge_error"] == "no judge"


def test_a_names_disagreement_on_an_extra_name() -> None:
    from benchmark.k2val.rubrics import same_reading

    assert same_reading(frozenset({"a", "b/"}), frozenset({"`a`", "b"}))
    assert not same_reading(frozenset({"a"}), frozenset({"a", "c"}))
    assert not same_reading(frozenset(), None) and same_reading(None, None)


# --- in a trial --------------------------------------------------------------------------------


class Disagreeing:
    async def __call__(self, scenario_id, *, brief, local, answer):
        return JudgeVerdict(scenario_id, value="SAYS_READY", evidence="ready", model="fake")


class Raising:
    async def __call__(self, scenario_id, *, brief, local, answer):
        raise ConnectionError("the judge's CLI died")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("judge", "flag"), [(Disagreeing(), "readers_disagree"), (Raising(), "judge_error")]
)
async def test_a_trial_is_needs_review_when_the_readings_differ_or_the_judge_fails(
    cfg, api, fed, clock, tmp_path, judge, flag
) -> None:
    answer = "No. The files are only at nordic-marine-dc, another provider."
    deps = make_deps(cfg, api, clock, tmp_path, static(answer), judge=judge)
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)
    assert art.outcome == "NEEDS_REVIEW" and flag in art.flags
    assert art.answer_reading["deterministic"] == "NOT_READY_ELSEWHERE"
    assert art.answer_reading["outcome_if"]["deterministic"] == "PASS"


@pytest.mark.asyncio
async def test_the_judge_sees_the_brief_the_agent_saw_and_the_answer_only(
    cfg, api, fed, clock, tmp_path
) -> None:
    seen = {}

    class Recording:
        async def __call__(self, scenario_id, *, brief, local, answer):
            seen.update(scenario_id=scenario_id, brief=brief, local=local, answer=answer)
            return JudgeVerdict(scenario_id, value="NOT_READY_ELSEWHERE", evidence="x")

    answer = "No: the data are only at nordic-marine-dc."
    deps = make_deps(cfg, api, clock, tmp_path, static(answer), judge=Recording())
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)
    assert seen == {
        "scenario_id": "V4",
        "brief": art.brief,
        "local": "vistula-dc",
        "answer": answer,
    }
    assert art.outcome == "PASS"


# --- the verdict's shape -------------------------------------------------------------------------


def test_parse_a_label_verdict_and_its_failures() -> None:
    spec = ANSWER_SPECS["V3n"]
    assert parse_verdict(spec, '{"label": "SAYS_NO_RULE", "evidence": "no rule"}') == (
        "SAYS_NO_RULE",
        "no rule",
    )
    assert parse_verdict(spec, '```json\n{"label": "OTHER", "evidence": ""}\n```')[0] == "OTHER"
    assert parse_verdict(spec, {"label": "OTHER", "evidence": "x"})[0] == "OTHER"
    for bad in ("not json", "[]", '{"label": "MAYBE", "evidence": "x"}', '{"label": "OTHER"}'):
        with pytest.raises(MalformedVerdict):
            parse_verdict(spec, bad)


def test_parse_a_names_verdict() -> None:
    spec = ANSWER_SPECS["LC"]
    value, _ = parse_verdict(
        spec, '{"names": ["`calib_runs/`", "/sp/d/raw_frames"], "evidence": "x"}'
    )
    assert value == frozenset({"calib_runs", "raw_frames"})
    assert parse_verdict(spec, '{"names": null, "evidence": "x"}')[0] is None
    with pytest.raises(MalformedVerdict):
        parse_verdict(spec, '{"names": [1], "evidence": "x"}')


def test_evidence_must_be_a_quote_of_the_answer() -> None:
    answer = "The file has **no** placement rule, so nothing was removed."
    assert evidence_quoted("the file has no placement rule", answer)
    assert not evidence_quoted("a rule was removed", answer)


class _ScriptedJudge(ClaudeSdkJudge):
    """The real judge's retry and error handling, with `_ask` replaced: no model is called."""

    def __init__(self, replies) -> None:
        super().__init__(JudgeConf(timeout_s=0.5))
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def _ask(self, spec, system, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if reply == "hang":
            await asyncio.sleep(5)
        return reply, reply if isinstance(reply, str) else json.dumps(reply)


@pytest.mark.asyncio
async def test_a_malformed_verdict_is_retried_once_then_an_error() -> None:
    ok = await _ScriptedJudge(["oops", '{"label": "SAYS_NO_RULE", "evidence": "no rule"}'])(
        "V3n", brief="b", local="vistula-dc", answer="It has no rule."
    )
    assert (ok.value, ok.error, ok.attempts) == ("SAYS_NO_RULE", None, 2)
    judge = _ScriptedJudge(["oops", "still not json"])
    bad = await judge("V3n", brief="b", local="vistula-dc", answer="x")
    assert bad.value is None and bad.error.startswith("malformed verdict after 2 attempt(s)")
    assert "not a valid verdict" in judge.prompts[1]


@pytest.mark.asyncio
async def test_a_timeout_or_a_failure_is_a_judge_error() -> None:
    slow = await _ScriptedJudge(["hang"])("V3n", brief="b", local="l", answer="x")
    assert slow.error == "timeout after 0s" or slow.error.startswith("timeout")
    limited = await _ScriptedJudge([RuntimeError("rate_limit_error")])(
        "V3n", brief="b", local="l", answer="x"
    )
    assert "rate_limit_error" in limited.error and limited.value is None


# --- the rubric ----------------------------------------------------------------------------------


def test_every_scenario_has_a_reader_a_spec_and_a_right_reading() -> None:
    assert set(READERS) == set(SCENARIOS) == set(ANSWER_SPECS)
    for sid in SCENARIOS:
        right = right_reading(sid)
        spec = ANSWER_SPECS[sid]
        if spec.kind == "label":
            assert right in spec.labels, sid
        else:
            assert isinstance(right, frozenset), sid


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_the_rubric_holds_the_brief_and_labels_but_never_the_right_answer(sid: str) -> None:
    brief = SCENARIOS[sid].render_brief(directory=DIR, local="vistula-dc")
    text = rubric(ANSWER_SPECS[sid], brief=brief, local="vistula-dc")
    assert brief.strip() in text
    assert "Label only what the answer CLAIMS" in text
    for word in ("correct", "right answer", "should pass", "expected"):
        assert word not in text.lower()
    spec = ANSWER_SPECS[sid]
    for label in spec.labels:
        assert f"- {label}:" in text
    schema = output_schema(spec)
    assert schema["required"][1] == "evidence" and schema["additionalProperties"] is False
    assert len(rubric_sha(spec)) == 16


def test_the_deterministic_reading_offline_equals_the_one_in_a_trial() -> None:
    assert read_answer("V3n", RIGHT) == "SAYS_NO_RULE"
    assert read_answer(
        "LC", "calib_runs, raw_frames, spare_slots and archive", {"brief": f"`{DIR}`"}
    ) == frozenset({"calib_runs", "raw_frames", "spare_slots", "archive"})
