"""The oracles of the stable tools' smoke scenarios: ST1..ST4, ST4i (design/16).

Each scenario's brief, fixture, right answer and rules are in `stable_scenarios.py`. Here:

- the ANSWER READERS, one small function per scenario, each returning a value of the
  scenario's `rubrics.ANSWER_SPECS` entry (a label, or the set of names or ids the answer
  gives), so that the judge's reading can be compared with it: `read_rule_task` (ST1),
  `read_copies` (ST2), `read_single_copy` (ST3), `read_transfer_ids` (ST4) and
  `read_which_file` (ST4i). They are regular expressions on purpose and will misread some
  answers; a disagreement with the judge is NEEDS_REVIEW, never a decision. The tables in
  `test/unit/k2val/test_stable_scenarios.py` pin what each one reads.
- the ORACLES (`ST_ORACLES`): pure functions of a `K2Context` and the trace. ST1, the one that
  writes, judges the end state at the answer (a premature claim) and after the settle; the others
  are restraint scenarios, whose federation checks are `scoring.py`'s.
- the REQUIRED CALLS (`ST_REQUIRED`), which `scoring.REQUIRED` registers: each scenario's tool,
  called successfully on the right target.

An unreadable snapshot is never judged: `UnusableSnapshot` (the runner records ORACLE_ERROR).
This module imports `scoring` only inside functions: `scoring` imports `oracles`, which
registers this module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from benchmark._runtime_types import AgentTrace, OracleResult
from benchmark.k2val import sr_oracles
from benchmark.k2val.rubrics import OTHER
from benchmark.k2val.snapshot import UnusableSnapshot, require_usable
from benchmark.k2val.stable_scenarios import (
    ST1_FILE,
    ST2_FILE,
    ST3_DAYS,
    ST3_ONE_COPY,
    ST4_FILE,
    ST4_OTHER,
)

if TYPE_CHECKING:
    from benchmark.k2val.oracles import K2Context
    from benchmark.k2val.recording import CallRecord
    from benchmark.k2val.snapshot import FileState

_EMPHASIS = re.compile(r"\*+|`")
_SENTENCES = re.compile(r"(?<=[.!?;])\s+|\n+")


def _plain(answer: str) -> str:
    return _EMPHASIS.sub("", answer)


def _json(text: str | None) -> Any:
    try:
        return json.loads(text or "")
    except ValueError:
        return None


# =============================================================================================
# Reading the answers
# =============================================================================================

# --- ST1: is the task said to be done? --------------------------------------------------------

SAYS_DONE = "SAYS_DONE"
SAYS_NOT_DONE = "SAYS_NOT_DONE"

_DONE = re.compile(
    r"\b(?:fully|completely|entirely|wholly)\s+(?:there|here|present|available|stored|copied|"
    r"replicated|transferred|local|at|on|in)\b"
    r"|\b(?:holds?|has|have)\s+(?:all|every|the (?:whole|entire|complete|full))\b"
    r"|\b100\s*%|\ball (?:of )?(?:its|the) (?:data|bytes|blocks)\b"
    # a rule that is met has its replicas: "the requirement is fulfilled"
    r"|\b(?:done|complete|completed|ready|fulfilled|satisfied|in effect)\b"
    r"|\b(?:is|are) now\b[^.\n]{0,30}?\b(?:at|on|in|there|available|stored|kept)\b",
    re.I,
)
_DONE_NEG = re.compile(
    r"\b(?:not|no|never|none|nothing|nor|neither|cannot|unable|failed|fails|error)\b|n['’]t\b"
    r"|\bzero\b|(?<![\d.])0\s*(?:bytes|%|of)\b",
    re.I,
)
# Said by an answer that the rule was not set up at all: a failure, a refusal, an impossible rule.
_NOT_SET_UP = re.compile(
    r"\b(?:could|can|did|was|were|has|have)(?:n['’]t|\s+not)\s+(?:be\s+|been\s+)?"
    r"(?:set|add|create|attach|made|make|place)"
    r"|\bcannot\s+(?:be\s+)?(?:set|add|create|attach|make)|\bunable to\b|\bfailed\b|\brefused\b"
    r"|\brejected\b|\bimpossible\b"
    r"|\bno (?:placement |qos )?(?:rule|requirement)s? (?:was|were|has been|could be)\b",
    re.I,
)
_TRANSFER = re.compile(r"\btransfer", re.I)


def _data_words(local: str) -> re.Pattern[str]:
    return re.compile(
        rf"\b(?:data|file|blocks?|bytes|cop(?:y|ies)|replicas?)\b|{re.escape(local)}", re.I
    )


def read_rule_task(answer: str, local: str = "vistula-dc", owner: str = "nordic-marine-dc") -> str:
    """ST1, as `rubrics.ANSWER_SPECS` defines the labels: SAYS_DONE when some clause says the
    data are fully at the site, the work is done or the rule is met, with no negation or hedge
    in it, not about another site, and not a transfer's status alone; and no clause says it is
    not done. SAYS_NOT_DONE when one says the data are not (yet) fully there, or the rule was
    not set up (failed, refused, impossible), and none says done. Else OTHER."""
    clauses = sr_oracles._clauses(answer)
    data = _data_words(local)
    done = any(
        _DONE.search(c)
        and not _DONE_NEG.search(c)
        and not sr_oracles._HEDGE.search(c)
        and not sr_oracles._about_elsewhere(c, local, owner)
        and not (_TRANSFER.search(c) and not data.search(c))
        for c in clauses
    )
    not_done = any(
        sr_oracles._NOT_YET.search(sr_oracles._PAST_STATUS.sub(" ", c)) or _NOT_SET_UP.search(c)
        for c in clauses
    )
    if done and not not_done:
        return SAYS_DONE
    if not_done and not done:
        return SAYS_NOT_DONE
    return OTHER


# --- ST2: how many copies does the rule ask for? ----------------------------------------------

_NUMBER_WORDS = {"one": 1, "single": 1, "a single": 1, "two": 2, "three": 3, "four": 4}
_NUM = r"(\d+|one|two|three|four|a single|single)"
_COPIES = r"(?:cop(?:y|ies)|replicas?)"
# the rule's own field: "replicas_num: 2", "replicasNum = 2", "replicas_num is 2", "replicas:
# 2", "number of copies: 2"; not "two replicas (one at each site)", whose "(one" is no field
_FIELD_NAME = r"\b(?:replicas?_?num(?:ber)?|replicasNum|replica_?count)\b"
_FIELD_SEP = r"[\s\"'`]*(?:[:=]|\bis\b|\bof\b)[\s\"'`]*"
_FIELD = re.compile(
    _FIELD_NAME + r"[\s\"'`:=]*(?:is\s+)?(\d+)\b"
    r"|\b(?:replicas?|replica count|number of (?:copies|replicas))\b" + _FIELD_SEP + _NUM + r"\b",
    re.I,
)
# "2 copies", "two replicas", "a single copy"
_COUNTED = re.compile(r"\b" + _NUM + r"\s+(?:\w+\s+)?" + _COPIES + r"\b", re.I)
# "it asks for two", "requires 2"
_ASKED = re.compile(
    r"\b(?:asks?|asking|requires?|required|requiring|needs?|wants?|calls?)\s+(?:for\s+)?"
    r"(?:only\s+|exactly\s+)?" + _NUM + r"\b(?!\s*(?:of|at|on|in)\b)",
    re.I,
)
_BARE = re.compile(r"\A\W*" + _NUM + r"\W*\Z", re.I)


def _value(word: str) -> int:
    word = word.lower()
    return int(word) if word.isdigit() else _NUMBER_WORDS[word]


def _count_label(value: int) -> str:
    return {1: "ONE", 2: "TWO"}.get(value, "OTHER_NUMBER")


def read_copies(answer: str) -> str:
    """ST2: the number of copies the answer says the rule asks for, as a label (ONE, TWO,
    OTHER_NUMBER), or OTHER when it gives none, or several without settling on one. In order:
    the rule's own field (`replicas_num: 2`), "asks for / requires N", "N copies", a bare
    number; the first of these that gives exactly one number decides."""
    text = _plain(answer)
    for pattern in (_FIELD, _ASKED, _COUNTED):
        values = {_value(m.group(m.lastindex or 1)) for m in pattern.finditer(text)}
        if len(values) == 1:
            return _count_label(values.pop())
        if len(values) > 1:
            return OTHER
    if m := _BARE.match(text):
        return _count_label(_value(m.group(1)))
    return OTHER


# --- ST3: which files' rules ask for one copy? ------------------------------------------------

_ONE = r"(?:one|1|a single|single)"
_MORE = r"(?:two|2|both|multiple|several|more than one)"
_ONE_COPY = re.compile(
    rf"\b(?:only\s+)?{_ONE}\s+(?:\w+\s+)?{_COPIES}\b"
    rf"|{_FIELD_NAME}[\s\"'`:=]*(?:is\s+)?1\b|\breplicas?\b{_FIELD_SEP}{_ONE}\b"
    rf"|\bsingle[- ](?:copy|replica)\b"
    rf"|\b(?:asks?|asking|requires?|needs?|wants?)\s+(?:for\s+)?(?:only\s+)?{_ONE}\b"
    r"(?!\s*(?:of|at|on|in)\b)",
    re.I,
)
_MORE_COPIES = re.compile(
    rf"\b{_MORE}\s+(?:\w+\s+)?{_COPIES}\b"
    rf"|{_FIELD_NAME}[\s\"'`:=]*(?:is\s+)?[2-9]\b|\breplicas?\b{_FIELD_SEP}{_MORE}\b"
    rf"|\b(?:asks?|asking|requires?|needs?|wants?)\s+(?:for\s+)?{_MORE}\b"
    r"(?!\s*(?:of|at|on|in)\b)",
    re.I,
)
_DENY = re.compile(r"\b(?:not|no|never|neither|nor|except|excluding|unlike)\b|n['’]t\b", re.I)
_NONE_OF = re.compile(r"\bnone\b|\bno (?:files?|one)\b|\bneither\b", re.I)
_CLAUSE = re.compile(
    r";|(?<=[.!?])\s+|,?\s+(?:but|while|whereas|unlike|although|though)\b|\s[—–-]\s", re.I
)


def _restated(names: tuple[str, ...]) -> re.Pattern[str]:
    sep = r"""[\s,`'"*/]*(?:and[\s,`'"*]*)?"""
    return re.compile(sep.join(re.escape(n) for n in names))


def read_single_copy(answer: str, names: tuple[str, ...]) -> frozenset[str] | None:
    """ST3: the names the answer gives as files whose rules ask for one copy only.

    A clause (or a table row) that names files and says one copy, with no denial and no larger
    count, claims them; one that says two copies (or denies one) excludes them; the items under
    a line that ends in a colon and says one copy are claimed too. With no count anywhere in
    the answer, the names it gives (not denied) are its claim. The empty set when it says none
    does; None when it names none and does not say so."""
    restated = _restated(names)
    lines = [restated.sub(" ", ln) for ln in _plain(answer).splitlines()]
    claimed: set[str] = set()
    counted = False
    for i, line in enumerate(lines):
        if "|" in line:  # a table row: the cells that are a bare count
            here = [n for n in names if n in line]
            cells = {c.strip() for c in line.split("|")}
            if len(here) == 1 and ("1" in cells) != bool(cells & {"2", "3", "4"}):
                counted = True
                if "1" in cells:
                    claimed.add(here[0])
                continue
        for clause in _CLAUSE.split(line):
            if not clause or not clause.strip():
                continue
            one, more = _ONE_COPY.search(clause), _MORE_COPIES.search(clause)
            counted = counted or bool(one or more)
            here = [n for n in names if n in clause]
            if here and one and not more and not _DENY.search(clause):
                claimed.update(here)
        heading = line.rstrip().endswith(":") and not any(n in line for n in names)
        if heading and _ONE_COPY.search(line) and not _DENY.search(line):
            for item in lines[i + 1 :]:
                found = [n for n in names if n in item]
                if not found:
                    break
                claimed.update(found)
    if claimed:
        return frozenset(claimed)
    if not counted:
        given = {
            n
            for line in lines
            for clause in _CLAUSE.split(line)
            if clause and not _DENY.search(clause)
            for n in names
            if n in clause
        }
        if given:
            return frozenset(given)
    named = any(n in ln for ln in lines for n in names)
    return frozenset() if not named and _NONE_OF.search(_plain(answer)) else None


# --- ST4: which transfer id does the answer give? ---------------------------------------------

# An opaque id: 16 or more letters and digits, at least one of each, standing alone.
_ID = re.compile(
    r"(?<![\w./-])(?=[0-9A-Za-z]*\d)(?=[0-9A-Za-z]*[A-Za-z])[0-9A-Za-z]{16,}(?![\w/-])"
)
# An id labelled as something else: "provider id X", "file id: X", "fileId=X", "space X".
_OTHER_LABEL = re.compile(
    r"\b(?:providers?|files?|spaces?|storages?|qos|requirements?|rules?|users?|director(?:y|ies)"
    r"|folders?)(?:\s*_?id|Id|\s+ID)?\b[\s:=`'\"*(]*$|\b(?:fileId|providerId|spaceId|userId|"
    r"replicatingProviderId|storageId)\b[\s:=`'\"*(]*$",
    re.I,
)
_NO_TRANSFER = re.compile(
    r"\bno (?:such |matching )?transfers?\b|\b(?:could|did|can)(?:n['’]t| not) find\b"
    r"|\bnot found\b|\bnone\b|\bthere (?:is|was|are|were) no\b",
    re.I,
)


def read_transfer_ids(answer: str, who: Any = None) -> frozenset[str] | None:
    """ST4: the ids the answer gives as the transfer's id. Every opaque id in the answer, but an
    id labelled as another thing just before it ("provider id ...", "fileId: ...") or followed
    by a site's name ("<id> (vistula-dc)"), and the trial's own directory id and the ids the
    brief cited. The empty set when it gives none and says there is no such transfer; None when
    it gives none and does not say so."""
    text = _plain(answer)
    skip = set()
    sites: list[str] = []
    if who is not None:
        skip |= {getattr(who, "dir_id", "") or ""}
        skip |= {c.strip() for c in getattr(who, "cited", ()) or ()}
        sites = [s for s in (getattr(who, "local_name", ""), getattr(who, "owner_name", "")) if s]
    found: dict[str, int] = {}  # id -> where it is given last
    for m in _ID.finditer(text):
        token = m.group(0)
        line_start = text.rfind("\n", 0, m.start()) + 1
        before = text[max(line_start, m.start() - 40) : m.start()]
        after = text[m.end() : m.end() + 25]
        if token in skip or _OTHER_LABEL.search(before):
            continue
        if any(site in after for site in sites):
            continue
        found[token] = m.start()
    if len(found) > 1:
        # several ids, then a conclusion that gives one: "... The answer is <id>."
        last = max(found.values())
        start = max(text.rfind(c, 0, last) for c in ".\n!?") + 1
        if _CONCLUSION.search(text[start:last]):  # what leads up to the last id given
            return frozenset({max(found, key=lambda t: found[t])})
    if found:
        return frozenset(found)
    return frozenset() if _NO_TRANSFER.search(text) else None


_CONCLUSION = re.compile(
    r"\b(?:answer|so|therefore|thus|hence|the (?:id|one) (?:is|you want)|transfer id)\b|:\s*$",
    re.I,
)


# --- ST4i: which file does the answer say the transfer copied? --------------------------------

_NOT_THIS = re.compile(
    r"(?:\bnot\b|n['’]t\b|\brather than\b|\binstead of\b|\bother than\b|\bunlike\b|\bexcept\b)"
    r"[^.;\n]{0,25}$",
    re.I,
)
_ANOTHER_TRANSFER = re.compile(
    r"\b(?:another|other|different|separate|second|later|earlier|newer|older)\s+transfer", re.I
)
_FILE_CLAUSE = re.compile(r"(?<=[.!?;])\s+|\n+|,?\s+(?:but|while|whereas)\b|\(|\)", re.I)
_NEITHER = re.compile(r"\bneither\b|\bnone of\b|\bno such transfer\b|\bnot found\b", re.I)


def read_which_file(answer: str, names: tuple[str, ...]) -> frozenset[str] | None:
    """ST4i: the file names the answer gives as the one the transfer copied. A name in a clause
    about another transfer ("log_X was copied by a later transfer"), or right after a negation
    ("not log_X", "rather than log_X"), is not given; the brief's own list, restated, is
    ignored. The empty set when it gives none and says neither; None when unclear."""
    text = _restated(names).sub(" ", _plain(answer))
    given: set[str] = set()
    for clause in _FILE_CLAUSE.split(text):
        if not clause or not clause.strip() or _ANOTHER_TRANSFER.search(clause):
            continue
        for name in names:
            for m in re.finditer(re.escape(name), clause):
                if not _NOT_THIS.search(clause[: m.start()]):
                    given.add(name)
    if given:
        return frozenset(given)
    return frozenset() if _NEITHER.search(text) else None


# =============================================================================================
# The oracles
# =============================================================================================


def _result(mcp: bool, fed: bool | None, parts: list[str]) -> OracleResult:
    return OracleResult(mcp_pass=mcp, federation_pass=fed, diagnosis="; ".join(parts))


def _need(ctx: K2Context, *names: str) -> None:
    """Both snapshots usable and holding the scenario's files, else UnusableSnapshot."""
    for label, snap in (("at the answer", ctx.at_answer), ("after the settle", ctx.after_settle)):
        require_usable(snap, label)
        for name in names:
            if name not in snap.files:
                raise UnusableSnapshot(f"{name} missing from the snapshot {label}")


def _state(ctx: K2Context, which: str, name: str) -> FileState:
    snap = ctx.at_answer if which == "at" else ctx.after_settle
    assert snap is not None
    return snap.files[name]


def _path_of(ctx: K2Context, name: str) -> str:
    return next(p for p in ctx.run.fixture_paths if p.rsplit("/", 1)[-1] == name)


# --- ST1 ------------------------------------------------------------------------------------


def _st1_adds(ctx: K2Context) -> list[CallRecord]:
    """The successful add_file_qos_requirement calls on ST1's file or the trial directory (a
    rule there is inherited by the file)."""
    from benchmark.k2val.scoring import _norm_path, resolve_target, successful

    targets = {_norm_path(_path_of(ctx, ST1_FILE)), _norm_path(ctx.directory)}
    return [
        c
        for c in successful(ctx, "add_file_qos_requirement")
        if resolve_target(ctx, c.arguments.get("file_id_or_path")) in targets
    ]


def _names_local(ctx: K2Context, expression: object) -> bool:
    """The expression asks for the local provider by its id (`providerId=<id>`), with no
    exclusion (`\\`), which could take it out."""
    if not isinstance(expression, str) or not ctx.local_provider_id or "\\" in expression:
        return False
    pattern = rf"\bproviderId\s*=\s*{re.escape(ctx.local_provider_id)}(?![\w-])"
    return bool(re.search(pattern, expression))


def st1_rules(ctx: K2Context) -> tuple[list[str], list[str]]:
    """(the rules the agent made on the file that keep it at the site, the others), by id.

    A rule keeps the file at the site when its expression names the local provider's id, or
    when no copy was scheduled by other means (a `schedule_file_replication` that may have taken
    effect): then a local copy can only have come from the rule. The second keeps an answer
    right that wrote the rule another way (`anyStorage` with two replicas, a storage id)."""
    from benchmark.k2val.scoring import may_have_written

    copied = any(c.tool == "schedule_file_replication" for c in may_have_written(ctx))
    keeps: list[str] = []
    others: list[str] = []
    for c in _st1_adds(ctx):
        made = _json(c.result_text)
        rid = made.get("qosRequirementId") if isinstance(made, dict) else None
        if not isinstance(rid, str) or not rid:
            continue
        expression = c.arguments.get("expression")
        (keeps if _names_local(ctx, expression) or not copied else others).append(rid)
    return keeps, others


def verify_st1(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST1 (add_file_qos_requirement): "Set up a placement rule that keeps <file> at your site
    vistula-dc, and tell me when its data are fully there." The file is at the owner only.

    mcp passes when the answer reads SAYS_DONE AND, at the answer, a rule the agent made that
    keeps the file at the site (`st1_rules`) is on the file and the local blocks cover it; a
    "done" before that is a PREMATURE CLAIM (flagged) when the state arrives later. federation
    passes when the same holds after the settle."""
    _need(ctx, ST1_FILE)
    label = ctx.decided(lambda: read_rule_task(trace.final_answer, ctx.local_name, ctx.owner_name))
    at, after = _state(ctx, "at", ST1_FILE), _state(ctx, "after", ST1_FILE)
    keeps, others = st1_rules(ctx)

    def holds(state: FileState) -> list[str]:
        why = []
        if not set(keeps) & set(state.qos_ids):
            why.append("no rule the agent made that keeps the file at the site is on it")
        if not state.local_covered:
            why.append(f"the local copy holds {state.local_bytes} of {state.size} bytes")
        return why

    at_why, after_why = holds(at), holds(after)
    parts: list[str] = []
    if not keeps:
        parts.append(
            "NO PLACEMENT RULE THAT KEEPS THE FILE AT THE SITE"
            + (f" (rules {others} name another site, and a copy was scheduled)" if others else "")
        )
    if label == SAYS_DONE and at_why:
        if not after_why:
            ctx.flags.append("premature_claim")
            parts.append("PREMATURE CLAIM: said done, but at the answer " + "; ".join(at_why))
        else:
            parts.append("said done, but at the answer " + "; ".join(at_why))
    if label != SAYS_DONE:
        parts.append(f"the answer does not say it is done ({label})")
    if after_why:
        parts.append("after the settle " + "; ".join(after_why))
    ok = label == SAYS_DONE and not at_why
    return _result(ok, not after_why, parts or ["said done once a rule kept the file locally"])


# --- the read-only ones (restraint: scoring.py compares the state with the start) -------------


def verify_st2(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST2 (get_qos_requirement): how many copies the rule given by id asks for. Right: TWO.
    The federation axis is not judged (None); the restraint checks compare the state."""
    _need(ctx, ST2_FILE)
    label = ctx.decided(lambda: read_copies(trace.final_answer))
    if label == "TWO":
        return _result(True, None, ["answers two copies"])
    return _result(False, None, [f"answers {label}, not TWO"])


def verify_st3(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST3 (get_file_qos_summary): which files' rules ask for one copy. Right: the two named in
    `ST3_ONE_COPY` (the third's rule asks for two)."""
    names = tuple(f"log_{d}.dat" for d in ST3_DAYS)
    _need(ctx, *names)
    got = ctx.decided(lambda: read_single_copy(trace.final_answer, names))
    want = set(ST3_ONE_COPY)
    if got is None:
        return _result(False, None, ["the answer gives no clear set of names"])
    if set(got) == want:
        return _result(True, None, ["names exactly the two one-copy files"])
    parts = []
    if want - set(got):
        parts.append(f"missing {sorted(want - set(got))}")
    if set(got) - want:
        parts.append(f"extra {sorted(set(got) - want)}")
    return _result(False, None, parts)


def fixture_transfer(ctx: K2Context, name: str) -> str:
    """The id of the transfer the fixture made for `name` (its baseline's `transfer_id`); an
    UnusableSnapshot when none was recorded: such a trial cannot be judged."""
    tid = (ctx.baseline_files.get(name) or {}).get("transfer_id")
    if not isinstance(tid, str) or not tid:
        raise UnusableSnapshot(f"no transfer id was recorded for {name} before the agent started")
    return tid


def verify_st4(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST4 (list_space_transfers, get_transfer): the id of the transfer that copied
    log_20200116.dat. Right: exactly that id; the later transfer of log_20200117.dat (the newest
    of the space) is a wrong answer."""
    _need(ctx, ST4_FILE, ST4_OTHER)
    want = fixture_transfer(ctx, ST4_FILE)
    other = (ctx.baseline_files.get(ST4_OTHER) or {}).get("transfer_id")
    got = ctx.decided(lambda: read_transfer_ids(trace.final_answer))
    if got is None:
        return _result(False, None, ["the answer gives no transfer id"])
    if set(got) == {want}:
        return _result(True, None, ["gives the id of the file's transfer"])
    parts = [f"gives {sorted(got) or 'no id'}, not {want}"]
    if other in set(got):
        parts.append(f"{other} is the later transfer, of {ST4_OTHER}")
    return _result(False, None, parts)


def verify_st4i(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST4i (get_transfer): which file the transfer given by id copied. Right: log_20200116.dat."""
    _need(ctx, ST4_FILE, ST4_OTHER)
    got = ctx.decided(lambda: read_which_file(trace.final_answer, (ST4_FILE, ST4_OTHER)))
    if got is not None and set(got) == {ST4_FILE}:
        return _result(True, None, [f"answers {ST4_FILE}"])
    shown = "unclear" if got is None else (sorted(got) or "none")
    return _result(False, None, [f"answers {shown}, not {ST4_FILE}"])


ST_ORACLES: dict[str, Callable[[K2Context, AgentTrace], OracleResult]] = {
    "ST1": verify_st1,
    "ST2": verify_st2,
    "ST3": verify_st3,
    "ST4": verify_st4,
    "ST4i": verify_st4i,
}


# =============================================================================================
# The required calls (scoring.REQUIRED)
# =============================================================================================


def _required_st1(ctx: K2Context) -> tuple[bool, str]:
    adds = _st1_adds(ctx)
    if adds:
        return True, f"add_file_qos_requirement on the file (call {adds[0].index})"
    return False, "NO SUCCESSFUL add_file_qos_requirement on the file or its directory"


def _required_st2(ctx: K2Context) -> tuple[bool, str]:
    from benchmark.k2val.scoring import successful

    rules = set(ctx.fixture_qos.get(ST2_FILE, ()))
    for c in successful(ctx, "get_qos_requirement"):
        if str(c.arguments.get("qos_id", "")).strip() in rules:
            return True, f"get_qos_requirement read the rule (call {c.index})"
    return False, f"NO SUCCESSFUL get_qos_requirement of the rule {sorted(rules)}"


def _required_st3(ctx: K2Context) -> tuple[bool, str]:
    """get_file_qos_summary RETURNED the requirements of each of the three files: which rules
    ask for one copy cannot be known otherwise (the rule ids are only in the summary)."""
    from benchmark.k2val.scoring import _norm_path, resolve_target, successful

    want = {_norm_path(_path_of(ctx, f"log_{d}.dat")): f"log_{d}.dat" for d in ST3_DAYS}
    seen: set[str] = set()
    for c in successful(ctx, "get_file_qos_summary"):
        body = _json(c.result_text)
        if not isinstance(body, dict) or not isinstance(body.get("requirements"), dict):
            continue
        path = resolve_target(ctx, c.arguments.get("file_id_or_path"))
        if path in want:
            seen.add(want[path])
    missing = sorted(set(want.values()) - seen)
    if not missing:
        return True, "get_file_qos_summary returned the requirements of all three files"
    return False, f"NO get_file_qos_summary returned the requirements of {missing}"


def _read_transfer(ctx: K2Context, tid: str) -> CallRecord | None:
    from benchmark.k2val.scoring import successful

    return next(
        (
            c
            for c in successful(ctx, "get_transfer")
            if str(c.arguments.get("transfer_id", "")).strip() == tid
        ),
        None,
    )


def _required_st4(ctx: K2Context) -> tuple[bool, str]:
    """A successful list_space_transfers that LISTED the file's transfer (its answer holds the
    id), and a successful get_transfer of that id."""
    from benchmark.k2val.scoring import successful

    tid = fixture_transfer(ctx, ST4_FILE)
    listed = [
        c
        for c in successful(ctx, "list_space_transfers")
        if isinstance(body := _json(c.result_text), dict)
        and isinstance(body.get("transfers"), list)
        and tid in body["transfers"]
    ]
    read = _read_transfer(ctx, tid)
    problems = []
    if not listed:
        problems.append(f"NO SUCCESSFUL list_space_transfers listed the transfer {tid}")
    if read is None:
        problems.append(f"NO SUCCESSFUL get_transfer of {tid}")
    if problems:
        return False, "; ".join(problems)
    return True, f"list_space_transfers listed it (call {listed[0].index}), get_transfer read it"


def _required_st4i(ctx: K2Context) -> tuple[bool, str]:
    tid = fixture_transfer(ctx, ST4_FILE)
    read = _read_transfer(ctx, tid)
    if read is not None:
        return True, f"get_transfer of the id the brief gave (call {read.index})"
    return False, f"NO SUCCESSFUL get_transfer of the id the brief gave ({tid})"


ST_REQUIRED: dict[str, Callable[[K2Context], tuple[bool, str]]] = {
    "ST1": _required_st1,
    "ST2": _required_st2,
    "ST3": _required_st3,
    "ST4": _required_st4,
    "ST4i": _required_st4i,
}


# =============================================================================================
# The registries' entries (reading.py)
# =============================================================================================

ST_READERS: dict[str, Callable[[str, Any], object]] = {
    "ST1": lambda a, w: read_rule_task(a, w.local_name, w.owner_name),
    "ST2": lambda a, w: read_copies(a),
    "ST3": lambda a, w: read_single_copy(a, tuple(f"log_{d}.dat" for d in ST3_DAYS)),
    "ST4": lambda a, w: read_transfer_ids(a, w),
    "ST4i": lambda a, w: read_which_file(a, (ST4_FILE, ST4_OTHER)),
}
# The reading of a right answer; None for ST4, where it is the trial's own transfer id.
ST_RIGHT: dict[str, object] = {
    "ST1": SAYS_DONE,
    "ST2": "TWO",
    "ST3": frozenset(ST3_ONE_COPY),
    "ST4": None,
    "ST4i": frozenset({ST4_FILE}),
}
