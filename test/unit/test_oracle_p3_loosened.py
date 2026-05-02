"""Tests for the P3 oracle's loosened mcp_pass criterion.

Per L-1 finding (research/llm-output-stability-findings.md):
Qwen3.6-35B sometimes terminates a P3 conversation with empty
content after correct tool calls. The federation work lands
correctly, but the strict text-match for "transfer"/"fulfilled"
trivially fails on an empty answer. The loosening accepts the
trial when:

    added_qos AND polled AND (answer_ok OR federation_pass)

This file pins the loosened behaviour so a future strict-mode
revert is a deliberate change with a failing test, not silent.
"""

from __future__ import annotations

import pytest

from benchmark._runtime_types import AgentTrace, RunContext, ToolCall
from benchmark.oracles.placement import verify_p3

P3_PATH = "/ppam_2026_mcp_tests/p3/result.bin"
P3_FILE_ID = "p3-file-id"


def _ctx() -> RunContext:
    return RunContext(scenario_id="P3", fixture_paths={P3_PATH: P3_FILE_ID})


def _trace_with_correct_calls(final_answer: str) -> AgentTrace:
    return AgentTrace(
        final_answer=final_answer,
        tool_calls=(
            ToolCall(
                tool_name="add_file_qos_requirement",
                arguments={
                    "expression": "country=PL",
                    "file_id_or_path": P3_PATH,
                    "replicas_num": 2,
                },
                succeeded=True,
            ),
            ToolCall(
                tool_name="get_file_qos_summary",
                arguments={"file_id_or_path": P3_PATH},
                succeeded=True,
            ),
            ToolCall(
                tool_name="list_space_transfers",
                arguments={"space_id": "any", "state": "ongoing"},
                succeeded=True,
            ),
        ),
    )


def _stub_federation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transfer_seen: bool = False,
    fulfilled: bool = False,
) -> None:
    import benchmark.oracles.placement as p

    async def _fake_space_id() -> str:
        return "stub-space-id"

    async def _fake_any_transfer(_space_id: str, _state: str, _file_id: str) -> bool:
        return transfer_seen

    async def _fake_summary(_file_id: str) -> dict:
        return {"requirements": {"r1": "fulfilled" if fulfilled else "pending"}}

    monkeypatch.setattr(p, "_space_id", _fake_space_id)
    monkeypatch.setattr(p, "_any_transfer_for_file", _fake_any_transfer)
    monkeypatch.setattr(p.qos_api, "get_file_qos_summary", _fake_summary)
    # Speed up the polling loop.
    monkeypatch.setattr(p, "DYNAMIC_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(p, "DYNAMIC_POLL_INTERVAL", 0.05)


@pytest.mark.asyncio
async def test_p3_loosening_empty_answer_with_federation_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L-1 case: Qwen empty-content + federation observed effect."""
    _stub_federation(monkeypatch, transfer_seen=True)
    result = await verify_p3(_ctx(), _trace_with_correct_calls(final_answer=""))
    assert result.mcp_pass is True
    assert result.federation_pass is True


@pytest.mark.asyncio
async def test_p3_strict_path_still_works_when_no_federation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict text-match path: federation didn't observe but answer is
    explicit. Useful for slower federations where the deadline expires
    before the transfer materialises."""
    _stub_federation(monkeypatch, transfer_seen=False, fulfilled=False)
    answer = "Added QoS rule; observed a transfer for this file in the log."
    result = await verify_p3(_ctx(), _trace_with_correct_calls(final_answer=answer))
    assert result.mcp_pass is True
    assert result.federation_pass is False


@pytest.mark.asyncio
async def test_p3_fails_when_empty_answer_and_no_federation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loosening must not paper over real fails: empty answer AND
    federation didn't see the effect → mcp_pass=False."""
    _stub_federation(monkeypatch, transfer_seen=False, fulfilled=False)
    result = await verify_p3(_ctx(), _trace_with_correct_calls(final_answer=""))
    assert result.mcp_pass is False
    assert result.federation_pass is False
    assert (
        "answer doesn't claim 'transfer' or 'fulfilled' "
        "and federation didn't observe the effect either"
    ) in result.diagnosis


@pytest.mark.asyncio
async def test_p3_fails_when_no_qos_call_regardless_of_federation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loosening targets answer-emission, not tool-use. If the agent
    didn't call add_file_qos_requirement at all, it fails even when
    federation happens to be in the right state from a prior trial."""
    _stub_federation(monkeypatch, transfer_seen=True, fulfilled=True)
    trace = AgentTrace(final_answer="all good", tool_calls=())
    result = await verify_p3(_ctx(), trace)
    assert result.mcp_pass is False
    assert "no add_file_qos_requirement" in result.diagnosis
