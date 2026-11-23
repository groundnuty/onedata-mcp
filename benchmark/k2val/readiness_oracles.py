"""The oracles of the readiness tools' scenarios: RC, RCn, RCo, RCp, RE, REn, REo, REp
(design/17, design/18). Each scenario's brief, fixture, tools, right answer and rules are in
`readiness_scenarios.py`. Here:

- the ORACLES (`READINESS_ORACLES`), pure functions of a `K2Context` and the trace. RC and RCp
  ask for SAYS_READY; RCn and REn for NOT_READY_WRITING with what is said of the files being
  written true (`oracles.verify_writing`, V5's rule); RCo is V1's oracle and REo SR1's. RE and
  REp judge the end state at the answer (each file asked about covered locally and held by a
  new QoS requirement; REp: the others left as they were) and after the window. The restraint
  scenarios' federation checks are `scoring.py`'s.
- the answer READERS and the right readings (`READINESS_READERS`, `READINESS_RIGHT`): the
  shared vocabulary's (`oracles.read_check`), V1's and SR1's.
- the REQUIRED CALLS (`READINESS_REQUIRED`): a successful call of the tool whose `extent`
  selects every file asked about, by the tool's own rules (`extent_selects`). REo's is SR1's,
  which `scoring.py` registers itself.

An unreadable snapshot is never judged: `UnusableSnapshot` (the runner records ORACLE_ERROR).
This module imports `scoring` only inside functions: `scoring` imports `oracles`, which
registers this module.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from benchmark._runtime_types import AgentTrace, OracleResult
from benchmark.k2val import oracles, sr_oracles
from benchmark.k2val.readiness_scenarios import READINESS_SCENARIOS, WRITING
from benchmark.k2val.snapshot import FileState, UnusableSnapshot, require_usable
from onedata_mcp.api.readiness import _normalise_extent, name_date

if TYPE_CHECKING:
    from benchmark.k2val.oracles import K2Context

SAYS_READY = "SAYS_READY"
WRITING_NAMES = tuple(f"log_{d}.dat" for d in WRITING)


def _result(mcp: bool, fed: bool | None, parts: list[str]) -> OracleResult:
    return OracleResult(mcp_pass=mcp, federation_pass=fed, diagnosis="; ".join(parts))


def asked_files(ctx: K2Context) -> list[str]:
    """The file names the scenario's brief asks about (every fixture file, unless it names
    some)."""
    asked = READINESS_SCENARIOS[ctx.scenario_id].asked
    return list(asked) if asked is not None else list(ctx.file_names)


# --- the extent, read as the tool reads it -------------------------------------------------------


def _norm(path: str) -> str:
    return posixpath.normpath("/" + path.strip()).replace("//", "/")


def extent_selects(extent: object, paths: Iterable[str]) -> set[str]:
    """The paths (of `paths`) an `extent` argument selects, by the tool's own rules: per item,
    every file under its dataset, or those its `files` name (a name, an absolute path, or a
    path relative to the dataset) and those whose YYYYMMDD falls in one of its `date_ranges`.
    An extent the tool would refuse selects nothing."""
    try:
        items = _normalise_extent(extent)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return set()
    known = [_norm(p) for p in paths]
    chosen: set[str] = set()
    for item in items:
        root = _norm(item.root)
        under = [p for p in known if p.startswith(root.rstrip("/") + "/")]
        if not item.files and not item.ranges:
            chosen.update(under)
            continue
        for name in item.files:
            if name.startswith("/"):
                chosen.update(p for p in under if p == _norm(name))
            elif "/" in name:
                chosen.update(p for p in under if p == _norm(f"{root}/{name}"))
            else:
                chosen.update(p for p in under if p.rsplit("/", 1)[-1] == name)
        for start, end in item.ranges:
            for p in under:
                day = name_date(p.rsplit("/", 1)[-1])
                if day is not None and start <= day <= end:
                    chosen.add(p)
    return chosen


def _required(tool: str) -> Callable[[K2Context], tuple[bool, str]]:
    """A successful call of `tool` whose extent selects every file the brief asks about."""

    def predicate(ctx: K2Context) -> tuple[bool, str]:
        from benchmark.k2val.scoring import successful

        want = {_norm(f"{ctx.directory}/{name}") for name in asked_files(ctx)}
        for c in successful(ctx, tool):
            if want <= extent_selects(c.arguments.get("extent"), ctx.run.fixture_paths):
                return True, f"{tool} on every file asked about (call {c.index})"
        return False, f"NO SUCCESSFUL {tool} whose extent selects every file asked about"

    return predicate


# --- the oracles ------------------------------------------------------------------------------


def verify_ready(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """RC, RCp: the files asked about are ready, and the answer says so (SAYS_READY). Nothing
    should change: the federation is the shared restraint checks' (federation None)."""
    label, _ = oracles._check_reading(ctx, trace)
    if label == SAYS_READY:
        return _result(True, None, ["says the files asked about are ready"])
    why = {
        "NOT_READY_WRITING": "says files are still being written, and none asked about is",
        "NOT_READY_ELSEWHERE": "says files are at another site, and none asked about is",
    }.get(label, "the answer does not say the files are ready")
    return _result(False, None, [f"{why} ({label})"])


def _state(ctx: K2Context, snap: Any, name: str) -> FileState:
    state = snap.files.get(name)
    if state is None:
        raise UnusableSnapshot(f"{name} is missing from a snapshot")
    return state


