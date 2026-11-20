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
from benchmark.k2val.rubrics import OTHER, normalise_name
from benchmark.k2val.snapshot import UnusableSnapshot  # the one rule (snapshot.py); re-exported

if TYPE_CHECKING:
    from collections.abc import Callable

    from benchmark.k2val.file_scenarios import FileScenario
    from benchmark.k2val.oracles import K2Context
    from benchmark.k2val.snapshot import Snapshot


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
        # a small fixture file's bytes, when both sides have read them (else its size decides)
        content = "sha256" in old and "sha256" in new and old["sha256"] != new["sha256"]
        if content or any(old.get(k) != new.get(k) for k in keys):
            changed.append(path)
    return {
        "added": sorted(set(now) - set(baseline)),
        "removed": sorted(set(baseline) - set(now)),
        "changed": sorted(changed),
    }


def tree_changes(baseline: dict[str, dict], now: dict[str, dict]) -> list[str]:
    """`tree_delta` as sentences ([] when the tree is the baseline): the restraint check."""
    delta = tree_delta(baseline, now)
    return [f"{what} {paths}" for what, paths in delta.items() if paths]


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


# --- LC: list_children (design/14) ---------------------------------------------------------

LOG_NAME = re.compile(r"log_\d{8}\.dat")

# "no subdirectories", "none", "zero subdirectories", "it has only files", "does not contain any"
_NO_SUBDIRS = re.compile(
    r"\bno (?:sub-?)?director(?:y|ies)\b|\bnone\b|\bzero (?:sub-?)?director"
    r"|\bonly (?:two )?(?:regular )?files\b|\b(?:has|have|contains?) no\b"
    r"|\b(?:does|do)(?:n['’]t| not) (?:have|contain) any\b",
    re.I,
)


