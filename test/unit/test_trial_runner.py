"""Unit tests for the trial runner.

Two layers of test:
1. Artefact roundtrip — the JSONL contract the pass^k aggregator (#22)
   depends on. If this changes, both this test AND the aggregator must
   change in lockstep.
2. Trial-runner orchestration — wires a fake adapter + fake oracle + fake
   prepare_trial monkey-patch to verify the four outcomes (PASS, FAIL,
   RESET_FAIL, ADAPTER_ERROR) all materialise correct artefacts.

No live federation, no live LLM calls. Pure-Python.
"""

from __future__ import annotations

import json

import pytest

from benchmark._runtime_types import (
    FixtureResetTimeout,
    OracleResult,
    RunContext,
    ToolCall,
)
from benchmark._trial_artefact import TrialArtefact, write_artefact
from benchmark.llm_adapters import LLMConfig, TrialDispatch

# ---------------------------------------------------------------------------
# Artefact JSONL roundtrip
# ---------------------------------------------------------------------------


def test_artefact_roundtrip_parses_back(tmp_path):
    """JSONL line written by write_artefact must parse cleanly via json.loads."""
    artefact = TrialArtefact(
        run_id="20260501T2200",
        llm_name="llama-3.3-70b",
        llm_model_id="meta-llama/Llama-3.3-70B-Instruct",
        scenario_id="D1",
        trial_ix=0,
        started_at=1714600000.0,
        ended_at=1714600005.0,
        fixture_started_at=1714599998.0,
        fixture_ready_at=1714600000.0,
        outcome="PASS",
        final_answer="| space | provider_count |",
        rounds_used=2,
        finish_reason="stop",
        usage_in_tokens=1234,
        usage_out_tokens=42,
        wall_clock_seconds=4.5,
        tool_calls=(
            ToolCall(
                tool_name="list_user_spaces",
                arguments={},
                succeeded=True,
                error=None,
                result='[{"name":"ppam_2026_mcp_tests"}]',
            ),
        ),
        oracle_mcp_pass=True,
        oracle_federation_pass=None,
        oracle_diagnosis="",
        error=None,
    )
    path = write_artefact(artefact, tmp_path)
    assert path.exists()

    line = path.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["run_id"] == "20260501T2200"
    assert parsed["llm_name"] == "llama-3.3-70b"
    assert parsed["scenario_id"] == "D1"
    assert parsed["outcome"] == "PASS"
    assert parsed["oracle_mcp_pass"] is True
    assert parsed["oracle_federation_pass"] is None
    assert isinstance(parsed["tool_calls"], list) and len(parsed["tool_calls"]) == 1
    assert parsed["tool_calls"][0]["tool_name"] == "list_user_spaces"


def test_artefact_appends_to_same_file(tmp_path):
    """Two artefacts for the same (LLM, scenario) write two lines, append-mode."""
    common_kwargs = dict(
        run_id="r",
        llm_name="L",
        llm_model_id="m",
        scenario_id="D1",
        started_at=0.0,
        ended_at=0.0,
        fixture_started_at=0.0,
        fixture_ready_at=0.0,
        outcome="PASS",
        final_answer="",
        rounds_used=0,
        finish_reason=None,
        usage_in_tokens=0,
        usage_out_tokens=0,
        wall_clock_seconds=0.0,
        tool_calls=(),
        oracle_mcp_pass=True,
        oracle_federation_pass=None,
        oracle_diagnosis="",
        error=None,
    )
    write_artefact(TrialArtefact(trial_ix=0, **common_kwargs), tmp_path)
    write_artefact(TrialArtefact(trial_ix=1, **common_kwargs), tmp_path)

    path = tmp_path / "L__D1.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["trial_ix"] == 0
    assert json.loads(lines[1])["trial_ix"] == 1


