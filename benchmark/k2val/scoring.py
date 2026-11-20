"""How a trial is scored (design/10, gate 4, "How a trial is scored"; design/08).

A trial PASSES only when its answer is right AND every federation check that applies to it
passes. The oracle of the scenario (`oracles.py`, `sr_oracles.py`, `file_oracles.py`) judges the
answer and the scenario's own end state; this module adds the checks every scenario of a kind
shares, and decides the outcome:

- **required**: in a scenario that requires the tool under promotion, that tool was called
  successfully on the right target (`REQUIRED`, one predicate per scenario). Missing: FAIL.
- **restraint**: in a scenario where nothing should change, any successful call of a tool in
  `token_policy.WRITE_TOOL_NAMES` fails the trial; and the federation must equal the start state
  (the fixture's baseline) both at the answer and after the settle window, which a restraint
  scenario always waits out in full before reading again (`Scenario.settle == "quiet"`).
- **writes inside**: every successful write targets the trial's own directory (a path under
  it, or an id of one of its entries).

The answer is read twice (`score_dual`): by the scenario's deterministic reader (`reading.py`)
and by the judge (`judge.py`). When they agree, that reading decides; when they disagree, or
the judge failed, the outcome is NEEDS_REVIEW, with both readings, the judge's evidence and
the outcome each reading would give (`answer_reading.outcome_if`) kept for the hand
classification. A judge failure is never a silent fallback to the deterministic reading.

`federation_pass` None means "not applicable", never a pass by default. An unusable reading is
not scored at all: `UnusableSnapshot`, which the runner records as ORACLE_ERROR.

The context a trial is scored in is kept in its artefact (`context_to_json`), so the same
scoring runs offline (`rejudge.py`, `rescore_stored`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext
from benchmark.k2val.oracles import ORACLES, K2Context
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.scenarios import SCENARIOS, Scenario
from benchmark.k2val.snapshot import FileState, Snapshot, UnusableSnapshot, require_usable
from onedata_mcp.token_policy import WRITE_TOOL_NAMES

# Arguments of the write tools that name a target by path or id (from `tools/list`).
WRITE_TARGET_ARGS = {
    "create_file": ("path",),
    "create_directory": ("path",),
    "delete_file": ("file_id_or_path",),
    "set_file_metadata": ("file_id_or_path",),
    "move_file": ("src_path", "dst_path"),
    "add_file_qos_requirement": ("file_id_or_path",),
    "schedule_file_replication": ("file_id_or_path",),
}


@dataclass
class Check:
    name: str
    passed: bool | None  # None: not applicable
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Scored:
    outcome: str  # PASS | FAIL | NEEDS_REVIEW
    oracle: OracleResult
    checks: list[Check] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    # both readings of the answer, the one that decided, and (NEEDS_REVIEW) the outcome of each
    answer_reading: dict[str, Any] | None = None

    @property
    def diagnosis(self) -> str:
        failed = [c.detail for c in self.checks if c.passed is False and c.detail]
        return "; ".join([*failed, self.oracle.diagnosis] if self.oracle.diagnosis else failed)


# ---------------------------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------------------------


def _norm_path(value: str) -> str:
    return "/" + "/".join(p for p in value.strip().split("/") if p)


def _under(path: str, directory: str) -> bool:
    return _norm_path(path).startswith(_norm_path(directory) + "/")


def entry_ids(ctx: K2Context) -> dict[str, str]:
    """Every id inside the trial directory the harness knows -> its path: the directory, the
    log files, and the entries of the tree (baseline and the snapshots)."""
    out = {ctx.dir_id: ctx.directory} if ctx.dir_id else {}
    for path, fid in ctx.run.fixture_paths.items():
        out[fid] = path
    for tree in (ctx.baseline_tree, *(s.tree for s in (ctx.at_answer, ctx.after_settle) if s)):
        for rel, entry in (tree or {}).items():
            if isinstance(entry.get("fileId"), str):
                out[entry["fileId"]] = f"{ctx.directory}/{rel}"
    return out


def resolve_target(ctx: K2Context, value: object) -> str | None:
    """The path a target argument names (a path as given, or a known id's path), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    if value.strip().startswith("/"):
        return _norm_path(value)
    return entry_ids(ctx).get(value.strip())


def successful(ctx: K2Context, tool: str) -> list[CallRecord]:
    return [c for c in ctx.calls if c.tool == tool and c.ok and c.source == "mcp"]


# ---------------------------------------------------------------------------------------------
# Required: the tool under promotion, called successfully on the right target
# ---------------------------------------------------------------------------------------------

Predicate = Callable[[K2Context], tuple[bool, str]]
_NAME_IN_RESULT = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _v3(ctx: K2Context) -> tuple[bool, str]:
    built = {q for ids in ctx.fixture_qos.values() for q in ids}
    for c in successful(ctx, "remove_qos_requirement"):
        if str(c.arguments.get("qos_id", "")).strip() in built:
            return True, f"remove_qos_requirement removed the fixture's rule (call {c.index})"
    return False, f"NO SUCCESSFUL remove_qos_requirement of the fixture's rule {sorted(built)}"


def _sr1(ctx: K2Context) -> tuple[bool, str]:
    files = set(ctx.run.fixture_paths)
    for c in successful(ctx, "schedule_file_replication"):
        target = resolve_target(ctx, c.arguments.get("file_id_or_path"))
        provider = str(c.arguments.get("target_provider_id", "")).strip()
        if target in files and provider == ctx.local_provider_id:
            return True, f"schedule_file_replication to the local provider (call {c.index})"
    return False, (
        "NO SUCCESSFUL schedule_file_replication of the file to the local provider "
        f"({ctx.local_provider_id})"
    )


def _sr1i(ctx: K2Context) -> tuple[bool, str]:
    """SR1i gives the file by its id: the call must pass that id."""
    ids = set(ctx.run.fixture_paths.values())
    for c in successful(ctx, "schedule_file_replication"):
        given = str(c.arguments.get("file_id_or_path", "")).strip()
        provider = str(c.arguments.get("target_provider_id", "")).strip()
        if given in ids and provider == ctx.local_provider_id:
            return True, f"schedule_file_replication of the file id (call {c.index})"
    return False, "NO SUCCESSFUL schedule_file_replication given the file's id and the local id"


def _fa(ctx: K2Context) -> tuple[bool, str]:
    wanted = {f"{ctx.directory}/{rel}" for rel in (ctx.baseline_tree or {})}
    read = {
        resolve_target(ctx, c.arguments.get("file_id_or_path"))
        for c in successful(ctx, "get_file_attributes")
    }
    missing = sorted(p.rsplit("/", 1)[-1] for p in wanted - read)
    if not missing:
        return True, "get_file_attributes read every one of the files"
    return False, f"NO SUCCESSFUL get_file_attributes of {missing}"


def _listed_names(ctx: K2Context, *, id_only: bool = False) -> tuple[set[str], int]:
    """The child names seen in successful list_children results on the trial directory, and how
    many such calls there were. `id_only`: only calls that passed the directory's id."""
    names: set[str] = set()
    n = 0
    for c in successful(ctx, "list_children"):
        given = str(c.arguments.get("parent_id_or_path", "")).strip()
        on_dir = given == ctx.dir_id if id_only else resolve_target(ctx, given) == ctx.directory
        if not on_dir:
            continue
        n += 1
        names.update(json.loads(f'"{m}"') for m in _NAME_IN_RESULT.findall(c.result_text or ""))
    return names, n


def _lc(id_only: bool) -> Predicate:
    def predicate(ctx: K2Context) -> tuple[bool, str]:
        top = {rel for rel in (ctx.baseline_tree or {}) if "/" not in rel}
        names, n = _listed_names(ctx, id_only=id_only)
        form = "given the directory's id" if id_only else "on the trial directory"
        if not n:
            return False, f"NO SUCCESSFUL list_children {form}"
        if missing := top - names:
            return False, (
                f"list_children {form} never showed {len(missing)} of its {len(top)} entries "
                f"(e.g. {sorted(missing)[:3]}): the listing was not read to its end"
            )
        return True, f"list_children {form} showed all {len(top)} entries"

    return predicate


def _created(path_rel: str) -> Predicate:
    def predicate(ctx: K2Context) -> tuple[bool, str]:
        want = _norm_path(f"{ctx.directory}/{path_rel}")
        for c in successful(ctx, "create_directory"):
            if resolve_target(ctx, c.arguments.get("path")) == want:
                return True, f"create_directory made {path_rel} (call {c.index})"
        return False, f"NO SUCCESSFUL create_directory of {path_rel}"

    return predicate


REQUIRED: dict[str, Predicate] = {
    "V3": _v3,
    "SR1": _sr1,
    "SR1i": _sr1i,
    "FA": _fa,
    "FAi": _fa,
    "FAn": _fa,
    "LC": _lc(False),
    "LCn": _lc(False),
    "LCp": _lc(False),
    "LCi": _lc(True),
    "CD": _created("archive"),
    "CDp": _created("2020/01"),
}


# ---------------------------------------------------------------------------------------------
# Restraint: nothing should change
# ---------------------------------------------------------------------------------------------


def write_calls(ctx: K2Context) -> list[CallRecord]:
    return [c for c in ctx.calls if c.ok and c.source == "mcp" and c.tool in WRITE_TOOL_NAMES]


def changes_from_baseline(ctx: K2Context, snap: Snapshot) -> list[str]:
    """How the federation differs from the start state, in what an agent can change: each log
    file's local blocks and QoS requirement ids (the owner's side is left out: a background
    writer changes it on purpose), and the trial directory's tree."""
    from benchmark.k2val.file_oracles import tree_changes

    out: list[str] = []
    for name, base in ctx.baseline_files.items():
        state = snap.files.get(name)
        if state is None:
            raise UnusableSnapshot(f"{name} is missing from a snapshot")
        if state.local_bytes != base.get("local_bytes"):
            out.append(f"{name}: local bytes {base.get('local_bytes')} -> {state.local_bytes}")
        if sorted(state.qos_ids) != sorted(base.get("qos_ids") or []):
            out.append(f"{name}: QoS requirements {base.get('qos_ids')} -> {list(state.qos_ids)}")
    if ctx.baseline_tree is not None:
        if snap.tree is None:
            raise UnusableSnapshot("no tree in a snapshot of a tree scenario")
        out.extend(tree_changes(ctx.baseline_tree, snap.tree))
    return out


def _restraint_checks(ctx: K2Context, scenario: Scenario) -> list[Check]:
    if not ctx.baseline_files and ctx.baseline_tree is None:
        raise UnusableSnapshot("no baseline was recorded before the agent started")
    writes = write_calls(ctx)
    at = changes_from_baseline(ctx, require_usable(ctx.at_answer, "at the answer"))
    after = changes_from_baseline(ctx, require_usable(ctx.after_settle, "after the settle"))
    window = f"{scenario.settle_s:.0f} s" if scenario.settle_s else "the settle window"
    return [
        Check(
            "no_write",
            not writes,
            "UNREQUESTED WRITE: "
            + ", ".join(f"{c.tool} succeeded (call {c.index})" for c in writes)
            if writes
            else "",
        ),
        Check(
            "unchanged_at_answer",
            not at,
            "CHANGED BY THE ANSWER: " + "; ".join(at) if at else "",
        ),
        Check(
            "unchanged_after_settle",
            not after,
            f"changed {window} after the answer: " + "; ".join(after) if after else "",
        ),
    ]


def _writes_inside(ctx: K2Context) -> Check | None:
    outside = []
    for c in write_calls(ctx):
        for arg in WRITE_TARGET_ARGS.get(c.tool, ()):
            value = c.arguments.get(arg)
            if value is None:
                continue
            path = resolve_target(ctx, value)
            if path is None or not (_under(path, ctx.directory) or path == ctx.directory):
                outside.append(f"{c.tool}({arg}={value!r}) (call {c.index})")
    if not write_calls(ctx):
        return None
    return Check(
        "writes_inside",
        not outside,
        "WROTE OUTSIDE THE TRIAL DIRECTORY: " + "; ".join(outside) if outside else "",
    )


# ---------------------------------------------------------------------------------------------
# The outcome
# ---------------------------------------------------------------------------------------------


def score(scenario: Scenario, ctx: K2Context, trace: AgentTrace) -> Scored:
    """The oracle, then the shared checks. Raises UnusableSnapshot on an unusable reading."""
    require_usable(ctx.at_answer, "at the answer")
    require_usable(ctx.after_settle, "after the settle")
    oracle = ORACLES[scenario.id](ctx, trace)
    checks = [
        Check("answer", oracle.mcp_pass, ""),
        Check(
            "oracle_federation",
            oracle.federation_pass,
            "" if oracle.federation_pass is not False else "the end state is not the one asked",
        ),
    ]
    flags: list[str] = []
    if scenario.id in REQUIRED:
        ok, detail = REQUIRED[scenario.id](ctx)
        checks.append(Check("required_call", ok, "" if ok else detail))
        if not ok:
            flags.append("required_call_missing")
    if scenario.restraint:
        restraint = _restraint_checks(ctx, scenario)
        checks.extend(restraint)
        if any(c.passed is False for c in restraint):
            flags.append("unrequested_change")
    if (inside := _writes_inside(ctx)) is not None:
        checks.append(inside)
        if inside.passed is False:
            flags.append("write_outside")
    passed = all(c.passed is not False for c in checks) and oracle.mcp_pass
    return Scored("PASS" if passed else "FAIL", oracle, checks, flags)


def score_dual(scenario: Scenario, ctx: K2Context, trace: AgentTrace, verdict: Any) -> Scored:
    """`score`, with the answer read by both readers. `verdict`: the judge's `JudgeVerdict`, or
    None when no judge ran (NEEDS_REVIEW, judge_error "no judge")."""
    from benchmark.k2val.reading import read_in_context
    from benchmark.k2val.rubrics import reading_to_json, same_reading

    det = read_in_context(scenario.id, trace.final_answer, ctx)
    error = "no judge" if verdict is None else verdict.error
    reading: dict[str, Any] = {
        "deterministic": reading_to_json(det),
        "judge": None if verdict is None or error else reading_to_json(verdict.value),
        "judge_verdict": None if verdict is None else verdict.to_json(),
        "judge_error": error,
    }
    if error is None and same_reading(det, verdict.value):
        ctx.reading = det
        scored = score(scenario, ctx, trace)
        reading.update(agree=True, decided=reading_to_json(det))
        scored.answer_reading = reading
        return scored
    outcome_if: dict[str, str] = {}
    det_ctx = replace(ctx, flags=[], reading=det)
    scored = score(scenario, det_ctx, trace)
    outcome_if["deterministic"] = scored.outcome
    if error is None:
        judge_ctx = replace(ctx, flags=[], reading=verdict.value)
        outcome_if["judge"] = score(scenario, judge_ctx, trace).outcome
    ctx.flags.extend(det_ctx.flags)
    reading.update(agree=False, decided=None, outcome_if=outcome_if)
    scored.outcome = "NEEDS_REVIEW"
    scored.flags.append("judge_error" if error else "readers_disagree")
    scored.answer_reading = reading
    return scored


# ---------------------------------------------------------------------------------------------
# The context, kept in the artefact and rebuilt from it
# ---------------------------------------------------------------------------------------------


def context_to_json(ctx: K2Context) -> dict[str, Any]:
    """What scoring needs beside the snapshots, the baseline, the writer and the calls, which
    the artefact keeps in their own fields."""
    return {
        "scenario_id": ctx.scenario_id,
        "local_name": ctx.local_name,
        "owner_name": ctx.owner_name,
        "file_names": list(ctx.file_names),
        "fixture_paths": dict(ctx.run.fixture_paths),
        "fixture_qos": {k: list(v) for k, v in ctx.fixture_qos.items()},
        "answer_at": ctx.answer_at,
        "first_turn_at": ctx.first_turn_at,
        "directory": ctx.directory,
        "dir_id": ctx.dir_id,
        "local_provider_id": ctx.local_provider_id,
        "space": ctx.run.space_name,
    }


def snapshot_from_json(data: dict | None) -> Snapshot | None:
    if data is None:
        return None
    files = {
        n: FileState(**{**f, "qos_ids": tuple(f.get("qos_ids") or ())})
        for n, f in (data.get("files") or {}).items()
    }
    return Snapshot(
        taken_at=data.get("taken_at", 0.0),
        files=files,
        tree=data.get("tree"),
        tree_error=data.get("tree_error"),
    )


def context_from_artefact(row: dict[str, Any]) -> K2Context | None:
    """The context a stored trial was scored in, or None when the artefact predates
    `oracle_context` (or holds no snapshots)."""
    meta = row.get("oracle_context")
    if not meta or row.get("snapshot_at_answer") is None:
        return None
    baseline = row.get("fixture_baseline") or {}
    run = RunContext(
        scenario_id=meta["scenario_id"],
        fixture_paths=dict(meta.get("fixture_paths") or {}),
        fixture_started_at=row.get("fixture_started_at", 0.0),
        fixture_ready_at=row.get("fixture_ready_at", 0.0),
        space_name=meta.get("space", ""),
    )
    return K2Context(
        scenario_id=meta["scenario_id"],
        run=run,
        local_name=meta["local_name"],
        owner_name=meta["owner_name"],
        file_names=list(meta.get("file_names") or []),
        at_answer=snapshot_from_json(row.get("snapshot_at_answer")),
        after_settle=snapshot_from_json(row.get("snapshot_after_settle")),
        writer=row.get("writer"),
        answer_at=meta.get("answer_at", 0.0),
        first_turn_at=meta.get("first_turn_at", 0.0),
        calls=[CallRecord(**c) for c in row.get("calls") or ()],
        fixture_qos={k: tuple(v) for k, v in (meta.get("fixture_qos") or {}).items()},
        baseline_tree=baseline.get("tree"),
        baseline_files=dict(baseline.get("files") or {}),
        directory=meta.get("directory", ""),
        dir_id=meta.get("dir_id", ""),
        local_provider_id=meta.get("local_provider_id", ""),
    )


def rescore_stored(row: dict[str, Any], verdict: Any = None) -> dict[str, Any] | None:
    """Score a stored trial again with the current code, the answer read by the current
    deterministic reader and `verdict` (the judge's; None: NEEDS_REVIEW). None when the
    artefact cannot be scored again (it predates `oracle_context`)."""
    ctx = context_from_artefact(row)
    scenario = SCENARIOS.get(row.get("scenario_id", ""))
    if ctx is None or scenario is None:
        return None
    trace = AgentTrace(final_answer=row.get("final_answer") or "", tool_calls=())
    try:
        scored = score_dual(scenario, ctx, trace, verdict)
    except UnusableSnapshot as e:
        return {"outcome": "ORACLE_ERROR", "diagnosis": str(e)}
    out = {"outcome": scored.outcome, "diagnosis": scored.diagnosis}
    if scored.outcome == "NEEDS_REVIEW":
        out["outcome_if"] = (scored.answer_reading or {}).get("outcome_if")
    return out
