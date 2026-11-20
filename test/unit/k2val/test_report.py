"""The report: control first, Haiku configs side by side, DESIGN SUSPECT, K2 violation,
nothing counted (a blank classification line per failure), no secret."""

from __future__ import annotations

from pathlib import Path

from benchmark.k2val.artefact import K2TrialArtefact, write_trial
from benchmark.k2val.changes import record_change
from benchmark.k2val.report import build_report, ordered_models
from benchmark.k2val.scenarios import parse_scenarios


def art(llm: str, scenario: str, ix: int, outcome: str, **kw) -> K2TrialArtefact:
    base = {
        "run_id": "R",
        "llm_name": llm,
        "llm_model_id": "m",
        "scenario_id": scenario,
        "trial_ix": ix,
        "started_at": 0.0,
        "ended_at": 1.0,
        "fixture_started_at": 0.0,
        "fixture_ready_at": 0.0,
        "outcome": outcome,
        "final_answer": kw.pop("final_answer", "answer"),
        "rounds_used": 3,
        "finish_reason": "end_turn",
        "usage_in_tokens": 10,
        "usage_out_tokens": 5,
        "wall_clock_seconds": 2.0,
        "tool_calls": (),
        "oracle_mcp_pass": outcome == "PASS",
        "oracle_federation_pass": None,
        "oracle_diagnosis": kw.pop("diagnosis", ""),
        "trial_id": f"{scenario}-{llm}-{ix}",
        "scenario_title": "t",
        "tier": "K2",
        "cost_usd": 0.01,
    }
    base.update(kw)
    return K2TrialArtefact(**base)


def call(tool: str, ok: bool = True, **kw) -> dict:
    return {
        "index": 0,
        "tool": tool,
        "arguments": {"x": 1},
        "started_at": 0.0,
        "ended_at": 1.0,
        "ok": ok,
        "result_text": kw.get("result_text", "{}"),
        "error": kw.get("error"),
        "source": "mcp",
    }


def build(cfg, tmp_path: Path, arts: list[K2TrialArtefact]) -> str:
    run_dir = tmp_path / "run"
    for a in arts:
        write_trial(a, run_dir)
    return build_report(run_dir, cfg, run_id="R", meta={"k": 2})


def test_columns_put_the_control_first_and_the_haiku_configs_together(cfg) -> None:
    order = ordered_models(
        cfg, {"glm", "haiku-low", "qwen36", "haiku-high", "sonnet-medium", "haiku-medium"}
    )
    assert order[0] == "sonnet-medium"
    h = [order.index(n) for n in ("haiku-low", "haiku-medium", "haiku-high")]
    assert h == list(range(h[0], h[0] + 3))
    assert set(order) == {
        "glm",
        "qwen36",
        "haiku-low",
        "haiku-medium",
        "haiku-high",
        "sonnet-medium",
    }


def test_a_control_that_does_not_pass_marks_the_scenario_design_suspect(cfg, tmp_path) -> None:
    arts = [
        art("sonnet-medium", "V1", 0, "PASS"),
        art("sonnet-medium", "V1", 1, "PASS"),
        art("sonnet-medium", "V2", 0, "PASS"),
        art("sonnet-medium", "V2", 1, "FAIL", diagnosis="missing ['log_20200102.dat']"),
        art("sonnet-medium", "V3", 0, "NO_RESULT", no_result_reason="max tool rounds reached"),
        art("glm", "V1", 0, "FAIL"),
    ]

    text = build(cfg, tmp_path, arts)

    assert "DESIGN SUSPECT: V2, V3" in text
    assert text.index("Control: sonnet-medium") < text.index("Raw pass^k table")
    table = text[text.index("Raw pass^k table") :]
    row_v2 = next(line for line in table.splitlines() if line.startswith("| V2"))
    assert "DESIGN SUSPECT" in row_v2
    row_v1 = next(line for line in table.splitlines() if line.startswith("| V1"))
    assert "DESIGN SUSPECT" not in row_v1


def test_a_clean_control_is_said_so(cfg, tmp_path) -> None:
    text = build(cfg, tmp_path, [art("sonnet-medium", "V1", 0, "PASS")])
    assert "The control passed every scenario it ran." in text
    assert "DESIGN SUSPECT: V" not in text


def test_the_pass_k_table_is_raw_and_marks_other_outcomes(cfg, tmp_path) -> None:
    arts = [
        art("glm", "V4", 0, "PASS"),
        art("glm", "V4", 1, "PASS"),
        art("qwen36", "V4", 0, "PASS"),
        art("qwen36", "V4", 1, "FAIL"),
        art("haiku-low", "V4", 0, "PASS"),
        art("haiku-low", "V4", 1, "NO_RESULT"),
    ]

    text = build(cfg, tmp_path, arts)

    assert "2/2 (pass^2=1)" in text
    assert "1/2 (pass^2=0)" in text
    assert "1/1 +1 NO_RESULT" in text
    assert "Raw and unclassified: nothing below is counted yet" in text


