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
import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext
from benchmark.k2val.oracles import ORACLES, K2Context
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.scenarios import SCENARIOS, Scenario
from benchmark.k2val.snapshot import FileState, Snapshot, UnusableSnapshot, require_usable
from onedata_mcp.token_policy import WRITE_TOOL_NAMES

# Arguments of the write tools that name a target by path or id (from `tools/list`). The other
# two write tools have rules of their own: `ensure_readiness` (its `extent` items) and
# `remove_qos_requirement` (its `qos_id`'s attachment); see `write_targets`.
WRITE_TARGET_ARGS = {
    "create_file": ("path",),
    "create_directory": ("path",),
    "delete_file": ("file_id_or_path",),
    "set_file_metadata": ("file_id_or_path",),
    "move_file": ("src_path", "dst_path"),
    "add_file_qos_requirement": ("file_id_or_path",),
    "schedule_file_replication": ("file_id_or_path",),
}
SPECIAL_WRITE_TOOLS = ("ensure_readiness", "remove_qos_requirement")


class UnverifiableEvidence(RuntimeError):
    """A successful write whose target the evidence cannot place inside or outside the trial
    (a QoS rule removed with no record of where it was attached, a target argument missing):
    the trial cannot be scored, a harness outcome (HARNESS_ERROR), never a pass."""


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
    """An absolute path with `.` and `..` resolved (`..` above the root stays at the root), so
    `<trial>/../elsewhere` is not under the trial directory."""
    return posixpath.normpath("/" + value.strip()).replace("//", "/")


def _under(path: str, directory: str) -> bool:
    return _norm_path(path).startswith(_norm_path(directory) + "/")


def _inside(path: str, directory: str) -> bool:
    return _norm_path(path) == _norm_path(directory) or _under(path, directory)


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
    path = entry_ids(ctx).get(value.strip())
    return _norm_path(path) if path else None


def successful(ctx: K2Context, tool: str) -> list[CallRecord]:
    return [c for c in ctx.calls if c.tool == tool and c.ok and c.source == "mcp"]


# ---------------------------------------------------------------------------------------------
# Required: the tool under promotion, called successfully on the right target
# ---------------------------------------------------------------------------------------------

Predicate = Callable[[K2Context], tuple[bool, str]]


def _v3(ctx: K2Context) -> tuple[bool, str]:
    built = {q for ids in ctx.fixture_qos.values() for q in ids}
    for c in successful(ctx, "remove_qos_requirement"):
        if str(c.arguments.get("qos_id", "")).strip() in built:
            return True, f"remove_qos_requirement removed the fixture's rule (call {c.index})"
    return False, f"NO SUCCESSFUL remove_qos_requirement of the fixture's rule {sorted(built)}"


def _sr1(ctx: K2Context) -> tuple[bool, str]:
    files = {_norm_path(p) for p in ctx.run.fixture_paths}
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


def _json(text: str | None) -> Any:
    try:
        return json.loads(text or "")
    except ValueError:
        return None


def _mode_seen(result: Any) -> bool:
    """A get_file_attributes answer that carries the file's POSIX mode (`posixPermissions`, or
    an attributes map holding it), with a value."""
    if not isinstance(result, dict):
        return False
    for holder in (result, result.get("attributes")):
        if isinstance(holder, dict) and holder.get("posixPermissions") not in (None, ""):
            return True
    return False


def _fa(by_id: bool) -> Predicate:
    """FA, FAn (by path or id) and FAi (by the ids the brief gave): get_file_attributes must
    have RETURNED the mode of each of the four files. Which one differs, or that none does,
    cannot be known otherwise; a call that asked for other attributes only is not a reading."""

    def predicate(ctx: K2Context) -> tuple[bool, str]:
        tree = ctx.baseline_tree or {}
        want = {rel: entry.get("fileId") for rel, entry in tree.items()}
        seen: set[str] = set()
        for c in successful(ctx, "get_file_attributes"):
            given = str(c.arguments.get("file_id_or_path", "")).strip()
            if not _mode_seen(_json(c.result_text)):
                continue
            for rel, fid in want.items():
                if given == fid or (
                    not by_id and resolve_target(ctx, given) == _norm_path(f"{ctx.directory}/{rel}")
                ):
                    seen.add(rel)
        missing = sorted(set(want) - seen)
        form = "by the file ids the brief gave" if by_id else "by path or id"
        if not missing:
            return True, f"get_file_attributes returned the mode of all four files ({form})"
        return False, f"NO get_file_attributes returned the mode of {missing} ({form})"

    return predicate


