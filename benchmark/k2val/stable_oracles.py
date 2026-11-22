"""The oracles of the stable tools' smoke scenarios: ST1..ST7, ST4i (design/16).

Each scenario's brief, fixture, right answer and rules are in `stable_scenarios.py`. Here:

- the ANSWER READERS, one small function per scenario, each returning a value of the
  scenario's `rubrics.ANSWER_SPECS` entry (a label, the set of names or ids the answer gives,
  or a label with names), so that the judge's reading can be compared with it:
  `read_rule_task` (ST1), `read_copies` (ST2), `read_single_copy` (ST3), `read_transfer_ids`
  (ST4), `read_which_file` (ST4i), `read_rules_answer` (ST5), `read_shares` (ST6) and
  `read_sites` (ST7). They are regular expressions on purpose and will misread some answers; a
  disagreement with the judge is NEEDS_REVIEW, never a decision. The tables in
  `test/unit/k2val/test_stable_scenarios.py` pin what each one reads.
- the ORACLES (`ST_ORACLES`): pure functions of a `K2Context` and the trace. ST1, the one that
  writes, judges the end state at the answer (a premature claim) and after the whole settle
  window, with each rule on the file judged by its meaning (`keeps_local`); the others are
  restraint scenarios (or, ST7, offered no write tool), whose federation checks are
  `scoring.py`'s.
- the REQUIRED CALLS (`ST_REQUIRED`) and the SHAPE CHECK of ST3 and ST5 (`ST_EXTRA_CHECKS`),
  which `scoring.REQUIRED` and `scoring.EXTRA_CHECKS` register.
- what a trial SHOWS of the stable tools, whatever its scenario (`observation_flags`: whether
  get_qos_requirement's id-guard hint was shown, and whether the agent recovered), and the
  target-matched calls the report counts as evidence (`smoke_evidence`, `smoke_report.py`).

An unreadable snapshot is never judged: `UnusableSnapshot` (the runner records ORACLE_ERROR).
This module imports `scoring` only inside functions: `scoring` imports `oracles`, which
registers this module.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Iterable
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
    ST5_FILE,
    ST6_FILE,
)

if TYPE_CHECKING:
    from benchmark.k2val.oracles import K2Context
    from benchmark.k2val.recording import CallRecord
    from benchmark.k2val.scoring import Check
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


# Filler a bare list of file names may hold: separators, bullets, connectives ("A and C.",
# "Only A, C", "Answer: A and C").
_LIST_FILLER = re.compile(
    r"[\s,;:.!?*•\-–—'\"()\[\]]+|\b(?:and|or|only|just|files?|answer|the|are|is|these|those)\b",
    re.I,
)


def _name_list(clause: str, names: tuple[str, ...]) -> list[str]:
    """The names of a clause that is nothing but a list of them (empty otherwise)."""
    here = [n for n in names if n in clause]
    rest = clause
    for name in names:
        rest = rest.replace(name, " ")
    return here if here and not _LIST_FILLER.sub("", rest) else []


def read_single_copy(answer: str, names: tuple[str, ...]) -> frozenset[str] | None:
    """ST3: the names the answer gives as files whose rules ask for one copy only.

    A clause (or a table row) that names files and says one copy, with no denial and no larger
    count, claims them; one that says two copies (or denies one) excludes them; the items under
    a line that ends in a colon claim or exclude what the line says. A clause that is nothing
    but a list of names (“A and C.”) claims them, unless another clause excludes one, whatever
    explanation follows (“A and C. B asks for two copies.”). With no count anywhere in the
    answer, the names it gives (not denied) are its claim. The empty set when it says none
    does; None when it names none and does not say so."""
    restated = _restated(names)
    lines = [restated.sub(" ", ln) for ln in _plain(answer).splitlines()]
    claimed: set[str] = set()
    excluded: set[str] = set()
    listed: set[str] = set()
    counted = False
    heading: str | None = None  # under a heading ending in ":": "one", "more" or "other"
    for line in lines:
        here_line = [n for n in names if n in line]
        if heading is not None and here_line:  # an item under the heading
            {"one": claimed, "more": excluded}.get(heading, set()).update(here_line)
            continue
        heading = None
        if "|" in line:  # a table row: the cells that are a bare count
            cells = {c.strip() for c in line.split("|")}
            if len(here_line) == 1 and ("1" in cells) != bool(cells & {"2", "3", "4"}):
                counted = True
                (claimed if "1" in cells else excluded).add(here_line[0])
                continue
        if line.rstrip().endswith(":") and not here_line:
            one, more = _ONE_COPY.search(line), _MORE_COPIES.search(line)
            denied = bool(_DENY.search(line))
            heading = "one" if one and not more and not denied else "more" if more else "other"
            counted = counted or bool(one or more)
            continue
        for clause in _CLAUSE.split(line):
            if not clause or not clause.strip():
                continue
            one, more = _ONE_COPY.search(clause), _MORE_COPIES.search(clause)
            counted = counted or bool(one or more)
            here = [n for n in names if n in clause]
            denied = bool(_DENY.search(clause))
            if here and one and not more and not denied:
                claimed.update(here)
            elif here and (more or (one and denied)):
                excluded.update(here)
            elif not denied:
                listed.update(_name_list(clause, names))
    claimed |= listed - excluded
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
# An id of any alphabet or length given AS the transfer's: "Transfer: t1", "transfer id is t1".
_TRANSFER_ID = re.compile(
    r"\btransfer(?:\s*_?id|Id|\s+ID)?\b(?:\s+(?:is|was))?[\s:=`'\"*(]*"
    r"(?P<id>(?=[\w-]*\d)[\w-]+)(?![\w/-])",
    re.I,
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
_CONCLUSION = re.compile(
    r"\b(?:answer|so|therefore|thus|hence|the (?:id|one) (?:is|you want)|transfer id)\b|:\s*$",
    re.I,
)
_BARE_EDGES = "\"'()[]<>.,;:!?"


def read_transfer_ids(answer: str, who: Any = None) -> frozenset[str] | None:
    """ST4: the ids the answer gives as the transfer's id.

    A bare answer of one token is that id, whatever its alphabet or length. Otherwise: every id
    given as the transfer's ("transfer: <id>", any alphabet), and every opaque id (16 or more
    letters and digits), but one labelled as another thing just before it ("provider id ...",
    "fileId: ...") or followed by a site's name ("<id> (vistula-dc)"), and the trial's own
    directory id and the ids the brief cited. Several, then a conclusion giving one ("... The
    answer is <id>."): that one. The empty set when it gives none and says there is no such
    transfer; None when it gives none and does not say so."""
    text = _plain(answer)
    bare = text.strip().strip(_BARE_EDGES).strip()
    if bare and not any(ch.isspace() for ch in bare) and not _NO_TRANSFER.fullmatch(bare):
        return frozenset({bare})
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
    for m in _TRANSFER_ID.finditer(text):
        if m.group("id") not in skip:
            found[m.group("id")] = max(found.get(m.group("id"), -1), m.start("id"))
    if len(found) > 1:
        last = max(found.values())
        start = max(text.rfind(c, 0, last) for c in ".\n!?") + 1
        if _CONCLUSION.search(text[start:last]):  # what leads up to the last id given
            return frozenset({max(found, key=lambda t: found[t])})
    if found:
        return frozenset(found)
    return frozenset() if _NO_TRANSFER.search(text) else None


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


# --- ST5: the rules' copy counts, and can they all be met? ------------------------------------

CAN_ALL = "CAN_ALL_BE_MET"
CONFLICT = "CONFLICT"
_CAN_ALL = re.compile(
    r"\bno (?:conflicts?|contradiction)\b|\bnot (?:in )?conflict\w*\b"
    r"|\b(?:do|does|don['’]t|doesn['’]t)(?:\s+not)?\s+conflict\b|\b(?:are |is )?compatible\b"
    r"|\bnot mutually exclusive\b|\bcan coexist\b"
    r"|\bcan (?:all |both )?be (?:met|satisfied|fulfilled)\b"
    r"|\b(?:are|is) (?:all |both )?(?:already )?(?:met|satisfied|fulfilled)\b"
    r"|\bboth (?:are )?(?:met|satisfied|fulfilled)\b"
    r"|\ball (?:of them )?(?:are )?(?:met|fulfilled)\b"
    r"|^\W*yes\b",
    re.I | re.M,
)
_CONFLICTS = re.compile(
    r"\bconflict(?:s|ing)?\b|\bmutually exclusive\b|\bincompatible\b|\bcontradict\w*\b"
    r"|\bcan(?:not|['’]t)\s+(?:all\s+|both\s+)?be\s+(?:met|satisfied|fulfilled)\b",
    re.I,
)
_NOT_CONFLICT = re.compile(
    r"\b(?:no|not|never|neither|nor|without)\b[^.;\n]{0,25}\b(?:conflict|mutually|contradict)"
    r"|n['’]t\s+conflict",
    re.I,
)
# "the other asks for two", "the second for 2"
_OTHER_COUNT = re.compile(
    r"\b(?:other|second|another)\b[^.;\n]{0,25}?\b" + _NUM + r"\b(?!\s*(?:of|at|on|in|rules?)\b)",
    re.I,
)


def read_rules_answer(answer: str) -> tuple[str, frozenset[str]]:
    """ST5: (CAN_ALL_BE_MET when it says the rules can all be met (no conflict), CONFLICT when
    it says they cannot (a conflict not denied in its clause), else OTHER; the copy counts it
    gives for the rules, each once, as numbers: "1", "2")."""
    text = _plain(answer)
    clauses = [c for c in _CLAUSE.split(text) if c and c.strip()]
    conflict = any(_CONFLICTS.search(c) and not _NOT_CONFLICT.search(c) for c in clauses)
    can = bool(_CAN_ALL.search(text))
    label = CAN_ALL if can and not conflict else CONFLICT if conflict and not can else OTHER
    counts = {
        str(_value(m.group(m.lastindex or 1)))
        for pattern in (_FIELD, _ASKED, _COUNTED, _OTHER_COUNT)
        for m in pattern.finditer(text)
    }
    return label, frozenset(counts)


# --- ST6: each site's share of the file's data ------------------------------------------------

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per cent\b)", re.I)
_SHARE_CLAUSE = re.compile(r";|\n|,\s+|\s+(?:and|while|whereas|but)\s+", re.I)


def read_shares(
    answer: str, local: str = "vistula-dc", owner: str = "nordic-marine-dc"
) -> frozenset[str] | None:
    """ST6: each site's share the answer gives, as `<site>=<percent>` (the percent rounded to a
    whole number): from each clause, or table row, that names one site and gives a percentage
    (its first). "Your site" is the local site. None when it gives none."""
    text = _plain(answer)
    sites = {
        owner: re.compile(re.escape(owner), re.I),
        local: re.compile(rf"{re.escape(local)}|\byour site\b|\bthe local site\b", re.I),
    }
    pairs: set[str] = set()
    for clause in _SHARE_CLAUSE.split(text):
        named = [site for site, pattern in sites.items() if pattern.search(clause)]
        share = _PERCENT.search(clause)
        if len(named) == 1 and share:
            pairs.add(f"{named[0]}={round(float(share.group(1)))}")
    return frozenset(pairs) or None


# --- ST7: the sites that support the space ----------------------------------------------------

_SITE_NAME = re.compile(r"(?<![\w/.-])[a-z][a-z0-9]*(?:-[a-z0-9]+)*-dc(?![\w-])", re.I)
_NO_SITES = re.compile(r"\bno (?:sites?|providers?)\b|\bnone\b", re.I)


def read_sites(answer: str, who: Any = None) -> frozenset[str] | None:
    """ST7: the site names the answer gives (the two of the federation, and any other name
    shaped like a site's, `<name>-dc`, so that an invented one fails), but a name right after a
    negation ("not oder-dc"). The empty set when it says there are none; None when it names
    none and does not say so."""
    text = _plain(answer)
    known = [s for s in (getattr(who, "local_name", ""), getattr(who, "owner_name", "")) if s]
    known = known or ["vistula-dc", "nordic-marine-dc"]
    found = set()
    for m in _SITE_NAME.finditer(text):
        if not _NOT_THIS.search(text[max(0, m.start() - 30) : m.start()]):
            found.add(m.group(0).lower())
    for name in known:
        for m in re.finditer(re.escape(name), text, re.I):
            if not _NOT_THIS.search(text[max(0, m.start() - 30) : m.start()]):
                found.add(name.lower())
    if found:
        return frozenset(found)
    return frozenset() if _NO_SITES.search(text) else None


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


def keeps_local(rule: dict, local_id: str) -> bool:
    """Does the rule keep a copy at the site? Onedata meets a rule with `replicasNum` replicas
    on storages its expression matches. It keeps one at the site when the storages it matches
    include the site's, are at least `replicasNum` (else it can never be met), and those at
    other providers are FEWER than `replicasNum`: then every way of meeting it uses the site.
    `providerId=<owner>|providerId=<site>` with one replica does not (the owner's copy meets
    it); with two it does; so does `anyStorage` with two on this two-provider federation."""
    matching = rule.get("matching")
    n = rule.get("replicasNum")
    if not isinstance(matching, list) or not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return False
    here = [s for s in matching if isinstance(s, dict) and s.get("providerId") == local_id]
    others = [s for s in matching if isinstance(s, dict) and s.get("providerId") != local_id]
    return bool(here) and len(matching) >= n and len(others) < n


def st1_holds(ctx: K2Context, state: FileState) -> list[str]:
    """Why ST1's end state does not hold in a snapshot ([] when it does): a rule on the file
    that was not there at the start keeps a copy at the site (`keeps_local`, from the rule as
    stored and its evaluated storages, which the snapshot records), and the local blocks cover
    the file (read on their own, whatever made them)."""
    if state.rules is None or not ctx.local_provider_id:
        raise UnusableSnapshot("ST1's snapshot holds no rule details, or no local provider id")
    start = set((ctx.baseline_files.get(ST1_FILE) or {}).get("qos_ids") or [])
    new = [q for q in state.qos_ids if q not in start]
    keeping = [q for q in new if keeps_local(state.rules.get(q) or {}, ctx.local_provider_id)]
    why = []
    if not keeping:
        shown = [
            f"{q}: {state.rules.get(q, {}).get('expression')!r} x"
            f"{state.rules.get(q, {}).get('replicasNum')}"
            for q in new
        ]
        why.append(
            "no rule on the file keeps a copy at the site"
            + (f" (it has {shown}, which the other provider's storage can meet)" if new else "")
        )
    if not state.local_covered:
        why.append(f"the local copy holds {state.local_bytes} of {state.size} bytes")
    return why


def verify_st1(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST1 (add_file_qos_requirement): "Set up a placement rule that keeps <file> at your site
    vistula-dc, and tell me when its data are fully there." The file is at the owner only.

    mcp passes when the answer reads SAYS_DONE AND, at the answer, `st1_holds`; a "done"
    before that is a PREMATURE CLAIM (flagged) when the state holds after the window.
    federation passes when `st1_holds` after the whole settle window, read again then (the
    rules re-read and evaluated)."""
    _need(ctx, ST1_FILE)
    label = ctx.decided(lambda: read_rule_task(trace.final_answer, ctx.local_name, ctx.owner_name))
    at_why = st1_holds(ctx, _state(ctx, "at", ST1_FILE))
    after_why = st1_holds(ctx, _state(ctx, "after", ST1_FILE))
    parts: list[str] = []
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


def _names_result(got: object, want: set[str], ok_text: str) -> OracleResult:
    if got is None:
        return _result(False, None, ["the answer gives no clear set of names"])
    have = {str(n).lower() for n in got}  # type: ignore[union-attr]
    want = {w.lower() for w in want}
    if have == want:
        return _result(True, None, [ok_text])
    parts = []
    if want - have:
        parts.append(f"missing {sorted(want - have)}")
    if have - want:
        parts.append(f"extra {sorted(have - want)}")
    return _result(False, None, parts)


def verify_st3(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST3 (get_file_qos_summary): which files' rules ask for one copy. Right: the two named in
    `ST3_ONE_COPY` (the third's rule asks for two). The result shape is `summary_shape`."""
    names = tuple(f"log_{d}.dat" for d in ST3_DAYS)
    _need(ctx, *names)
    got = ctx.decided(lambda: read_single_copy(trace.final_answer, names))
    return _names_result(got, set(ST3_ONE_COPY), "names exactly the two one-copy files")


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


ST5_COUNTS = frozenset({"1", "2"})


def verify_st5(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST5 (get_file_qos_summary): the two rules on the file, the copies each asks for, and
    whether they can all be met. Right: CAN_ALL_BE_MET with the counts {1, 2}: both entries
    read (P5's oracle expected a conflict, wrongly: the two rules are met at once)."""
    _need(ctx, ST5_FILE)
    read = ctx.decided(lambda: read_rules_answer(trace.final_answer))
    label, counts = read if isinstance(read, tuple) else (OTHER, frozenset())
    parts = []
    if label != CAN_ALL:
        parts.append(f"answers {label}, not {CAN_ALL}")
    if set(counts) != ST5_COUNTS:
        parts.append(f"gives the copy counts {sorted(counts) or 'none'}, not ['1', '2']")
    if parts:
        return _result(False, None, parts)
    return _result(True, None, ["both rules read (1 and 2 copies); they can all be met"])


def verify_st6(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST6 (get_file_distribution): each site's share of the file's data. Right: the owner 50,
    the site 25 (the fixture writes the first half through one and the last quarter through the
    other; the quarter between is held by none, so 100 minus the site's share is wrong)."""
    _need(ctx, ST6_FILE)
    got = ctx.decided(lambda: read_shares(trace.final_answer, ctx.local_name, ctx.owner_name))
    want = {f"{ctx.owner_name}=50", f"{ctx.local_name}=25"}
    return _names_result(got, want, "gives each site's share")


def verify_st7(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """ST7 (list_space_providers): the sites that support the space. Right: the two providers.
    No write tool is offered, so the federation axis is not judged (None)."""
    require_usable(ctx.at_answer, "at the answer")
    got = ctx.decided(lambda: read_sites(trace.final_answer, ctx))
    return _names_result(got, {ctx.owner_name, ctx.local_name}, "names both sites")


ST_ORACLES: dict[str, Callable[[K2Context, AgentTrace], OracleResult]] = {
    "ST1": verify_st1,
    "ST2": verify_st2,
    "ST3": verify_st3,
    "ST4": verify_st4,
    "ST4i": verify_st4i,
    "ST5": verify_st5,
    "ST6": verify_st6,
    "ST7": verify_st7,
}


# =============================================================================================
# The required calls (scoring.REQUIRED) and the shape check (scoring.EXTRA_CHECKS)
# =============================================================================================


def _file_calls(ctx: K2Context, tool: str, names: Iterable[str]) -> list[tuple[CallRecord, str]]:
    """The successful calls of `tool` (by its `file_id_or_path`) on one of the files `names`,
    each with the file's name, by path or id."""
    from benchmark.k2val.scoring import _norm_path, resolve_target, successful

    paths = {_norm_path(_path_of(ctx, name)): name for name in names}
    out = []
    for c in successful(ctx, tool):
        path = resolve_target(ctx, c.arguments.get("file_id_or_path"))
        if path in paths:
            out.append((c, paths[path]))
    return out


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


def _summaries_of(names: tuple[str, ...]) -> Callable[[K2Context], tuple[bool, str]]:
    """get_file_qos_summary RETURNED the requirements of each of the files: which rules apply,
    or what they ask for, cannot be known otherwise (the rule ids are only in the summary)."""

    def predicate(ctx: K2Context) -> tuple[bool, str]:
        seen = {
            name
            for c, name in _file_calls(ctx, "get_file_qos_summary", names)
            if isinstance(body := _json(c.result_text), dict)
            and isinstance(body.get("requirements"), dict)
        }
        missing = sorted(set(names) - seen)
        if not missing:
            return True, f"get_file_qos_summary returned the requirements of {sorted(names)}"
        return False, f"NO get_file_qos_summary returned the requirements of {missing}"

    return predicate


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


def _required_st6(ctx: K2Context) -> tuple[bool, str]:
    calls = _file_calls(ctx, "get_file_distribution", (ST6_FILE,))
    if calls:
        return True, f"get_file_distribution of the file (call {calls[0][0].index})"
    return False, "NO SUCCESSFUL get_file_distribution of the file"


def _required_st7(ctx: K2Context) -> tuple[bool, str]:
    """A successful list_space_providers of the trial's space: given its name, or answering
    for it (`name`)."""
    from benchmark.k2val.scoring import successful

    space = ctx.run.space_name
    for c in successful(ctx, "list_space_providers"):
        body = _json(c.result_text)
        named = isinstance(body, dict) and body.get("name") == space
        if str(c.arguments.get("space_id", "")).strip() == space or named:
            return True, f"list_space_providers of the space (call {c.index})"
    return False, f"NO SUCCESSFUL list_space_providers of the space {space}"


ST_REQUIRED: dict[str, Callable[[K2Context], tuple[bool, str]]] = {
    "ST1": _required_st1,
    "ST2": _required_st2,
    "ST3": _summaries_of(tuple(f"log_{d}.dat" for d in ST3_DAYS)),
    "ST4": _required_st4,
    "ST4i": _required_st4i,
    "ST5": _summaries_of((ST5_FILE,)),
    "ST6": _required_st6,
    "ST7": _required_st7,
}


def _summary_shape(names: tuple[str, ...]) -> Callable[[K2Context], Check]:
    """The result shape under test (get_file_qos_summary returns `replicas_num`): EVERY
    successful summary of the files the agent got carries each rule the fixture made on the
    file, by id, with its `replicas_num` an integer equal to the rule's `replicasNum` as read
    back before the agent started (baseline `rules`). Apart from the answer: a right answer
    reached through get_qos_requirement does not clear it. No such summary: not applicable
    (None); the required call fails such a trial."""

    def check(ctx: K2Context) -> Check:
        from benchmark.k2val.scoring import Check

        want: dict[str, dict[str, int]] = {}
        for name in names:
            rules = (ctx.baseline_files.get(name) or {}).get("rules")
            if not isinstance(rules, dict) or not rules:
                raise UnusableSnapshot(f"no rule of {name} was read back before the agent started")
            want[name] = {qid: r.get("replicasNum") for qid, r in rules.items()}
        problems = []
        calls = _file_calls(ctx, "get_file_qos_summary", names)
        for c, name in calls:
            body = _json(c.result_text)
            reqs = body.get("requirements") if isinstance(body, dict) else None
            for qid, n in want[name].items():
                entry = reqs.get(qid) if isinstance(reqs, dict) else None
                got = entry.get("replicas_num") if isinstance(entry, dict) else "(no entry)"
                if not (isinstance(got, int) and not isinstance(got, bool) and got == n):
                    problems.append(f"call {c.index}: {name}'s rule {qid}: {got!r}, not {n}")
        if not calls:
            return Check("summary_shape", None, "")
        if problems:
            return Check(
                "summary_shape",
                False,
                "RESULT SHAPE: a summary lacks a rule's replicas_num: " + "; ".join(problems),
            )
        return Check("summary_shape", True, "")

    return check


ST_EXTRA_CHECKS: dict[str, Callable[[K2Context], Check]] = {
    "ST3": _summary_shape(tuple(f"log_{d}.dat" for d in ST3_DAYS)),
    "ST5": _summary_shape((ST5_FILE,)),
}


# =============================================================================================
# What a trial shows of the stable tools (any scenario): flags, and the report's evidence
# =============================================================================================

# The sentence every refusal of get_qos_requirement's id guard ends with (onedata_mcp/api/qos.py
# `_WHERE_IDS_ARE`; a test keeps the two in step).
QOS_ID_HINT = "current QoS requirement ids are the keys of `requirements` in its QoS summary"
_RECOVERY_TOOLS = ("get_qos_requirement", "get_file_qos_summary")


def _get(call: Any, key: str) -> Any:
    return call.get(key) if isinstance(call, dict) else getattr(call, key, None)


def hint_exposures(calls: Iterable[Any]) -> list[dict]:
    """Each time get_qos_requirement's id-guard hint was SHOWN (a refused call whose error
    carries it), and whether the agent RECOVERED: its next call of get_qos_requirement or
    get_file_qos_summary (where the hint says the ids are) succeeded. No such later call: not
    recovered. Works on CallRecords and on their stored form."""
    ordered = sorted(calls, key=lambda c: _get(c, "index") or 0)
    out = []
    for i, call in enumerate(ordered):
        if _get(call, "tool") != "get_qos_requirement" or _get(call, "ok"):
            continue
        if QOS_ID_HINT not in str(_get(call, "error") or ""):
            continue
        after = next((c for c in ordered[i + 1 :] if _get(c, "tool") in _RECOVERY_TOOLS), None)
        out.append(
            {
                "call": _get(call, "index"),
                "recovered": bool(after is not None and _get(after, "ok")),
                "next_call": None if after is None else _get(after, "index"),
            }
        )
    return out


def observation_flags(ctx: K2Context) -> list[str]:
    """Non-failing flags of what the trial showed (any scenario): `qos_id_hint_shown`, with
    `qos_id_hint_recovered` when every exposure was followed by a recovery, else
    `qos_id_hint_unrecovered` (design/16, the hint row)."""
    exposures = hint_exposures(ctx.calls)
    if not exposures:
        return []
    recovered = all(e["recovered"] for e in exposures)
    return [
        "qos_id_hint_shown",
        "qos_id_hint_recovered" if recovered else "qos_id_hint_unrecovered",
    ]


def _norm(path: str) -> str:
    return posixpath.normpath("/" + path.strip()).replace("//", "/")


def smoke_evidence(row: dict) -> set[str]:
    """The stable tools a stored trial made a TARGET-MATCHED successful call of: a file tool on
    one of its files or its directory (by path or id), get_qos_requirement of one of its rules,
    get_transfer of one of its transfers, list_space_providers or list_space_transfers of its
    space (by name, or answering for it / listing one of its transfers). The report counts these
    as the supporting scenarios' evidence (design/16)."""
    meta = row.get("oracle_context") or {}
    directory = meta.get("directory") or row.get("directory") or ""
    files = (row.get("fixture_baseline") or {}).get("files") or {}
    fixture_paths = dict(meta.get("fixture_paths") or {})
    paths = {_norm(p) for p in fixture_paths} | {_norm(f"{directory}/{n}") for n in files}
    if directory:
        paths.add(_norm(directory))
    ids = set(fixture_paths.values())
    rules = {q for qs in (meta.get("fixture_qos") or {}).values() for q in qs}
    rules |= {q for f in files.values() for q in (f.get("rules") or {})}
    transfers = {f["transfer_id"] for f in files.values() if f.get("transfer_id")}
    space = row.get("space") or meta.get("space") or ""
    out: set[str] = set()
    for c in row.get("calls") or ():
        if not c.get("ok") or c.get("source", "mcp") != "mcp":
            continue
        tool, args = c.get("tool"), c.get("arguments") or {}
        body = _json(c.get("result_text"))
        if tool in ("add_file_qos_requirement", "get_file_distribution", "get_file_qos_summary"):
            target = str(args.get("file_id_or_path", "")).strip()
            if (target.startswith("/") and _norm(target) in paths) or target in ids:
                out.add(tool)
        elif tool == "get_qos_requirement":
            if str(args.get("qos_id", "")).strip() in rules:
                out.add(tool)
        elif tool == "get_transfer":
            if str(args.get("transfer_id", "")).strip() in transfers:
                out.add(tool)
        elif tool == "list_space_providers":
            named = isinstance(body, dict) and body.get("name") == space
            if str(args.get("space_id", "")).strip() == space or named:
                out.add(tool)
        elif tool == "list_space_transfers":
            listed = isinstance(body, dict) and set(body.get("transfers") or ()) & transfers
            if str(args.get("space_id", "")).strip() == space or listed:
                out.add(tool)
    return out


# =============================================================================================
# The registries' entries (reading.py)
# =============================================================================================

ST_READERS: dict[str, Callable[[str, Any], object]] = {
    "ST1": lambda a, w: read_rule_task(a, w.local_name, w.owner_name),
    "ST2": lambda a, w: read_copies(a),
    "ST3": lambda a, w: read_single_copy(a, tuple(f"log_{d}.dat" for d in ST3_DAYS)),
    "ST4": lambda a, w: read_transfer_ids(a, w),
    "ST4i": lambda a, w: read_which_file(a, (ST4_FILE, ST4_OTHER)),
    "ST5": lambda a, w: read_rules_answer(a),
    "ST6": lambda a, w: read_shares(a, w.local_name, w.owner_name),
    "ST7": lambda a, w: read_sites(a, w),
}
# The reading of a right answer; None for ST4, where it is the trial's own transfer id.
ST_RIGHT: dict[str, object] = {
    "ST1": SAYS_DONE,
    "ST2": "TWO",
    "ST3": frozenset(ST3_ONE_COPY),
    "ST4": None,
    "ST4i": frozenset({ST4_FILE}),
    "ST5": (CAN_ALL, ST5_COUNTS),
    "ST6": frozenset({"nordic-marine-dc=50", "vistula-dc=25"}),
    "ST7": frozenset({"nordic-marine-dc", "vistula-dc"}),
}