# Words a list item may be that are not a name; and names of the system, not of an entry.
_NOT_A_NAME = {
    *("none", "no", "nothing", "empty", "n/a", "na", "yes", "and", "or", "the", "it", "there"),
    *("file", "files", "directory", "directories", "subdirectory", "subdirectories", "folder"),
    *("folders", "entries", "entry", "islast", "nextpagetoken", "fileid", "posixpermissions"),
}
_FILE_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_DIR_WORDS = re.compile(r"\b(?:sub-?)?(?:director(?:y|ies)|folders?)\b", re.I)
_LIST_SPLIT = re.compile(r",|;|\band\b|\bor\b|\s[—–-]\s")
_PARENS = re.compile(r"\([^)]*\)")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(\S+)", re.M)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_UNDERSCORED = re.compile(r"(?<![\w./-])([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)(?![\w.]*\.[A-Za-z])")


def _system_names() -> set[str]:
    from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED

    return {n.lower() for n in ALL_KNOWN | FLAG_GATED}


def _dir_like(token: str, exclude: set[str]) -> str | None:
    """`token` as a directory name the answer gives, or None (a file, a word, a system name)."""
    name = normalise_name(token).strip(".,;:!?")
    if not re.fullmatch(r"[A-Za-z0-9][\w-]*", name or "-") or _FILE_EXT.search(name):
        return None
    if name.lower() in _NOT_A_NAME or name.lower() in exclude:
        return None
    return name


def _extra_names(text: str, candidates: tuple[str, ...], exclude: set[str]) -> set[str]:
    """Names beyond `candidates` the answer gives as subdirectories: items of a list on a line
    that names a candidate or says "subdirectory", bullets, backticked and snake_case names."""
    tokens: list[str] = []
    tokens.extend(m.group(1) for m in _BULLET.finditer(text))
    tokens.extend(_BACKTICKED.findall(text))
    tokens.extend(_UNDERSCORED.findall(_EMPHASIS.sub("", text)))
    for line in _plain(text).splitlines():
        if not (any(c in line for c in candidates) or _DIR_WORDS.search(line)):
            continue
        body = _PARENS.sub(" ", line)
        for item in _LIST_SPLIT.split(body.rsplit(":", 1)[-1] if ":" in body else body):
            words = item.strip().split()
            if len(words) == 1:
                tokens.append(words[0])
    out = set()
    for t in tokens:
        if (name := _dir_like(t, exclude)) is not None and name not in candidates:
            out.add(name)
    return out


def names_claimed(
    answer: str, candidates: tuple[str, ...], exclude: tuple[str, ...] = ()
) -> frozenset[str] | None:
    """LC, LCn, LCp, LCi: the subdirectory names the answer gives. The candidates (the
    fixture's names) it mentions, plus any other name it lists as one (so that an invented
    fourth name fails); `exclude`: names of the system it may cite (the trial directory's path
    components, the sites). With no name at all, the empty set when it says there are none,
    else None (unreadable)."""
    text = _plain(answer)
    found = {c for c in candidates if re.search(rf"(?<![\w.]){re.escape(c)}(?![\w])", text)}
    skip = {e.lower() for e in exclude} | _system_names()
    found |= _extra_names(answer, candidates, skip)
    if found:
        return frozenset(found)
    return frozenset() if _NO_SUBDIRS.search(text) else None


_ANY_DATA_FILE = re.compile(r"(?<![\w-])[\w-]+\.(?:dat|txt|log|csv)\b")
_NO_LOGS = re.compile(
    r"\bno (?:log )?files?\b|\bnone\b|\bzero (?:log )?files\b|\bthere are no\b", re.I
)


def log_names_claimed(answer: str) -> frozenset[str] | None:
    """LCo: every data file name the answer gives (a path before it is ignored), including
    names that are not in the directory; the empty set when it says there are none; else None."""
    text = _plain(answer)
    found = frozenset(_ANY_DATA_FILE.findall(text))
    if found:
        return found
    return frozenset() if _NO_LOGS.search(text) else None


def _verify_listing(ctx: K2Context, trace: AgentTrace, got: frozenset[str] | None) -> OracleResult:
    spec = file_scenarios.FILE_SCENARIOS[ctx.scenario_id]
    baseline = _baseline(ctx)
    at = end_state(spec, baseline, _usable_tree(ctx.at_answer, "at the answer"))
    after = end_state(spec, baseline, _usable_tree(ctx.after_settle, "after the answer"))
    parts = []
    right = got == spec.expect
    if not right:
        shown = "unreadable" if got is None else sorted(got) or "none"
        parts.append(f"answers {shown}, not {sorted(spec.expect) or 'none'}")
    if at:
        ctx.flags.append("unrequested_change")
        parts.append("CHANGED THE DIRECTORY at the answer: " + "; ".join(at))
    elif after:
        parts.append("the directory changed after the answer: " + "; ".join(after))
    return _result(right and not at, not after, parts or ["right answer; directory unchanged"])


def system_names(ctx: K2Context) -> tuple[str, ...]:
    """Names an answer may cite that are not entries: the trial directory's path components
    and the two sites."""
    return (*[p for p in ctx.directory.split("/") if p], ctx.local_name, ctx.owner_name)


def verify_lc(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """LC, LCn, LCp, LCi (list_children): which subdirectories the directory has. Right answer:
    `spec.expect` (the empty set: none). The directory must be unchanged at the answer (the set
    offers create_directory and create_file) and after it."""
    got = ctx.decided(
        lambda: names_claimed(trace.final_answer, file_scenarios.LC_DIRS, system_names(ctx))
    )
    return _verify_listing(ctx, trace, got)


def verify_lco(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """LCo (list_children offered, not needed): every log file, subdirectories included. Right
    answer: the three names of `spec.expect`. The directory must be unchanged."""
    return _verify_listing(ctx, trace, ctx.decided(lambda: log_names_claimed(trace.final_answer)))


FILE_ORACLES.update(dict.fromkeys(("LC", "LCn", "LCp", "LCi"), verify_lc))
FILE_ORACLES["LCo"] = verify_lco


# --- CD: create_directory (design/15) ------------------------------------------------------

# Said first, it wins: "could not be created" is not a creation.
_NOT_CREATED = re.compile(
    r"\balready\b|\bis (?:a|an) (?:regular |existing |plain )?file\b"
    r"|\b(?:could|can|did|was|were|has|have|had)(?:n['’]t|\s+not)\s+(?:be\s+|been\s+)?"
    r"(?:create|made|make)|\bcannot\s+(?:be\s+)?(?:create|make)|\bnot created\b|\bunable\b"
    r"|\bfail(?:ed|s|ure)?\b|\bno directory was\b|\bname is (?:taken|in use)\b|\bconflict"
    r"|\bnot a directory\b",
    re.I,
)
_CREATED = re.compile(
    r"\b(?:created|made|done|now exists|exists now|succeeded|successfully|is in place)\b", re.I
)
# The reason CDn's right answer gives: a regular FILE has the name.
_FILE_IN_WAY = re.compile(
    r"\bregular file\b|\b(?:is|as|by|exists as)\s+(?:a|an)\s+(?:existing |plain )?file\b"
    r"|\b(?:a|an)\s+(?:existing\s+)?file\s+(?:named|called|with (?:that|the same) name|of that "
    r"name|is in the way|already)|\bnot a directory\b|\btaken by a file\b|\boccupied by a file\b",
    re.I,
)
# A wrong reason: a DIRECTORY of that name already exists.
_DIR_EXISTS = re.compile(
    r"\b(?:directory|folder|dir)\b(?:\s+[`'\"]?[\w./-]+[`'\"]?)?\s+(?:already\s+)?exists?\b"
    r"|\balready exists? as a (?:directory|folder)\b|\bis already a (?:directory|folder)\b"
    r"|\b(?:directory|folder)\b[^.\n]{0,40}\b(?:is )?already (?:there|present|in place)\b",
    re.I,
)

CREATED = "CREATED"
FILE_IN_WAY = "NOT_CREATED_FILE_IN_WAY"
DIR_EXISTS = "NOT_CREATED_DIR_EXISTS"
NOT_CREATED_OTHER = "NOT_CREATED_OTHER"


def cd_label(answer: str) -> str:
    """CD, CDn, CDo, CDp, as `rubrics.ANSWER_SPECS` defines the labels. When the answer says it
    could not or did not create, or that something is already there: NOT_CREATED_FILE_IN_WAY if
    it says a regular file has the name (CDn's right answer), NOT_CREATED_DIR_EXISTS if it says
    a directory of that name exists (a wrong reason), else NOT_CREATED_OTHER. Otherwise CREATED
    when it says created / made / done, else OTHER."""
    text = _plain(answer)
    if _NOT_CREATED.search(text):
        if _FILE_IN_WAY.search(text):
            return FILE_IN_WAY
        if _DIR_EXISTS.search(text):
            return DIR_EXISTS
        return NOT_CREATED_OTHER
    if _CREATED.search(text):
        return CREATED
    return OTHER


def verify_cd(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """CD, CDn, CDo, CDp (create_directory): make what the brief asks, or say why not. The
    answer must be `spec.expect`, AND the tree at the answer must be the expected end state (a
    "created" with the end state not there yet is a PREMATURE CLAIM). The federation axis: the
    same end state after the answer."""
    spec = file_scenarios.FILE_SCENARIOS[ctx.scenario_id]
    baseline = _baseline(ctx)
    at = end_state(spec, baseline, _usable_tree(ctx.at_answer, "at the answer"))
    after = end_state(spec, baseline, _usable_tree(ctx.after_settle, "after the answer"))
    got = ctx.decided(lambda: cd_label(trace.final_answer))
    parts = []
    if got != spec.expect:
        parts.append(f"answers {got}, not {spec.expect}")
    if at:
        if got == CREATED and not after:
            ctx.flags.append("premature_claim")
            parts.append("PREMATURE CLAIM: at the answer, " + "; ".join(at))
        else:
            parts.append("end state at the answer: " + "; ".join(at))
    if after:
        parts.append("end state after the answer: " + "; ".join(after))
    ok = got == spec.expect and not at
    return _result(ok, not after, parts or [f"answers {got}; the end state is right"])


FILE_ORACLES.update(dict.fromkeys(("CD", "CDn", "CDo", "CDp"), verify_cd))
