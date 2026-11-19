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

    async def _fake_space_id(*_args: object, **_kwargs: object) -> str:
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


def _trace_with_string_replicas() -> AgentTrace:
    """A model that passes replicas_num as the STRING "2" (issue #16 —
    Llama-3.3-70B / Qwen3-Coder did this, crashing the oracle with
    TypeError: '>=' not supported between 'str' and 'int')."""
    return AgentTrace(
        final_answer="Set QoS to 2 EU replicas.",
        tool_calls=(
            ToolCall(
                tool_name="add_file_qos_requirement",
                arguments={
                    "expression": "country=PL",
                    "file_id_or_path": P3_PATH,
                    "replicas_num": "2",  # <-- string, not int
                },
                succeeded=True,
            ),
            ToolCall(
                tool_name="list_space_transfers",
                arguments={"space_id": "any", "state": "ongoing"},
                succeeded=True,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_p3_accepts_string_replicas_num_without_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (issue #16): replicas_num='2' must be coerced, not crash,
    and must count as replicas>=2 (the model specified 2)."""
    _stub_federation(monkeypatch, transfer_seen=True)
    result = await verify_p3(_ctx(), _trace_with_string_replicas())
    assert result.mcp_pass is True
    assert result.federation_pass is True


@pytest.mark.asyncio
async def test_p3_garbage_replicas_num_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric replicas_num coerces to 0 (fails the >=2 test) but must
    not raise."""
    _stub_federation(monkeypatch, transfer_seen=False, fulfilled=False)
    trace = AgentTrace(
        final_answer="",
        tool_calls=(
            ToolCall(
                tool_name="add_file_qos_requirement",
                arguments={
                    "expression": "country=PL",
                    "file_id_or_path": P3_PATH,
                    "replicas_num": "lots",
                },
                succeeded=True,
            ),
        ),
    )
    result = await verify_p3(_ctx(), trace)  # must not raise
    assert result.mcp_pass is False


@pytest.mark.asyncio
async def test_p3_uses_ctx_space_id_without_shared_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (issue #16): with per-LLM spaces (no shared default space),
    verify_p3 must resolve the federation query via ctx.space_id, NOT re-resolve
    the shared default (which raises RuntimeError when it doesn't exist)."""
    import benchmark.oracles.placement as p

    # Any call to the shared-name resolver must fail loudly — the fix must not
    # reach it when ctx.space_id is provided.
    async def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not fall back to shared-space resolution")

    monkeypatch.setattr("benchmark.fixture_runner._resolve_space_id_async", _boom)

    seen: dict[str, str] = {}

    async def _fake_any_transfer(space_id: str, _state: str, _file_id: str) -> bool:
        seen["space_id"] = space_id
        return True

    monkeypatch.setattr(p, "_any_transfer_for_file", _fake_any_transfer)
    monkeypatch.setattr(p, "DYNAMIC_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(p, "DYNAMIC_POLL_INTERVAL", 0.05)

    per_llm_path = "/ppam_2026_mcp_tests_llama_3_3_70b/p3/result.bin"
    ctx = RunContext(
        scenario_id="P3",
        fixture_paths={per_llm_path: P3_FILE_ID},
        space_name="ppam_2026_mcp_tests_llama_3_3_70b",
        space_id="per-llm-space-id-123",
    )
    result = await verify_p3(ctx, _trace_with_correct_calls(final_answer="done"))
    assert result.federation_pass is True
    assert seen["space_id"] == "per-llm-space-id-123"  # used ctx, not a lookup