def _new_rules(ctx: K2Context, name: str, state: FileState) -> set[str]:
    base = ctx.baseline_files.get(name) or {}
    return set(state.qos_ids) - set(base.get("qos_ids") or ())


def _left_alone(ctx: K2Context, snap: Any, names: Iterable[str]) -> list[str]:
    """How files not asked about differ from their start state (local bytes, rules)."""
    out = []
    for name in names:
        state = _state(ctx, snap, name)
        base = ctx.baseline_files.get(name) or {}
        if state.local_bytes != base.get("local_bytes"):
            out.append(f"{name}: local bytes {base.get('local_bytes')} -> {state.local_bytes}")
        if sorted(state.qos_ids) != sorted(base.get("qos_ids") or ()):
            out.append(f"{name}: QoS requirements {base.get('qos_ids')} -> {list(state.qos_ids)}")
    return out


def verify_ensured(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """RE, REp: made ready. mcp passes when the answer reads SAYS_READY AND, at the answer,
    every file asked about was covered by the local blocks (else PREMATURE CLAIM) and held by
    a QoS requirement it did not have at the start (else NOT HELD: the tool's answer says
    "held there"), AND (REp) the files not asked about were as they started (else CHANGED
    FILES NOT ASKED ABOUT). federation passes when, after the window, every file asked about is
    covered and those not asked about are as they started."""
    at = require_usable(ctx.at_answer, "at the answer")
    after = require_usable(ctx.after_settle, "after the settle")
    asked = asked_files(ctx)
    others = [n for n in ctx.file_names if n not in asked]
    label, _ = oracles._check_reading(ctx, trace)
    claim = label == SAYS_READY
    parts: list[str] = []
    not_local = [n for n in asked if not _state(ctx, at, n).local_covered]
    not_held = [n for n in asked if not _new_rules(ctx, n, _state(ctx, at, n))]
    touched = _left_alone(ctx, at, others)
    if not claim:
        parts.append(f"the answer does not say the files are ready ({label})")
    if claim and not_local:
        ctx.flags.append("premature_claim")
        parts.append(
            f"PREMATURE CLAIM: said ready, but the local copy did not cover {not_local} when "
            "the answer was given"
        )
    if claim and not_held and not not_local:
        ctx.flags.append("not_held")
        parts.append(f"NOT HELD: no new QoS requirement on {not_held} at the answer")
    if touched:
        ctx.flags.append("unrequested_change")
        parts.append("CHANGED FILES NOT ASKED ABOUT: " + "; ".join(touched))
    end_missing = [n for n in asked if not _state(ctx, after, n).local_covered]
    end_touched = _left_alone(ctx, after, others)
    fed = not end_missing and not end_touched
    if end_missing:
        parts.append(f"after the settle the local copy does not cover {end_missing}")
    if end_touched and not touched:
        parts.append("after the settle, files not asked about changed: " + "; ".join(end_touched))
    ok = claim and not not_local and not not_held and not touched
    return _result(ok, fed, parts or ["said ready once every file asked about was local and held"])


READINESS_ORACLES: dict[str, Callable[[K2Context, AgentTrace], OracleResult]] = {
    "RC": verify_ready,
    "RCn": oracles.verify_writing,
    "RCo": oracles.verify_v1,
    "RCp": verify_ready,
    "RE": verify_ensured,
    "REn": oracles.verify_writing,
    "REo": sr_oracles.verify_sr1,
    "REp": verify_ensured,
}

READINESS_REQUIRED: dict[str, Callable[[K2Context], tuple[bool, str]]] = {
    "RC": _required("check_readiness"),
    "RCn": _required("check_readiness"),
    "RCp": _required("check_readiness"),
    "RE": _required("ensure_readiness"),
    "REn": _required("ensure_readiness"),
    "REp": _required("ensure_readiness"),
}


# --- the readers and the right readings (reading.py) -----------------------------------------


def _check(answer: str, who: Any) -> object:
    return oracles.read_check(answer, who.local_name, who.owner_name)


READINESS_READERS: dict[str, Callable[[str, Any], object]] = {
    **dict.fromkeys(("RC", "RCn", "RCp", "RE", "REn", "REp"), _check),
    "RCo": lambda a, w: oracles.read_v1(a, tuple(w.file_names) or oracles.V1_EXPECTED),
    "REo": lambda a, w: sr_oracles.read_copy_answer(a, w.local_name, w.owner_name),
}

_READY = (SAYS_READY, frozenset())
READINESS_RIGHT: Mapping[str, object] = {
    "RC": _READY,
    "RCp": _READY,
    "RE": _READY,
    "REp": _READY,
    "RCn": ("NOT_READY_WRITING", frozenset(WRITING_NAMES)),
    "REn": ("NOT_READY_WRITING", frozenset(WRITING_NAMES)),
    "RCo": frozenset(oracles.V1_EXPECTED),
    "REo": "SAYS_WHOLE",
}


def is_right(sid: str, value: object) -> bool | None:
    """Whether a reading is a right answer's (the oracle may ask more: the state when it was
    given). The ready scenarios: SAYS_READY, whatever names. RCn, REn: V5's rule. None for the
    others (the caller compares the readings)."""
    if sid in ("RC", "RCp", "RE", "REp"):
        return isinstance(value, tuple) and value[0] == SAYS_READY
    if sid in ("RCn", "REn"):
        return (
            isinstance(value, tuple)
            and value[0] == "NOT_READY_WRITING"
            and not oracles.writing_claim_problems(frozenset(value[1]), WRITING_NAMES, 5)
        )
    return None