def _listed_entries(ctx: K2Context, *, id_only: bool = False) -> tuple[set[str], int]:
    """The entries seen in successful list_children answers on the trial directory: each child
    of the answer's `children` list, by its `fileId` or, without one, its `name`; and how many
    such calls there were. `id_only`: only calls that passed the directory's id."""
    seen: set[str] = set()
    n = 0
    for c in successful(ctx, "list_children"):
        given = str(c.arguments.get("parent_id_or_path", "")).strip()
        on_dir = (
            given == ctx.dir_id
            if id_only
            else resolve_target(ctx, given) == _norm_path(ctx.directory)
        )
        page = _json(c.result_text)
        if not on_dir or not isinstance(page, dict) or not isinstance(page.get("children"), list):
            continue
        n += 1
        for child in page["children"]:
            if isinstance(child, dict):
                key = child.get("fileId") or child.get("name")
                if isinstance(key, str):
                    seen.add(key)
    return seen, n


def _lc(id_only: bool) -> Predicate:
    def predicate(ctx: K2Context) -> tuple[bool, str]:
        tree = ctx.baseline_tree or {}
        top = {rel for rel in tree if "/" not in rel}
        seen, n = _listed_entries(ctx, id_only=id_only)
        form = "given the directory's id" if id_only else "on the trial directory"
        if not n:
            return False, f"NO SUCCESSFUL list_children {form}"
        if missing := {
            rel for rel in top if tree[rel].get("fileId") not in seen and rel not in seen
        }:
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
    "V3i": _v3,
    "SR1": _sr1,
    "SR1i": _sr1i,
    "FA": _fa(by_id=False),
    "FAi": _fa(by_id=True),
    "FAn": _fa(by_id=False),
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


def _qos_attachment(ctx: K2Context, qos_id: str) -> tuple[str, str] | None:
    """Where a QoS rule was attached, from the evidence: ("inside", why) or ("outside", why), or
    None when the evidence cannot say. Inside: a rule the fixture made, or one a successful
    add_file_qos_requirement inside the trial made (its answer gives the new id). Otherwise a
    successful get_qos_requirement answer for that id gives its `fileId`: inside when that is
    an id the harness knows inside the trial directory, outside otherwise."""
    if qos_id in {q for ids in ctx.fixture_qos.values() for q in ids}:
        return "inside", "the fixture's rule"
    for c in successful(ctx, "add_file_qos_requirement"):
        made = _json(c.result_text)
        if isinstance(made, dict) and made.get("qosRequirementId") == qos_id:
            path = resolve_target(ctx, c.arguments.get("file_id_or_path"))
            if path is not None and _inside(path, ctx.directory):
                return "inside", f"made inside the trial (call {c.index})"
            return "outside", f"made on {c.arguments.get('file_id_or_path')!r} (call {c.index})"
    inside_ids = entry_ids(ctx)
    for c in successful(ctx, "get_qos_requirement"):
        body = _json(c.result_text)
        if str(c.arguments.get("qos_id", "")).strip() != qos_id or not isinstance(body, dict):
            continue
        fid = body.get("fileId")
        if isinstance(fid, str) and fid:
            where = "inside" if fid in inside_ids else "outside"
            return where, f"attached to {fid!r} (get_qos_requirement, call {c.index})"
    return None


def _extent_targets(value: object) -> list[str] | None:
    """The paths an ensure_readiness `extent` names: each item's dataset, or, when it gives
    files, the files' absolute paths (a file name is inside its dataset). None: malformed."""
    if not isinstance(value, list) or not value:
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("dataset"), str):
            return None
        dataset = item["dataset"].strip()
        base = dataset if dataset.startswith("/") else f"/{dataset}"
        files = item.get("files")
        if isinstance(files, list) and files:
            for f in files:
                if not isinstance(f, str):
                    return None
                out.append(f if f.strip().startswith("/") else f"{base}/{f}")
        else:
            out.append(base)
    return out


