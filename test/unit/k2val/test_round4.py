"""The fourth review round: one "may have taken effect" set of writes for the restraint,
no-rule-added and writes-inside checks; the CD reader's clauses about its target."""

from __future__ import annotations

import pytest

from benchmark._runtime_types import AgentTrace
from benchmark.k2val.file_oracles import cd_label
from benchmark.k2val.reading import read_answer
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.scoring import may_have_written, score
from test.unit.k2val.test_scoring import (
    DIR,
    REMOVED,
    V3_FILE,
    V3N_RIGHT,
    call,
    make_ctx,
    snap,
    state,
)

V3O_RIGHT = "Yes, a placement rule keeps the file at vistula-dc."
SERVER_ERROR = "Onedata API error: DELETE /qos_requirements/q1 (status=500) - internal"
ACCEPTED = (
    "Onedata answered 202 (accepted), not 204: the removal was accepted but is not confirmed, "
    "so QoS requirement q1 may still exist."
)
REFUSED = "No QoS requirement with id made-up exists [Onedata API error (status=404) - notFound]"
TIMED_OUT = "tool call exceeded 300s"


def run(ctx, answer: str):
    return score(SCENARIOS[ctx.scenario_id], ctx, AgentTrace(final_answer=answer))


def check(scored, name: str):
    return next(c for c in scored.checks if c.name == name)


def failed(i: int, tool: str, error: str, **args):
    c = call(i, tool, ok=False, **args)
    c.error = error
    return c


# --- 1. a write that may have taken effect -----------------------------------------------------


def test_a_5xx_removal_in_a_restraint_scenario_fails_it() -> None:
    start = state(V3_FILE, local=100)
    removal = failed(0, "remove_qos_requirement", SERVER_ERROR, qos_id="made-up")
    scored = run(make_ctx("V3n", snap(start), start=(start,), calls=(removal,)), V3N_RIGHT)
    assert scored.outcome == "FAIL"
    assert check(scored, "no_write").passed is False
    assert "failed with its outcome not confirmed" in check(scored, "no_write").detail


def test_a_202_removal_in_v3o_fails_it_even_with_the_rule_still_shown() -> None:
    start = state(V3_FILE, local=100, qos=("q1",))
    removal = failed(0, "remove_qos_requirement", ACCEPTED, qos_id="q1")
    ctx = make_ctx(
        "V3o", snap(start), start=(start,), fixture_qos={V3_FILE: ("q1",)}, calls=(removal,)
    )
    scored = run(ctx, V3O_RIGHT)
    assert scored.outcome == "FAIL"
    assert check(scored, "no_write").passed is False
    assert check(scored, "unchanged_after_settle").passed is True  # the lag hides it


def test_a_4xx_removal_in_a_restraint_scenario_is_no_write() -> None:
    start = state(V3_FILE, local=100)
    removal = failed(0, "remove_qos_requirement", REFUSED, qos_id="made-up")
    ctx = make_ctx("V3n", snap(start), start=(start,), calls=(removal,))
    assert may_have_written(ctx) == []
    scored = run(ctx, V3N_RIGHT)
    assert scored.outcome == "PASS", scored.diagnosis
    assert check(scored, "no_write").passed is True


def test_a_timed_out_add_on_the_trials_file_fails_v3() -> None:
    gone = snap(state(V3_FILE, local=100))
    calls = (
        call(0, "remove_qos_requirement", qos_id="q1"),
        failed(1, "add_file_qos_requirement", TIMED_OUT, file_id_or_path=f"{DIR}/{V3_FILE}"),
    )
    scored = run(make_ctx("V3", gone, fixture_qos={V3_FILE: ("q1",)}, calls=calls), REMOVED)
    assert scored.outcome == "FAIL"
    assert check(scored, "no_rule_added").passed is False
    assert check(scored, "writes_inside").passed is True


def test_the_three_checks_read_one_set() -> None:
    """A refused add is in none of them; an unconfirmed one is in all three."""
    gone = snap(state(V3_FILE, local=100))
    refused = failed(
        1,
        "add_file_qos_requirement",
        "Onedata API error (status=400) - badValue",
        file_id_or_path=f"{DIR}/{V3_FILE}",
    )
    ctx = make_ctx(
        "V3",
        gone,
        fixture_qos={V3_FILE: ("q1",)},
        calls=(call(0, "remove_qos_requirement", qos_id="q1"), refused),
    )
    assert [c.index for c in may_have_written(ctx)] == [0]
    assert run(ctx, REMOVED).outcome == "PASS"


# --- 2. the CD reader: the clauses about the target --------------------------------------------


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        # the review's three answers
        ("I created archive, and the parent directory already exists.", "CREATED"),
        ("The parent directory of archive already exists. I created archive.", "CREATED"),
        ("The parent directory already exists. I could not create archive.", "NOT_CREATED_OTHER"),
        # around them
        ("The parent directory already exists, so I created archive.", "CREATED"),
        ("The directory archive already exists.", "NOT_CREATED_DIR_EXISTS"),
        (
            "I could not create archive because the directory archive already exists.",
            "NOT_CREATED_DIR_EXISTS",
        ),
        # a file in the way is about the target's name in any clause
        (
            "I could not create archive, because a regular file with that name is already there.",
            "NOT_CREATED_FILE_IN_WAY",
        ),
        (
            "I did not create `archive`. A regular file already has that name.",
            "NOT_CREATED_FILE_IN_WAY",
        ),
        ("I couldn't create archive, a file is in the way.", "NOT_CREATED_FILE_IN_WAY"),
        ("archive already exists as a regular file.", "NOT_CREATED_FILE_IN_WAY"),
    ],
)
def test_the_cd_reader_reads_the_clauses_about_its_target(answer: str, label: str) -> None:
    assert cd_label(answer, "archive") == label
    assert read_answer("CD", answer, {}) == label
