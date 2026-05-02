"""Per-trial orchestration: wire fixture_runner, adapter, oracle into one cycle.

Public entry: `run_trial(adapter, scenario, mcp_app, ...)` → TrialArtefact.

The trial cycle:
  1. `prepare_trial(scenario)` resets the federation subtree and materialises
     fixtures. On FixtureResetTimeout, retry up to `reset_retries` times.
  2. `adapter.dispatch(...)` runs the LLM agent loop, dispatching tool calls
     through `mcp_app.call_tool(...)`. Adapter exceptions → ADAPTER_ERROR.
  3. `get_oracle(scenario.id)(ctx, trace)` returns OracleResult. Oracle
     exceptions propagate (oracle bugs should crash, not be swallowed —
     per benchmark/oracles/__init__.py contract).

Each trial produces one TrialArtefact (regardless of outcome) which the
run-panel script persists as a JSONL line.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path

from benchmark._per_llm_spaces import (
    PER_LLM_SPACE,
    PER_LLM_SPACE_ID,
)
from benchmark._runtime_types import (
    AgentTrace,
    FixtureResetTimeout,
    OracleResult,
    RunContext,
)
from benchmark._scenario_specialise import DEFAULT_SPACE, specialise_for_space
from benchmark._scenario_types import Scenario
from benchmark._trial_artefact import TrialArtefact, TrialOutcome, write_artefact
from benchmark.fixture_runner import prepare_trial
from benchmark.llm_adapters import LLMAdapter
from benchmark.oracles import get_oracle


def _resolve_space_for(llm_name: str) -> tuple[str, str | None]:
    """Look up (space_name, space_id) for an LLM. Falls back to the
    shared default if the LLM isn't registered.
    """
    name = PER_LLM_SPACE.get(llm_name, DEFAULT_SPACE)
    sid = PER_LLM_SPACE_ID.get(llm_name)
    return name, sid


SYSTEM_PROMPT = (
    "You are an assistant for the SPICE federated data platform. The user "
    "will ask questions about files, metadata, providers, and replication "
    "state. You have a set of tools that read and write the federation; use "
    "them as needed. After you have finished using tools, give a final "
    "answer in plain text. Be concise and follow the format the user requests."
)


async def run_trial(
    *,
    adapter: LLMAdapter,
    scenario: Scenario,
    mcp_app,
    run_id: str,
    trial_ix: int,
    artefact_dir: Path,
    reset_retries: int = 3,
) -> TrialArtefact:
    """Run one (LLM, scenario, trial) cycle and persist the artefact.

    On RESET_FAIL, the artefact is still written so the aggregator can
    distinguish reset failures from agent failures. Adapter errors likewise
    produce an artefact with outcome=ADAPTER_ERROR.
    """
    started_at = time.time()

    # Specialise the scenario to this LLM's dedicated Onedata space.
    # The brief, fixture paths, oracle_check are rewritten so the agent
    # operates entirely within its own space and other LLMs running
    # the same scenario in parallel can't corrupt each other's
    # fixtures. See research/empirical-mcp-server-findings.md M-2.
    space_name, space_id = _resolve_space_for(adapter.config.name)
    specialised = specialise_for_space(scenario, space_name)

    ctx, reset_error = await _prepare_with_retry(
        specialised, reset_retries, space_name=space_name, space_id=space_id
    )

    if ctx is None:
        return _persist(
            _build_reset_fail(
                adapter=adapter,
                scenario=specialised,
                run_id=run_id,
                trial_ix=trial_ix,
                started_at=started_at,
                error=reset_error or "unknown reset failure",
            ),
            artefact_dir,
        )

    try:
        dispatch = await adapter.dispatch(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=specialised.brief,
            mcp_app=mcp_app,
            allowed_tools=specialised.allowed_tools_minimal,
        )
    except Exception as e:  # noqa: BLE001
        return _persist(
            _build_adapter_error(
                adapter=adapter,
                scenario=specialised,
                ctx=ctx,
                run_id=run_id,
                trial_ix=trial_ix,
                started_at=started_at,
                error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
            ),
            artefact_dir,
        )

    trace = AgentTrace(
        final_answer=dispatch.final_answer,
        tool_calls=dispatch.tool_calls,
    )
    oracle = get_oracle(specialised.id)
    oracle_result: OracleResult = await oracle(ctx, trace)

    outcome: TrialOutcome = "PASS" if oracle_result.mcp_pass else "FAIL"
    denied = tuple(dispatch.raw_trace.get("denied_calls") or ())
    artefact = TrialArtefact(
        run_id=run_id,
        llm_name=adapter.config.name,
        llm_model_id=adapter.config.model_id,
        scenario_id=specialised.id,
        trial_ix=trial_ix,
        started_at=started_at,
        ended_at=time.time(),
        fixture_started_at=ctx.fixture_started_at,
        fixture_ready_at=ctx.fixture_ready_at,
        outcome=outcome,
        final_answer=dispatch.final_answer,
        rounds_used=dispatch.rounds_used,
        finish_reason=dispatch.finish_reason,
        usage_in_tokens=dispatch.usage_in_tokens,
        usage_out_tokens=dispatch.usage_out_tokens,
        wall_clock_seconds=dispatch.wall_clock_seconds,
        tool_calls=dispatch.tool_calls,
        oracle_mcp_pass=oracle_result.mcp_pass,
        oracle_federation_pass=oracle_result.federation_pass,
        oracle_diagnosis=oracle_result.diagnosis,
        denied_calls=denied,
        error=dispatch.error,
    )
    return _persist(artefact, artefact_dir)


async def _prepare_with_retry(
    scenario: Scenario,
    reset_retries: int,
    *,
    space_name: str,
    space_id: str | None,
) -> tuple[RunContext | None, str | None]:
    """Run prepare_trial up to (1 + reset_retries) times; return last error.

    Retries on transient federation errors (`OnedataApiError`,
    `FixtureResetTimeout`) with exponential backoff (1s, 2s, 4s, ...).
    Non-transient errors (programming bugs, missing fixtures) propagate
    immediately as `prepare_trial error: ...`. See research/empirical-mcp-
    server-findings.md for the federation transient pattern that motivates
    this retry layer.
    """
    import asyncio as _asyncio

    from onedata_mcp.utils import OnedataApiError

    last_error: str | None = None
    for attempt in range(reset_retries + 1):
        try:
            ctx = await prepare_trial(scenario, space_name=space_name, space_id=space_id)
            return ctx, None
        except FixtureResetTimeout as e:
            last_error = f"FixtureResetTimeout (attempt {attempt + 1}): {e}"
        except OnedataApiError as e:
            last_error = f"OnedataApiError (attempt {attempt + 1}): {e}"
        except Exception as e:  # noqa: BLE001
            return None, f"prepare_trial error: {type(e).__name__}: {e}"
        # Backoff before next attempt — federation HTTP transients usually
        # clear in a few seconds.
        if attempt < reset_retries:
            await _asyncio.sleep(2**attempt)
    return None, last_error


def _build_reset_fail(
    *,
    adapter: LLMAdapter,
    scenario: Scenario,
    run_id: str,
    trial_ix: int,
    started_at: float,
    error: str,
) -> TrialArtefact:
    return TrialArtefact(
        run_id=run_id,
        llm_name=adapter.config.name,
        llm_model_id=adapter.config.model_id,
        scenario_id=scenario.id,
        trial_ix=trial_ix,
        started_at=started_at,
        ended_at=time.time(),
        fixture_started_at=started_at,
        fixture_ready_at=0.0,
        outcome="RESET_FAIL",
        final_answer="",
        rounds_used=0,
        finish_reason=None,
        usage_in_tokens=0,
        usage_out_tokens=0,
        wall_clock_seconds=0.0,
        tool_calls=(),
        oracle_mcp_pass=None,
        oracle_federation_pass=None,
        oracle_diagnosis="",
        error=error,
    )


def _build_adapter_error(
    *,
    adapter: LLMAdapter,
    scenario: Scenario,
    ctx: RunContext,
    run_id: str,
    trial_ix: int,
    started_at: float,
    error: str,
) -> TrialArtefact:
    return TrialArtefact(
        run_id=run_id,
        llm_name=adapter.config.name,
        llm_model_id=adapter.config.model_id,
        scenario_id=scenario.id,
        trial_ix=trial_ix,
        started_at=started_at,
        ended_at=time.time(),
        fixture_started_at=ctx.fixture_started_at,
        fixture_ready_at=ctx.fixture_ready_at,
        outcome="ADAPTER_ERROR",
        final_answer="",
        rounds_used=0,
        finish_reason="adapter_error",
        usage_in_tokens=0,
        usage_out_tokens=0,
        wall_clock_seconds=0.0,
        tool_calls=(),
        oracle_mcp_pass=None,
        oracle_federation_pass=None,
        oracle_diagnosis="",
        error=error,
    )


def _persist(artefact: TrialArtefact, artefact_dir: Path) -> TrialArtefact:
    write_artefact(artefact, artefact_dir)
    return artefact