def test_the_haiku_effort_comparison_reads_side_by_side(cfg, tmp_path) -> None:
    arts = [
        art(f"haiku-{e}", "V4", 0, "PASS", rounds_used=n)
        for e, n in (("low", 2), ("medium", 4), ("high", 6))
    ]

    text = build(cfg, tmp_path, arts)

    section = text[text.index("Haiku effort comparison") :]
    assert "| scenario | haiku-low | haiku-medium | haiku-high |" in section
    assert "| haiku-high | 1 | 6.0 |" in section


def test_every_non_pass_has_diagnosis_trace_lines_and_a_blank_classification(cfg, tmp_path) -> None:
    arts = [
        art(
            "glm",
            "V5",
            0,
            "FAIL",
            diagnosis="a plain 'not ready' without the reason 'still being written'",
            calls=tuple(
                [
                    call("check_readiness", result_text='{"ready": false}'),
                    call("ensure_readiness", ok=False, error="Input should be a valid integer"),
                ]
            ),
            final_answer="Not ready.",
        )
    ]

    text = build(cfg, tmp_path, arts)

    block = text[text.index("### V5 / glm / trial 0: FAIL") :]
    assert "without the reason" in block
    assert '`check_readiness({"x": 1})`' in block
    assert "final answer: Not ready." in block
    assert "Classification (harness bug / deployment artefact / model failure): \n" in block
    # sequences, wrong arguments (a validation-style error), and the errors tools returned
    assert "check_readiness > ensure_readiness!" in text
    wrong = text.split("## Wrong arguments")[1].split("## Errors returned by tools")[0]
    assert '`ensure_readiness({"x": 1})`' in wrong and "Input should be a valid integer" in wrong
    errors = text.split("## Errors returned by tools")[1]
    assert "| ensure_readiness | Input should be a valid integer | 1 |" in errors


def test_a_k2_violation_is_in_large_letters_and_premature_claims_are_listed(cfg, tmp_path) -> None:
    arts = [
        art(
            "haiku-low",
            "V7",
            0,
            "FAIL",
            flags=("premature_claim", "k2_violation"),
            k2_violations=({"tool": "ensure_readiness", "call_index": 0, "level": "certain"},),
        )
    ]

    text = build(cfg, tmp_path, arts)

    assert "# K2 VIOLATION (a TOOL BUG, not a model failure)" in text
    assert "V7 / haiku-low / trial 0" in text.split("## Premature claims")[1]


def test_recorded_changes_appear_with_before_and_after(cfg, tmp_path) -> None:
    run_dir = tmp_path / "run"
    write_trial(art("glm", "V4", 0, "PASS"), run_dir)
    record_change(
        run_dir,
        target="tool:check_readiness",
        trigger_trial="V5-glm-k0-abc",
        reason="misread wording",
        before="old text",
        after="new text",
    )

    text = build_report(run_dir, cfg, run_id="R")

    assert "tool:check_readiness" in text and "V5-glm-k0-abc" in text
    assert "before: old text" in text and "after: new text" in text


def test_the_scenario_range_syntax() -> None:
    assert parse_scenarios("V1..V7") == [f"V{i}" for i in range(1, 8)]
    assert parse_scenarios("V5,V2") == ["V5", "V2"]


def test_needs_review_is_its_own_count_and_section_never_a_pass_or_a_fail(cfg, tmp_path) -> None:
    reading = {
        "deterministic": "NOT_READY_ELSEWHERE",
        "judge": "SAYS_READY",
        "judge_error": None,
        "judge_verdict": {
            "evidence": "they are ready",
            "model": "claude-sonnet-5-5",
            "effort": "medium",
            "rubric_sha": "abc",
            "evidence_quoted": True,
        },
        "outcome_if": {"deterministic": "PASS", "judge": "FAIL"},
    }
    text = build(
        cfg,
        tmp_path,
        [
            art("glm", "V4", 0, "PASS"),
            art("glm", "V4", 1, "NEEDS_REVIEW", answer_reading=reading, final_answer="Not ready."),
        ],
    )
    assert "| V4 check, remote | 1/1 **+1 NEEDS_REVIEW** |" in text
    section = text.split("## NEEDS_REVIEW")[1].split("\n## ")[0]
    assert "V4 / glm / trial 1: NEEDS_REVIEW" in section
    assert "`NOT_READY_ELSEWHERE`" in section and "`SAYS_READY`" in section
    assert "they are ready" in section and "'deterministic': 'PASS'" in section
    every_non_pass = text.split("## Every non-pass")[1].split("\n## ")[0]
    assert "NEEDS_REVIEW" not in every_non_pass  # listed once, in its own section
