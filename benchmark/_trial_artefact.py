"""Per-trial artefact: the JSON record persisted for each (LLM, scenario, trial).

One JSONL file per (LLM, scenario) pair lives in `<artefact_dir>/<run_id>/`,
with one line per trial. The pass^k aggregator (#22) ingests these files
to compute headline metrics.

Schema is intentionally flat — easier to grep and to ingest into pandas /
duckdb / R. Tool calls are nested as a list of dicts.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from benchmark._runtime_types import ToolCall

TrialOutcome = Literal["PASS", "FAIL", "RESET_FAIL", "ADAPTER_ERROR"]


@dataclass(frozen=True)
class TrialArtefact:
    """One row in the panel × scenarios × K results matrix.

    `oracle_*` fields are None when the trial never reached the oracle
    (RESET_FAIL or ADAPTER_ERROR). `error` carries the diagnostic when set.
    """

    run_id: str
    llm_name: str
    llm_model_id: str
    scenario_id: str
    trial_ix: int
    started_at: float
    ended_at: float
    fixture_started_at: float
    fixture_ready_at: float
    outcome: TrialOutcome
    final_answer: str
    rounds_used: int
    finish_reason: str | None
    usage_in_tokens: int
    usage_out_tokens: int
    wall_clock_seconds: float
    tool_calls: tuple[ToolCall, ...]
    oracle_mcp_pass: bool | None
    oracle_federation_pass: bool | None
    oracle_diagnosis: str
    # Built-in tools the agent attempted to use that the adapter denied
    # (e.g. Bash, Read, ToolSearch). Empty for the OpenAI-compat leg, which
    # has no built-in toolset to fall back on. Paper-relevant: shows
    # harness-bundled-tool substitution behaviour. See
    # benchmark/llm_adapters/claude_agent_sdk.py for the gate logic.
    denied_calls: tuple[dict, ...] = ()
    error: str | None = None
    # Per-round adapter trace: full request/response shapes for each
    # tool-use round of the conversation. Captured for false-negative
    # analysis — once K=8 lands, we want to be able to inspect WHY a
    # specific trial decided what it decided without re-running. Schema
    # comes from the adapter's `dispatch.raw_trace` (typically
    # {"rounds": [{messages_pre_request, tool_calls_emitted,
    # finish_reason, content_text_length}, ...]}). Optional — adapters
    # that don't capture rounds (e.g. claude-agent-sdk wrapping the
    # local CC binary) leave this empty.
    raw_trace: dict | None = None


def to_json_dict(artefact: TrialArtefact) -> dict:
    """Serialise to a plain dict suitable for `json.dumps`."""
    return dataclasses.asdict(artefact)


def write_artefact(artefact: TrialArtefact, dest_dir: Path) -> Path:
    """Append the artefact as a JSONL line. Returns the file path written.

    Filename convention: `<llm_name>__<scenario_id>.jsonl` so per-(LLM,
    scenario) trial sequences are colocated. The trial runner is the only
    writer; concurrent trials within a single run target different files.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{artefact.llm_name}__{artefact.scenario_id}.jsonl"
    path = dest_dir / fname
    line = json.dumps(to_json_dict(artefact), ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path