# ---------------------------------------------------------------------------
# Trial-runner orchestration with fakes
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Adapter that returns a configurable TrialDispatch (or raises)."""

    def __init__(
        self, *, dispatch: TrialDispatch | None = None, raise_exc: Exception | None = None
    ):
        self.config = LLMConfig(name="fake-llm", model_id="fake/model-1")
        self._dispatch = dispatch
        self._raise_exc = raise_exc

    async def dispatch(self, *, system_prompt, user_prompt, mcp_app, allowed_tools):
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._dispatch is not None
        return self._dispatch


def _ok_dispatch() -> TrialDispatch:
    return TrialDispatch(
        final_answer="answer",
        tool_calls=(),
        rounds_used=1,
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_runner_pass_outcome(tmp_path, monkeypatch):
    """Happy path: prepare succeeds, adapter returns dispatch, oracle says pass."""
    from benchmark import trial_runner
    from benchmark.oracles import __init__ as oracles_init  # noqa: F401

    async def fake_prepare(scenario):
        return RunContext(
            scenario_id=scenario.id,
            fixture_started_at=100.0,
            fixture_ready_at=101.0,
        )

    async def fake_oracle(ctx, trace):
        return OracleResult(mcp_pass=True, federation_pass=None, diagnosis="")

    monkeypatch.setattr(trial_runner, "prepare_trial", fake_prepare)
    monkeypatch.setattr(trial_runner, "get_oracle", lambda sid: fake_oracle)

    from benchmark._scenario_types import Scenario

    scenario = Scenario(
        id="D1",
        band="discovery",
        brief="brief",
        oracle_tier="format",
        required_tools=frozenset(),
        allowed_tools_minimal=frozenset(),
    )

    adapter = _FakeAdapter(dispatch=_ok_dispatch())
    artefact = await trial_runner.run_trial(
        adapter=adapter,
        scenario=scenario,
        mcp_app=None,
        run_id="r",
        trial_ix=0,
        artefact_dir=tmp_path,
    )
    assert artefact.outcome == "PASS"
    assert artefact.oracle_mcp_pass is True


@pytest.mark.asyncio
async def test_runner_reset_fail_outcome(tmp_path, monkeypatch):
    """RESET_FAIL: prepare_trial keeps timing out beyond reset_retries."""
    from benchmark import trial_runner

    async def fake_prepare(scenario):
        raise FixtureResetTimeout("simulated timeout")

    monkeypatch.setattr(trial_runner, "prepare_trial", fake_prepare)

    from benchmark._scenario_types import Scenario

    scenario = Scenario(
        id="D1",
        band="discovery",
        brief="brief",
        oracle_tier="format",
        required_tools=frozenset(),
        allowed_tools_minimal=frozenset(),
    )

    artefact = await trial_runner.run_trial(
        adapter=_FakeAdapter(dispatch=_ok_dispatch()),
        scenario=scenario,
        mcp_app=None,
        run_id="r",
        trial_ix=0,
        artefact_dir=tmp_path,
        reset_retries=1,
    )
    assert artefact.outcome == "RESET_FAIL"
    assert artefact.oracle_mcp_pass is None
    assert "FixtureResetTimeout" in (artefact.error or "")


@pytest.mark.asyncio
async def test_runner_adapter_error_outcome(tmp_path, monkeypatch):
    """ADAPTER_ERROR: adapter.dispatch raises; trial still produces an artefact."""
    from benchmark import trial_runner

    async def fake_prepare(scenario):
        return RunContext(scenario_id=scenario.id, fixture_started_at=0.0, fixture_ready_at=1.0)

    monkeypatch.setattr(trial_runner, "prepare_trial", fake_prepare)

    from benchmark._scenario_types import Scenario

    scenario = Scenario(
        id="D1",
        band="discovery",
        brief="brief",
        oracle_tier="format",
        required_tools=frozenset(),
        allowed_tools_minimal=frozenset(),
    )

    adapter = _FakeAdapter(raise_exc=RuntimeError("LLM API down"))
    artefact = await trial_runner.run_trial(
        adapter=adapter,
        scenario=scenario,
        mcp_app=None,
        run_id="r",
        trial_ix=0,
        artefact_dir=tmp_path,
    )
    assert artefact.outcome == "ADAPTER_ERROR"
    assert artefact.oracle_mcp_pass is None
    assert "RuntimeError: LLM API down" in (artefact.error or "")


@pytest.mark.asyncio
async def test_runner_fail_outcome_when_oracle_says_no(tmp_path, monkeypatch):
    """FAIL: dispatch succeeded but oracle.mcp_pass is False."""
    from benchmark import trial_runner

    async def fake_prepare(scenario):
        return RunContext(scenario_id=scenario.id, fixture_started_at=0.0, fixture_ready_at=1.0)

    async def fake_oracle(ctx, trace):
        return OracleResult(mcp_pass=False, federation_pass=False, diagnosis="agent invented data")

    monkeypatch.setattr(trial_runner, "prepare_trial", fake_prepare)
    monkeypatch.setattr(trial_runner, "get_oracle", lambda sid: fake_oracle)

    from benchmark._scenario_types import Scenario

    scenario = Scenario(
        id="A1",
        band="access",
        brief="brief",
        oracle_tier="static",
        required_tools=frozenset(),
        allowed_tools_minimal=frozenset(),
    )

    artefact = await trial_runner.run_trial(
        adapter=_FakeAdapter(dispatch=_ok_dispatch()),
        scenario=scenario,
        mcp_app=None,
        run_id="r",
        trial_ix=0,
        artefact_dir=tmp_path,
    )
    assert artefact.outcome == "FAIL"
    assert artefact.oracle_mcp_pass is False
    assert artefact.oracle_diagnosis == "agent invented data"