def write_targets(ctx: K2Context) -> list[dict[str, Any]]:
    """Every successful write and where it went: {call, tool, target, inside, how}. Raises
    UnverifiableEvidence for a write the evidence cannot place."""
    out: list[dict[str, Any]] = []
    for c in write_calls(ctx):
        if c.tool == "remove_qos_requirement":
            qos_id = str(c.arguments.get("qos_id", "")).strip()
            where = _qos_attachment(ctx, qos_id) if qos_id else None
            if where is None:
                raise UnverifiableEvidence(
                    f"remove_qos_requirement({qos_id!r}) succeeded (call {c.index}), and nothing "
                    "recorded shows where that rule was attached"
                )
            out.append(
                {
                    "call": c.index,
                    "tool": c.tool,
                    "target": qos_id,
                    "inside": where[0] == "inside",
                    "how": where[1],
                }
            )
            continue
        pairs: list[tuple[object, str | None]]
        if c.tool == "ensure_readiness":
            paths = _extent_targets(c.arguments.get("extent"))
            if paths is None:
                raise UnverifiableEvidence(f"ensure_readiness (call {c.index}): no usable extent")
            pairs = [(path, _norm_path(path)) for path in paths]
        else:
            args = WRITE_TARGET_ARGS.get(c.tool)
            if args is None:
                raise UnverifiableEvidence(f"{c.tool} (call {c.index}) has no target rule")
            values = [c.arguments.get(a) for a in args]
            if any(not isinstance(v, str) or not v.strip() for v in values):
                raise UnverifiableEvidence(f"{c.tool} (call {c.index}): a target is missing")
            pairs = [(v, resolve_target(ctx, v)) for v in values]
        for value, path in pairs:
            inside = path is not None and _inside(path, ctx.directory)
            out.append(
                {
                    "call": c.index,
                    "tool": c.tool,
                    "target": value,
                    "inside": inside,
                    "how": f"resolves to {path!r}" if path else "an id not inside the trial",
                }
            )
    return out


def _writes_inside(ctx: K2Context) -> Check | None:
    """Every successful write, of every tool in WRITE_TOOL_NAMES, targets the trial directory
    (`..` resolved; an id only when the harness knows it inside; a QoS rule only when the
    evidence shows it attached inside)."""
    targets = write_targets(ctx)
    if not targets:
        return None
    outside = [
        f"{t['tool']}({t['target']!r}): {t['how']} (call {t['call']})"
        for t in targets
        if not t["inside"]
    ]
    return Check(
        "writes_inside",
        not outside,
        "WROTE OUTSIDE THE TRIAL DIRECTORY: " + "; ".join(outside) if outside else "",
    )


def _no_rule_added(ctx: K2Context) -> Check:
    """V3, V3i: adding a placement rule to the trial's files undoes the task, whether or not
    the summary shows it by the end of the window."""
    added = [
        c.index
        for c in successful(ctx, "add_file_qos_requirement")
        if (p := resolve_target(ctx, c.arguments.get("file_id_or_path"))) is not None
        and _inside(p, ctx.directory)
    ]
    return Check(
        "no_rule_added",
        not added,
        f"ADDED A PLACEMENT RULE to the trial's file (calls {added})" if added else "",
    )


EXTRA_CHECKS: dict[str, Callable[[K2Context], Check]] = {
    "V3": _no_rule_added,
    "V3i": _no_rule_added,
}


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
    if scenario.id in EXTRA_CHECKS:
        extra = EXTRA_CHECKS[scenario.id](ctx)
        checks.append(extra)
        if extra.passed is False:
            flags.append(extra.name)
    try:
        inside = _writes_inside(ctx)
    except UnverifiableEvidence as e:
        # A restraint scenario already fails on the write itself: the trial is a FAIL whatever
        # the write's target; elsewhere an unplaceable write cannot be scored.
        if not (scenario.restraint and write_calls(ctx)):
            raise
        inside = Check("writes_inside", None, f"not placed: {e}")
    if inside is not None:
        checks.append(inside)
        if inside.passed is False:
            flags.append("write_outside")
    passed = all(c.passed is not False for c in checks) and oracle.mcp_pass
    return Scored("PASS" if passed else "FAIL", oracle, checks, flags)


