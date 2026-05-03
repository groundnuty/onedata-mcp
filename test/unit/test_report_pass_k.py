"""Tests for the K-aware paper report (pass^k aggregation).

Pins the new stability columns + stochastic-cells call-out so a future
report refactor doesn't silently drop them. Synthesises trial records
in-memory rather than driving end-to-end through the trial runner.

Issue #22 in the repo backlog. See `make sweep-k8` for the K=8
headline that consumes this output.
"""

from __future__ import annotations

from benchmark.report import TrialRecord, render_paper_report


def _trial(llm: str, scen: str, outcome: str = "PASS") -> TrialRecord:
    """Minimal TrialRecord — only fields the report uses."""
    return TrialRecord(
        llm_name=llm,
        scenario_id=scen,
        outcome=outcome,
        rounds_used=2,
        finish_reason="stop",
        usage_in_tokens=100,
        usage_out_tokens=50,
        wall_clock_seconds=5.0,
        final_answer="",
        oracle_diagnosis="",
        error=None,
        tool_calls=(),
    )


# ---------------------------------------------------------------------------
# K=1 backwards compat: existing per-cell P/K shape preserved.
# ---------------------------------------------------------------------------


def test_k1_per_cell_shows_pk_format() -> None:
    """LLMs are alphabetically sorted: qwen, sonnet — so qwen is col 1.
    Both pass D1; sonnet alone fails D2."""
    records = [
        _trial("sonnet", "D1", "PASS"),
        _trial("sonnet", "D2", "FAIL"),
        _trial("qwen", "D1", "PASS"),
        _trial("qwen", "D2", "PASS"),
    ]
    out = render_paper_report(records, "test-run-id")
    # Order: qwen | sonnet (alpha)
    assert "| D1 | 1/1 | 1/1 |" in out
    assert "| D2 | 1/1 | 0/1 |" in out  # qwen=1/1, sonnet=0/1


def test_k1_max_K_is_1() -> None:
    records = [_trial("sonnet", "D1", "PASS")]
    out = render_paper_report(records, "test")
    assert "K (max trials/cell):** 1" in out


def test_k1_no_stochastic_section_when_k_eq_1() -> None:
    """K=1 cells can't be stochastic (every cell is 0/1 or 1/1).
    The stochastic section should not appear."""
    records = [_trial("sonnet", "D1", "PASS"), _trial("sonnet", "D2", "FAIL")]
    out = render_paper_report(records, "test")
    assert "## Stochastic cells" not in out


# ---------------------------------------------------------------------------
# K=8 stability columns
# ---------------------------------------------------------------------------


def _k8_records_for(llm: str, *, scenarios: dict[str, int]) -> list[TrialRecord]:
    """Synthesise K=8 records for one LLM. `scenarios` maps scenario_id
    to the number of PASSes (out of 8)."""
    records = []
    for sid, p in scenarios.items():
        for i in range(8):
            outcome = "PASS" if i < p else "FAIL"
            records.append(_trial(llm, sid, outcome))
    return records


def test_k8_stable_pass_counted() -> None:
    """An LLM with all-8/8 cells gets stable_pass = N/N."""
    # Sonnet hypothetical: 18 scenarios, all 8/8.
    scenarios = {sid: 8 for sid in [f"S{i:02d}" for i in range(18)]}
    records = _k8_records_for("sonnet", scenarios=scenarios)
    out = render_paper_report(records, "test")
    # Expect "stable PASS = 18/18" cell.
    assert "stable PASS" in out
    # The Sonnet row should have 18/18 in the stable_pass column.
    sonnet_lines = [line for line in out.splitlines() if "`sonnet`" in line]
    assert any("| 18/18 |" in line for line in sonnet_lines)


def test_k8_mixed_outcomes_classified() -> None:
    """An LLM with mixed cells: some 8/8, some 0/8, some stochastic.
    All three columns populate."""
    scenarios = {
        "S00": 8,  # stable PASS
        "S01": 8,  # stable PASS
        "S02": 0,  # stable FAIL
        "S03": 6,  # stochastic
        "S04": 1,  # stochastic
    }
    records = _k8_records_for("glm", scenarios=scenarios)
    out = render_paper_report(records, "test")
    glm_lines = [line for line in out.splitlines() if "`glm`" in line]
    assert glm_lines, "GLM row should be present in the totals table"
    line = glm_lines[0]
    # 2 stable-pass, 1 stable-fail, 2 stochastic.
    assert "| 2/5 |" in line  # stable PASS
    assert "| 1/5 |" in line  # stable FAIL
    assert "| 2/5 |" in line  # stochastic


def test_k8_stochastic_section_lists_mixed_cells() -> None:
    """The stochastic-cells section names exactly which cells were
    mixed, with their P/K rate."""
    scenarios = {
        "D1": 8,  # stable PASS — should NOT appear
        "A1": 6,  # stochastic — SHOULD appear as 6/8
        "P3": 2,  # stochastic — SHOULD appear as 2/8
        "P6": 0,  # stable FAIL — should NOT appear
    }
    records = _k8_records_for("granite", scenarios=scenarios)
    out = render_paper_report(records, "test")
    assert "## Stochastic cells" in out
    # Both stochastic cells must appear with their P/K notation.
    assert "`A1`=6/8" in out
    assert "`P3`=2/8" in out
    # Stable cells must NOT appear in stochastic section.
    granite_section_idx = out.find("## Stochastic cells")
    granite_section = out[granite_section_idx:]
    assert "`D1`" not in granite_section.split("`granite`")[0]
    assert "`P6`" not in granite_section.split("`granite`")[0]


def test_k8_stochastic_section_says_none_when_all_stable() -> None:
    """When K=8 but no cells are stochastic (everything fully PASS or
    fully FAIL), the section shows 'none'."""
    scenarios = {"D1": 8, "D2": 0, "D3": 8}
    records = _k8_records_for("sonnet", scenarios=scenarios)
    out = render_paper_report(records, "test")
    assert "## Stochastic cells" in out
    assert "(none — all cells are stable PASS or stable FAIL" in out


# ---------------------------------------------------------------------------
# Mixed-K records (V4-pro probe at K=1, others at K=8)
# ---------------------------------------------------------------------------


def test_mixed_K_uses_max_K_for_header() -> None:
    """Real K=8 sweeps land V4-pro at K=1 (probe) + others at K=8.
    The header should say K (max trials/cell) = 8."""
    records = _k8_records_for("sonnet", scenarios={"D1": 8, "D2": 8})
    records.append(_trial("v4pro", "D1", "PASS"))  # K=1 probe
    out = render_paper_report(records, "test")
    assert "K (max trials/cell):** 8" in out
    # V4-pro D1 row reports as 1/1 (the probe), Sonnet as 8/8.
    assert "1/1" in out
    assert "8/8" in out
