"""Tests for the two-axis OracleResult contract on a representative
static-tier oracle (A2). Demonstrates the four cells of the matrix:

   mcp_pass=T, federation_pass=T  →  trial passes, no divergence
   mcp_pass=T, federation_pass=F  →  agent OK, Onedata diverged (the case
                                     the user surfaced 2026-05-01)
   mcp_pass=F, federation_pass=T  →  agent didn't issue calls, but the
                                     federation matches anyway
   mcp_pass=F, federation_pass=F  →  agent failed AND federation diverged
"""

from __future__ import annotations

import pytest

from benchmark._runtime_types import AgentTrace, RunContext, ToolCall
from benchmark.oracles.access import verify_a2

A2_SPACE_PREFIX = "/ppam_2026_mcp_tests/a2/inbox/"


def _ctx(file_count: int = 3) -> RunContext:
    return RunContext(
        scenario_id="A2",
        fixture_paths={
            f"{A2_SPACE_PREFIX}msg{i:02d}.txt": f"file-id-{i}" for i in range(file_count)
        },
    )


def _trace_with_correct_set_metadata_calls(file_count: int = 3) -> AgentTrace:
    return AgentTrace(
        final_answer="done",
        tool_calls=tuple(
            ToolCall(
                tool_name="set_file_metadata",
                arguments={
                    "file_id_or_path": f"{A2_SPACE_PREFIX}msg{i:02d}.txt",
                    "metadata_type": "json",
                    "metadata": '{"reviewed": false}',
                },
                succeeded=True,
            )
            for i in range(file_count)
        ),
    )


def _patch_federation_metadata(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Pretend get_file_metadata returns {'reviewed': value} for every file."""
    import benchmark.oracles.access as a

    async def _fake(_file_id: str, _types: list[str]) -> dict:
        return {"json": {"reviewed": value}}

    monkeypatch.setattr(a.files_api, "get_file_metadata", _fake)


@pytest.mark.asyncio
async def test_both_axes_pass_when_agent_called_correctly_and_federation_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_federation_metadata(monkeypatch, value=False)
    result = await verify_a2(_ctx(), _trace_with_correct_set_metadata_calls())
    assert result.mcp_pass is True
    assert result.federation_pass is True


@pytest.mark.asyncio
async def test_mcp_pass_federation_fail_signals_onedata_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact case the user surfaced 2026-05-01: agent did the right
    thing but Onedata's post-state doesn't reflect it (e.g. dbsync delay).
    Trial gets credit on mcp_pass; federation_pass exposes the divergence
    for §7 narrative."""
    # Federation says "no metadata set" — like dbsync didn't propagate yet.
    import benchmark.oracles.access as a

    async def _fake(_file_id: str, _types: list[str]) -> dict:
        return {"json": None}

    monkeypatch.setattr(a.files_api, "get_file_metadata", _fake)

    result = await verify_a2(_ctx(), _trace_with_correct_set_metadata_calls())
    assert result.mcp_pass is True
    assert result.federation_pass is False
    assert "MCP succeeded but federation diverged" in result.diagnosis


@pytest.mark.asyncio
async def test_mcp_fail_federation_pass_when_agent_did_nothing_but_state_already_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: agent didn't call anything, but federation happens to be
    in the expected state (e.g. fixture already had reviewed=false from a
    previous trial that didn't get cleaned up). Still counts as mcp_pass=False
    — the agent didn't do its job."""
    _patch_federation_metadata(monkeypatch, value=False)
    trace = AgentTrace(final_answer="done")  # no tool calls
    result = await verify_a2(_ctx(), trace)
    assert result.mcp_pass is False
    assert result.federation_pass is True


@pytest.mark.asyncio
async def test_both_axes_fail_when_agent_did_nothing_and_federation_doesnt_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_federation_metadata(monkeypatch, value=True)  # 'reviewed': True ← wrong
    trace = AgentTrace(final_answer="done")  # no tool calls
    result = await verify_a2(_ctx(), trace)
    assert result.mcp_pass is False
    assert result.federation_pass is False