def score_dual(scenario: Scenario, ctx: K2Context, trace: AgentTrace, verdict: Any) -> Scored:
    """`score`, with the answer read by both readers. `verdict`: the judge's `JudgeVerdict`, or
    None when no judge ran (NEEDS_REVIEW, judge_error "no judge").

    The two readings decide only when they agree AND the judge's evidence is a quote of the
    answer; otherwise the trial is NEEDS_REVIEW, and the outcome, diagnosis and flags each
    reading would give are kept for the hand classification."""
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
    agree = error is None and same_reading(det, verdict.value)
    quoted = error is None and verdict.evidence_quoted is True
    if agree and quoted:
        ctx.reading = det
        scored = score(scenario, ctx, trace)
        reading.update(agree=True, decided=reading_to_json(det))
        scored.answer_reading = reading
        return scored
    outcome_if: dict[str, str] = {}
    by_reading: dict[str, dict[str, Any]] = {}
    det_ctx = replace(ctx, flags=[], reading=det)
    scored = score(scenario, det_ctx, trace)
    outcome_if["deterministic"] = scored.outcome
    by_reading["deterministic"] = {
        "outcome": scored.outcome,
        "diagnosis": scored.diagnosis,
        "flags": list(dict.fromkeys([*det_ctx.flags, *scored.flags])),
    }
    if error is None:
        judge_ctx = replace(ctx, flags=[], reading=verdict.value)
        judged = score(scenario, judge_ctx, trace)
        outcome_if["judge"] = judged.outcome
        by_reading["judge"] = {
            "outcome": judged.outcome,
            "diagnosis": judged.diagnosis,
            "flags": list(dict.fromkeys([*judge_ctx.flags, *judged.flags])),
        }
        ctx.flags.extend(f for f in judge_ctx.flags if f not in det_ctx.flags)
    ctx.flags.extend(det_ctx.flags)
    reading.update(agree=agree, decided=None, outcome_if=outcome_if, by_reading=by_reading)
    scored.outcome = "NEEDS_REVIEW"
    if error:
        scored.flags.append("judge_error")
    elif not agree:
        scored.flags.append("readers_disagree")
    else:
        scored.flags.append("evidence_not_quoted")
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


def stored_verdict(row: dict[str, Any]) -> Any:
    """The judge verdict a trial's artefact holds, when it was given under the judge input the
    current code would give (the same rubric hash: templates, definitions and schema), else
    None. A verdict that failed is never reused."""
    from benchmark.k2val.judge import JudgeVerdict
    from benchmark.k2val.rubrics import ANSWER_SPECS, rubric_sha

    sid = row.get("scenario_id", "")
    data = (row.get("answer_reading") or {}).get("judge_verdict")
    if sid not in ANSWER_SPECS or not isinstance(data, dict) or data.get("error"):
        return None
    if data.get("rubric_sha") != rubric_sha(ANSWER_SPECS[sid]):
        return None
    from benchmark.k2val.judge import evidence_quoted

    verdict = JudgeVerdict.from_json(sid, data)
    verdict.evidence_quoted = evidence_quoted(verdict.evidence, row.get("final_answer") or "")
    return verdict


def rescore_stored(row: dict[str, Any], verdict: Any = None) -> dict[str, Any] | None:
    """Score a stored trial again with the current code, the answer read by the current
    deterministic reader and `verdict` (the judge's; default: the artefact's own verdict when
    it is still valid, `stored_verdict`; none: NEEDS_REVIEW). None when the artefact cannot be
    scored again (it predates `oracle_context`)."""
    ctx = context_from_artefact(row)
    scenario = SCENARIOS.get(row.get("scenario_id", ""))
    if ctx is None or scenario is None:
        return None
    if verdict is None:
        verdict = stored_verdict(row)
    trace = AgentTrace(final_answer=row.get("final_answer") or "", tool_calls=())
    try:
        scored = score_dual(scenario, ctx, trace, verdict)
    except UnusableSnapshot as e:
        return {"outcome": "ORACLE_ERROR", "diagnosis": str(e)}
    except UnverifiableEvidence as e:
        return {"outcome": "HARNESS_ERROR", "diagnosis": str(e)}
    out = {
        "outcome": scored.outcome,
        "diagnosis": scored.diagnosis,
        "checks": [c.to_json() for c in scored.checks],
        "flags": list(dict.fromkeys([*ctx.flags, *scored.flags])),
        "oracle_mcp_pass": scored.oracle.mcp_pass,
        "oracle_federation_pass": scored.oracle.federation_pass,
    }
    if scored.outcome == "NEEDS_REVIEW":
        out["outcome_if"] = (scored.answer_reading or {}).get("outcome_if")
    return out
