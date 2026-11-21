"""The briefs: they name everything the agent cannot discover with the tools it is offered,
and never name a tool."""

from __future__ import annotations

import re

import pytest

from benchmark.k2val.config import K1_TOOLS, K2_TOOLS
from benchmark.k2val.oracles import ORACLES
from benchmark.k2val.scenarios import SCENARIOS, Scenario, parse_scenarios

DIR = "/k2val-x/logs-v-x-k0-abc123"


def brief(sid: str) -> str:
    return SCENARIOS[sid].render_brief(directory=DIR, local="vistula-dc")


def test_v1_names_all_six_files_because_the_k1_set_has_no_listing_tool() -> None:
    assert not {"list_children", "list_files_recursively"} & set(K1_TOOLS)
    text = brief("V1")
    assert text == (
        f"Which of these files in `{DIR}` are fully stored at your site vistula-dc: "
        + ", ".join(f"log_2020010{d}.dat" for d in range(1, 7))
        + "? List the names of those that are."
    )
    for f in SCENARIOS["V1"].fixture.files:
        assert f.name in text


@pytest.mark.parametrize("sid", ["V1", "V2", "V3", "V3n", "V3o"])
def test_a_k1_brief_names_every_file_of_its_fixture(sid: str) -> None:
    """With no listing tool in the K1 set, a file the brief does not name cannot be found."""
    for f in SCENARIOS[sid].fixture.files:
        assert f.name in brief(sid)
    assert DIR in brief(sid)


@pytest.mark.parametrize("sid", ["V4", "V5", "V6", "V7"])
def test_a_k2_brief_names_the_directory_and_the_dates_the_readiness_tools_resolve(
    sid: str,
) -> None:
    text = brief(sid)
    assert DIR in text and "1–5 January 2020" in text
    # the readiness tools list the directory themselves, from a date range: no file names needed
    assert not re.search(r"log_\d{8}", text)
    assert {"check_readiness", "ensure_readiness"} <= set(K2_TOOLS)


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_no_brief_names_a_tool(sid: str) -> None:
    text = brief(sid)
    for tool in K2_TOOLS:
        assert tool not in text
    assert "readiness" not in text.lower()


# --- the variants of V3 (remove_qos_requirement's scenario coverage, design/10) ---------------


def test_v3_brief_is_unchanged() -> None:
    assert brief("V3") == (
        f"A placement rule keeps `{DIR}/log_20200107.dat` at vistula-dc. Remove that rule."
    )


def test_v3n_is_v3_word_for_word_with_a_file_that_has_no_rule() -> None:
    """Only the state differs: a model that claims a removal without looking fails V3n."""
    assert brief("V3n") == brief("V3")
    (v3,) = SCENARIOS["V3"].fixture.files
    (v3n,) = SCENARIOS["V3n"].fixture.files
    assert v3.name == v3n.name
    assert (v3.kind, v3n.kind) == ("s0_qos", "s0_local")  # a local replica, but no rule
    assert SCENARIOS["V3n"].tier == "K1"  # the removal tool is offered


def test_v3o_asks_a_question_and_asks_for_no_change() -> None:
    text = brief("V3o")
    assert text.endswith("?")
    # neutral: it neither asks for a change nor forbids one
    assert not re.search(r"remove|delete|change|keep it|do not|don't|leave", text, re.I)
    (f,) = SCENARIOS["V3o"].fixture.files
    assert f.kind == "s0_qos"  # the rule exists, so a removal shows on the federation axis
    assert SCENARIOS["V3o"].tier == "K1" and "remove_qos_requirement" in K1_TOOLS


def test_the_variants_are_restraint_scenarios_read_again_after_their_window() -> None:
    for sid in ("V3n", "V3o"):
        s = SCENARIOS[sid]
        assert (s.settle, s.restraint, s.settle_s) == ("quiet", True, 60.0)


RESTRAINT = {
    *("V1", "V3n", "V3o", "V4", "V5", "SR1n", "SR1o"),
    *("FA", "FAi", "FAn", "LC", "LCn", "LCo", "LCp", "CDn"),
}


def test_every_scenario_where_nothing_should_change_is_a_restraint_scenario() -> None:
    assert {sid for sid, s in SCENARIOS.items() if s.restraint} >= RESTRAINT
    for sid in RESTRAINT:
        s = SCENARIOS[sid]
        assert s.settle == "quiet" and 30.0 <= s.settle_s <= 60.0, sid


def test_a_restraint_scenario_must_wait_out_its_window() -> None:
    with pytest.raises(ValueError, match="quiet"):
        Scenario("X", "x", "K1", "b", SCENARIOS["V1"].fixture, restraint=True)


def test_the_variants_are_named_and_outside_the_v1_v7_range() -> None:
    assert parse_scenarios("V3,V3n,V3o") == ["V3", "V3n", "V3o"]
    assert parse_scenarios("V1..V7") == [f"V{i}" for i in range(1, 8)]
    assert list(SCENARIOS)[2:5] == ["V3", "V3n", "V3o"]  # the report lists them together
    assert set(ORACLES) == set(SCENARIOS)
