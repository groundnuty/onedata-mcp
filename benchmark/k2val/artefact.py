"""The per-trial record of the validation panel.

Follows `benchmark/_trial_artefact.py` (one JSONL file per (config, scenario), one line per
trial, flat fields, tool calls nested) and extends its `TrialArtefact` with what this panel
records: the directory used, both oracle snapshots, the writer's schedule, the full timed call
list, the cost, the serving id, flags, and an empty `classification` for the manual
three-layer sort (harness bug / deployment artefact / model failure).

Outcomes: `PASS` and `FAIL` come from the oracle. `NO_RESULT` is a cap reached (rounds, the
20 minute wall, tokens, a refusal): no result, not a failure. `BUDGET_STOP` and
`BUDGET_REFUSED` come from the Anthropic spend cap. `RESET_FAIL`, `ADAPTER_ERROR` and
`ORACLE_ERROR` and `HARNESS_ERROR` are the harness or the deployment, not the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from benchmark._trial_artefact import TrialArtefact, to_json_dict
from benchmark.k2val.secrets import redact

OUTCOMES = (
    "PASS",
    "FAIL",
    "NO_RESULT",
    "BUDGET_STOP",
    "BUDGET_REFUSED",
    "RESET_FAIL",
    "ADAPTER_ERROR",
    "ORACLE_ERROR",
    "HARNESS_ERROR",
)
# Outcomes that count as the model's answer being judged.
JUDGED = ("PASS", "FAIL")


@dataclass(frozen=True)
class K2TrialArtefact(TrialArtefact):
    trial_id: str = ""
    scenario_title: str = ""
    tier: str = ""
    space: str = ""
    directory: str = ""
    brief: str = ""
    no_result_reason: str | None = None
    cost_usd: float = 0.0
    serving_fingerprint: str | None = None
    response_ids: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    k2_violations: tuple[dict, ...] = ()
    snapshot_at_answer: dict | None = None
    snapshot_after_settle: dict | None = None
    writer: dict | None = None
    calls: tuple[dict, ...] = ()
    fixture_error: str | None = None
    teardown_problems: tuple[str, ...] = ()
    # Each file as the local provider reported it before the agent started (blocks, size, QoS).
    files_baseline: dict | None = None
    # Filled in by hand after the run: harness bug / deployment artefact / model failure.
    classification: str = ""


def artefact_path(run_dir: Path, llm_name: str, scenario_id: str) -> Path:
    return run_dir / f"{llm_name}__{scenario_id}.jsonl"


def write_trial(artefact: K2TrialArtefact, run_dir: Path) -> Path:
    """Append the artefact as one JSONL line, passed through the secret masker."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = artefact_path(run_dir, artefact.llm_name, artefact.scenario_id)
    line = redact(json.dumps(to_json_dict(artefact), ensure_ascii=False, default=str))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


def read_trials(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*__*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows
