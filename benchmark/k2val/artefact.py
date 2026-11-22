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
`NEEDS_REVIEW`: the deterministic reader and the judge read the answer differently, or the judge
failed (`answer_reading`); it is classified by hand, and no record is complete while one remains.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    "NEEDS_REVIEW",
)
# Outcomes that count as the model's answer being judged.
JUDGED = ("PASS", "FAIL")
# The two answer readers disagreed, or the judge failed: classified by hand before it counts.

# Outcomes of the harness or the deployment, never of the model: the trial did not really run
# (a federation that was down, a harness bug, an adapter that could not start). A resume runs
# such a trial again; every other outcome (`NO_RESULT`, a cap reached, included) is a result.
INFRASTRUCTURE_OUTCOMES = ("RESET_FAIL", "HARNESS_ERROR", "ORACLE_ERROR", "ADAPTER_ERROR")

# (config name, scenario id, trial index): one planned trial of a run.
TrialKey = tuple[str, str, int]


def is_valid_outcome(outcome: object) -> bool:
    """A record with this outcome is a result: it stays when a run is resumed, and it wins over
    an earlier infrastructure failure of the same trial."""
    return outcome not in INFRASTRUCTURE_OUTCOMES


def trial_key(row: dict[str, Any]) -> TrialKey | None:
    """The planned trial a record belongs to; None for a record that does not say (an artefact
    from before `trial_ix`), which then counts for itself."""
    ix = row.get("trial_ix")
    name, scenario = row.get("llm_name"), row.get("scenario_id")
    if isinstance(ix, int) and not isinstance(ix, bool) and name and scenario:
        return (str(name), str(scenario), ix)
    return None


def latest_per_trial[T](items: Iterable[T], row_of: Callable[[T], dict[str, Any]]) -> list[T]:
    """The records that count, `items` being in the order they were written.

    A trial can have several records: a resume appends a new one to a run's files, and a
    trial that failed for the infrastructure's sake is run again. Per trial the LAST valid
    record wins (`is_valid_outcome`), else the last record: an earlier infrastructure failure
    of a trial that later ran validly is not counted, and a valid record is never replaced by
    a later failure. The order of `items` is kept."""
    listed = list(items)
    chosen: dict[TrialKey, int] = {}
    for i, item in enumerate(listed):
        key = trial_key(row_of(item))
        if key is None:
            continue
        j = chosen.get(key)
        if (
            j is None
            or is_valid_outcome(row_of(item).get("outcome"))
            or not is_valid_outcome(row_of(listed[j]).get("outcome"))
        ):
            chosen[key] = i
    keep = set(chosen.values())
    return [
        item
        for i, item in enumerate(listed)
        if (key := trial_key(row_of(item))) is None or i in keep
    ]


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
    # The start state, as the local provider reported it before the agent started:
    # {"files": {name: reading}, "tree": the trial directory's tree or None} (BuiltFixture.baseline)
    fixture_baseline: dict | None = None
    writer: dict | None = None
    calls: tuple[dict, ...] = ()
    fixture_error: str | None = None
    teardown_problems: tuple[str, ...] = ()
    # The tools the model was offered (checked against the scenario's set before dispatch).
    offered_tools: tuple[str, ...] = ()
    # sha256 of the offered listing as the records hash it (surfaces.py); None: not checked
    offered_surface_sha: str | None = None
    # Every check the trial was scored on (scoring.py): the answer, the oracle's end state, the
    # required call, the restraint checks, writes inside the trial directory.
    checks: tuple[dict, ...] = ()
    # What scoring needs beside the snapshots, baseline, writer and calls (scoring.py), so that
    # a trial can be scored again offline.
    oracle_context: dict | None = None
    # Both readings of the final answer (scoring.score_dual): the deterministic reader's, the
    # judge's verdict (label or names, evidence, model, effort, rubric hash, error), whether they
    # agree, the reading that decided, and for NEEDS_REVIEW the outcome each would give.
    answer_reading: dict | None = None
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


def read_trials(run_dir: Path, *, all_records: bool = False) -> list[dict]:
    """The records of a run that count: per trial the last valid one, else the last
    (`latest_per_trial`). `all_records=True`: every line of every file, as written."""
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*__*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows if all_records else latest_per_trial(rows, lambda r: r)


def valid_trial_keys(run_dir: Path) -> set[TrialKey]:
    """The trials of a run that already have a valid record (a resume does not run them)."""
    return {
        key
        for row in read_trials(run_dir, all_records=True)
        if is_valid_outcome(row.get("outcome")) and (key := trial_key(row)) is not None
    }
