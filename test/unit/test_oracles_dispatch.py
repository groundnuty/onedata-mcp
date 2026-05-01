"""Drift / dispatch + smoke tests for the oracle module.

Heavier per-oracle behaviour tests live in test_oracles_<band>.py. This
file focuses on:
- every scenario has an oracle and vice versa
- the OracleResult two-axis contract (mcp_pass, federation_pass, diagnosis)
- a handful of representative format-tier oracles (no federation contact)
- the MCP-success / Onedata-failure distinction the design hinges on
"""

from __future__ import annotations

import pytest

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext, ToolCall
from benchmark.oracles import all_scenario_ids, get_oracle
from benchmark.scenarios import SCENARIOS


def test_every_scenario_has_an_oracle() -> None:
    scenario_ids = {s.id for s in SCENARIOS}
    oracle_ids = all_scenario_ids()
    missing = scenario_ids - oracle_ids
    assert not missing, f"scenarios without oracle: {sorted(missing)}"
    extra = oracle_ids - scenario_ids
    assert not extra, f"oracles without a scenario: {sorted(extra)}"


def test_get_oracle_returns_callable_for_every_scenario() -> None:
    for s in SCENARIOS:
        assert callable(get_oracle(s.id)), f"{s.id} oracle is not callable"


def test_get_oracle_raises_on_unknown_id() -> None:
    with pytest.raises(KeyError):
        get_oracle("NOPE")


@pytest.mark.asyncio
async def test_d6_format_oracle_passes_when_space_in_answer() -> None:
    oracle = get_oracle("D6")
    ctx = RunContext(scenario_id="D6")
    trace = AgentTrace(final_answer="Spaces: ppam_2026_mcp_tests, CloudSKTest")
    result = await oracle(ctx, trace)
    assert isinstance(result, OracleResult)
    assert result.mcp_pass is True
    # Format oracles always have federation_pass = None
    assert result.federation_pass is None


@pytest.mark.asyncio
async def test_d6_format_oracle_fails_when_space_missing() -> None:
    oracle = get_oracle("D6")
    ctx = RunContext(scenario_id="D6")
    trace = AgentTrace(final_answer="No spaces accessible.")
    result = await oracle(ctx, trace)
    assert result.mcp_pass is False
    assert result.federation_pass is None
    assert "ppam_2026_mcp_tests" in result.diagnosis


@pytest.mark.asyncio
async def test_p4_oracle_uses_captured_transfer_id() -> None:
    oracle = get_oracle("P4")
    ctx = RunContext(
        scenario_id="P4",
        captured_transfer_id="2727a9fe5f5df6b43a8033386d2990e8ch5df6",
    )
    trace = AgentTrace(
        final_answer="Most-recent migration: 2727a9fe5f5df6b43a8033386d2990e8ch5df6.",
        tool_calls=(
            ToolCall("list_space_transfers", {}, succeeded=True),
            ToolCall("get_transfer", {"transfer_id": "..."}, succeeded=True),
        ),
    )
    # Patch federation check to True so we isolate mcp_pass logic
    import benchmark.oracles.placement as p

    async def _fake_get_transfer(_tid: str) -> dict:
        return {"fileId": "x"}

    orig = p.transfers_api.get_transfer
    p.transfers_api.get_transfer = _fake_get_transfer  # type: ignore[assignment]
    try:
        result = await oracle(ctx, trace)
    finally:
        p.transfers_api.get_transfer = orig  # type: ignore[assignment]
    assert result.mcp_pass is True


@pytest.mark.asyncio
async def test_p4_oracle_fails_when_no_captured_id() -> None:
    """If fixture pre-staging didn't run, the oracle MUST fail — silent
    pass would mask infrastructure failures."""
    oracle = get_oracle("P4")
    ctx = RunContext(scenario_id="P4", captured_transfer_id=None)
    trace = AgentTrace(final_answer="abc123")
    result = await oracle(ctx, trace)
    assert result.mcp_pass is False
    assert result.federation_pass is False
    assert "captured_transfer_id missing" in result.diagnosis


@pytest.mark.asyncio
async def test_a1_format_oracle_counts_raw_files() -> None:
    """A1: 'tagged=N' must equal the number of fixture files under /a1/raw/."""
    oracle = get_oracle("A1")
    ctx = RunContext(
        scenario_id="A1",
        fixture_paths={
            "/ppam_2026_mcp_tests/a1/raw/sample00.txt": "f0",
            "/ppam_2026_mcp_tests/a1/raw/sample01.txt": "f1",
            "/ppam_2026_mcp_tests/a1/raw/sample02.txt": "f2",
            "/ppam_2026_mcp_tests/a1/raw/sample03.txt": "f3",
            "/ppam_2026_mcp_tests/a1/raw/sample04.txt": "f4",
            "/ppam_2026_mcp_tests/a1/outside.txt": "fx",  # distractor outside /a1/raw/
        },
    )
    trace = AgentTrace(final_answer="tagged=5")
    result = await oracle(ctx, trace)
    assert result.mcp_pass is True
    assert result.federation_pass is None


@pytest.mark.asyncio
async def test_a1_format_oracle_fails_on_wrong_count() -> None:
    oracle = get_oracle("A1")
    ctx = RunContext(
        scenario_id="A1",
        fixture_paths={f"/ppam_2026_mcp_tests/a1/raw/sample{i:02d}.txt": f"f{i}" for i in range(5)},
    )
    trace = AgentTrace(final_answer="tagged=3")
    result = await oracle(ctx, trace)
    assert result.mcp_pass is False
    assert "expected 5, got 3" in result.diagnosis
