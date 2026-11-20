"""The oracles of the three file tools' scenarios (FA, LC, CD; design/13-15).

Each scenario's brief, right answer and rules are in `file_scenarios.py`. Here:

- three ANSWER READERS, one per family, each one small function that returns a label (or the
  set of names claimed), so that a rubric judge can replace it: `fa_label`, `names_claimed`
  and `cd_label`. They are plain regular expressions on purpose and will misread some answers;
  every reading is in the artefact, and the test tables in test/unit/k2val/test_file_oracles.py
  pin what each one reads.
- the END-STATE check (`tree_delta`, `end_state`): the trial directory's tree at the LOCAL
  provider, at the answer and after it, against the baseline recorded before the agent started.
  An unreadable tree is never judged: the oracle raises `UnusableSnapshot`, and the runner
  records ORACLE_ERROR (a harness outcome), not a FAIL.

This module imports no other oracle module, so `oracles.py` can register it without a cycle.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from benchmark._runtime_types import AgentTrace, OracleResult
from benchmark.k2val import file_scenarios

if TYPE_CHECKING:
    from collections.abc import Callable

    from benchmark.k2val.file_scenarios import FileScenario
    from benchmark.k2val.oracles import K2Context
    from benchmark.k2val.snapshot import Snapshot


class UnusableSnapshot(RuntimeError):
    """The tree could not be read (at the answer, after it, or as the baseline): a harness or
    deployment problem, never a model's failure."""


# --- shared: reading answers, the end state, the result ------------------------------------

_EMPHASIS = re.compile(r"\*+|`")
_SENTENCES = re.compile(r"(?<=[.!?;:])\s+|\n+")


def _plain(answer: str) -> str:
    """The answer without Markdown emphasis and code ticks."""
    return _EMPHASIS.sub("", answer)


def _sentences(answer: str) -> list[str]:
    return [s for s in _SENTENCES.split(_plain(answer)) if s.strip()]


def _usable_tree(snap: Snapshot | None, which: str) -> dict[str, dict]:
    if snap is None:
        raise UnusableSnapshot(f"no snapshot {which}")
    if snap.tree_error or snap.tree is None:
        raise UnusableSnapshot(f"the tree {which} could not be read: {snap.tree_error}")
    return snap.tree


def tree_delta(baseline: dict[str, dict], now: dict[str, dict]) -> dict[str, list[str]]:
    """What changed in the trial directory: paths added, removed, and changed (another file id
    or type at the same path, or another size or mode of a file)."""
    changed = []
    for path in set(baseline) & set(now):
        old, new = baseline[path], now[path]
        keys = (
            ("fileId", "type", "size", "mode") if old.get("type") == "REG" else ("fileId", "type")
        )
        if any(old.get(k) != new.get(k) for k in keys):
            changed.append(path)
    return {
        "added": sorted(set(now) - set(baseline)),
        "removed": sorted(set(baseline) - set(now)),
        "changed": sorted(changed),
    }


def end_state(spec: FileScenario, baseline: dict[str, dict], now: dict[str, dict]) -> list[str]:
    """Why the tree is not the expected end state ([] when it is): the baseline plus exactly
    `spec.added`, each of its type, an added directory with nothing in it but other added
    entries, an added file with the text of `spec.added_content`."""
    delta = tree_delta(baseline, now)
    problems = []
    if delta["removed"]:
        problems.append(f"removed {delta['removed']}")
    if delta["changed"]:
        problems.append(f"changed {delta['changed']}")
    missing = sorted(set(spec.added) - set(delta["added"]))
    extra = sorted(set(delta["added"]) - set(spec.added))
    if missing:
        problems.append(f"missing {missing}")
    if extra:
        problems.append(f"UNREQUESTED {extra}")
    for path, kind in spec.added.items():
        entry = now.get(path)
        if entry is not None and entry.get("type") != kind:
            problems.append(f"{path} is {entry.get('type')}, not {kind}")
    for path, text in spec.added_content.items():
        entry = now.get(path)
        is_file = entry is not None and entry.get("type") == "REG"  # else reported above
        if is_file and (entry.get("content") or "").strip() != text:
            problems.append(f"{path} holds {entry.get('content')!r}, not {text!r}")
    return problems


def _result(mcp: bool, fed: bool | None, parts: list[str]) -> OracleResult:
    return OracleResult(mcp_pass=mcp, federation_pass=fed, diagnosis="; ".join(parts))


def _baseline(ctx: K2Context) -> dict[str, dict]:
    if ctx.baseline_tree is None:
        raise UnusableSnapshot("no baseline tree was recorded before the agent started")
    return ctx.baseline_tree


# Every oracle of this module, by scenario id; each family's section below adds its own.
FILE_ORACLES: dict[str, Callable[[K2Context, AgentTrace], OracleResult]] = {}


# --- FA: get_file_attributes (design/13) ---------------------------------------------------

# "none", "no file differs", "all four have the same permissions", "zero files differ", "they
# do not differ"; not "all the others have the same" (which goes with a named odd one).
_FA_NONE = re.compile(
    r"\bnone\b(?!\s+of\s+the\s+others?\b)|\bno (?:file|one|difference)s?\b|\bzero files?\b"
    r"|\ball\b(?!\s+(?:the\s+)?others?\b)[^.\n]{0,40}\b(?:same|identical)\b"
    r"|\b(?:do|does)(?:n['’]t| not) differ\b",
    re.I,
)


def fa_label(answer: str, names: tuple[str, ...]) -> str:
    """FA, FAi, FAn: one of `names`, "none", or "unclear".

    A name mentioned alone, with no "none" wording, is the answer. Otherwise the last sentence
    that names a file or says none decides: one name and no "none" wording is that name; "none"
    wording with no single name is "none"; anything else is "unclear"."""
    text = _plain(answer)
    named = {n for n in names if n in text}
    if len(named) == 1 and not _FA_NONE.search(text):
        return named.pop()
    if not named and _FA_NONE.search(text):
        return "none"
    for sentence in reversed(_sentences(answer)):
        in_sentence = {n for n in names if n in sentence}
        says_none = bool(_FA_NONE.search(sentence))
        if not in_sentence and not says_none:
            continue
        if len(in_sentence) == 1 and not says_none:
            return in_sentence.pop()
        if says_none and len(in_sentence) != 1:
            return "none"
        return "unclear"
    return "unclear"


def verify_fa(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """FA, FAi, FAn (get_file_attributes): which of four files has other permissions, or none.
    Right answer: `spec.expect`. The federation axis is not judged (None): nothing in the K1 set
    changes the trial directory's tree."""
    spec = file_scenarios.FILE_SCENARIOS[ctx.scenario_id]
    got = fa_label(trace.final_answer, file_scenarios.FA_NAMES)
    if got == spec.expect:
        return _result(True, None, [f"answers {got}"])
    return _result(False, None, [f"answers {got}, not {spec.expect}"])


FILE_ORACLES.update(dict.fromkeys(("FA", "FAi", "FAn"), verify_fa))
