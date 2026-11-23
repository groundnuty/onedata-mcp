"""Two-axis oracles of the validation panel, in the style of `benchmark/oracles/`.

`mcp_pass`: the answer is right, judged by string and regex rules on the final answer, as in
PPAM. For the scenarios that ask for an end state (V2, V6, V7) a "done" is also checked against
the federation at the moment the answer was given (the snapshot taken right after the final
turn): a "done" with the data not yet there is a FAIL, tagged PREMATURE CLAIM.
`federation_pass`: the end state is right, read after a bounded settle; None where nothing
should change (V1, V4, V5).

The oracles are pure functions of a `K2Context` (snapshots, the writer's record, the recorded
calls) and the agent's trace: they make no REST call. Rules the regexes cannot settle are left
to the manual three-layer classification; the full answer is always in the artefact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.rubrics import UNREAD
from benchmark.k2val.scenarios import S2_DAYS
from benchmark.k2val.snapshot import Snapshot, require_usable
from benchmark.oracles._helpers import _EXCLUSION_SECTION_HEADERS, _is_strong_header

FILE_RE = re.compile(r"log_(\d{8})\.dat")
V1_EXPECTED = ("log_20200102.dat", "log_20200105.dat")
S2_NAMES = tuple(f"log_{d}.dat" for d in S2_DAYS)


@dataclass
class K2Context:
    scenario_id: str
    run: RunContext
    local_name: str
    owner_name: str
    file_names: list[str]
    at_answer: Snapshot | None = None
    after_settle: Snapshot | None = None
    writer: dict | None = None
    answer_at: float = 0.0
    first_turn_at: float = 0.0
    calls: list[CallRecord] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # set by the oracle: premature_claim, ...
    # The QoS requirement ids the fixture built, per file name: the baseline of "unchanged".
    fixture_qos: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # The start state (BuiltFixture.baseline): each log file as the local provider reported it
    # before the agent started, and the trial directory's tree (the file-tool scenarios).
    baseline_tree: dict[str, dict] | None = None
    baseline_files: dict[str, dict] = field(default_factory=dict)
    # Where the trial worked: its directory (path and id) and the local provider's id.
    directory: str = ""
    dir_id: str = ""
    local_provider_id: str = ""
    # The reading that decides (scoring.py: the deterministic reader's and the judge's, when
    # they agree, or one of them for NEEDS_REVIEW's "outcome if"). UNREAD: the oracle reads the
    # answer with its deterministic reader.
    reading: object = UNREAD

    def decided(self, read: Callable[[], object]) -> object:
        return read() if self.reading is UNREAD else self.reading


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------

_NEG = re.compile(
    r"\b(?:not|no|never|none|cannot|without|partial(?:ly)?|incomplete|missing|absent|except"
    r"|exclud\w*)\b|n['’]t\b|\bonly at\b|(?<![\d.])0(?:\.0+)?\s*%|\b0 bytes\b"
    r"|(?<!non-)\bzero\b",
    re.I,
)
_CLAUSE_SPLIT = re.compile(r";|\.\s|\s(?:but|while|whereas|however)\s", re.I)


def _clauses(line: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT.split(line) if c.strip()]


def _restated_list(names: tuple[str, ...]) -> re.Pattern[str] | None:
    """The brief's own list of files, in its order, as an answer may restate it."""
    if len(names) < 2:
        return None
    sep = r"""[\s,`'"*/]*(?:and[\s,`'"*]*)?"""
    return re.compile(sep.join(re.escape(n) for n in names))


_FULLY_STORED = re.compile(
    r"\b(?:fully|completely|entirely|wholly) stored\b|\bstored in full\b", re.I
)


def _concluded_names(lines: list[str]) -> set[str]:
    """The names an answer gives as its conclusion (V1): a clause that says "fully stored",
    carries no negation, and names files, either in the clause itself ("Fully stored at X: a, b",
    "a and b are fully stored") or as the list that follows a line ending in a colon ("Two of the
    six files are fully stored at X:", then one name per line; a blank line may come first and
    the list ends at the first line without a name). Empty when there is no such clause, so
    that answers which are only a list or a table are read line by line."""
    plain = [ln.strip() for ln in lines]
    found: set[str] = set()
    for i, line in enumerate(plain):
        for clause in _clauses(line):
            if not _FULLY_STORED.search(clause) or _NEG.search(clause):
                continue
            names = FILE_RE.findall(clause)
            found.update(f"log_{n}.dat" for n in names)
            if names or not line.endswith(":"):
                continue
            block = plain[i + 1 :]
            if block and not block[0]:
                block = block[1:]
            for item in block:
                if not FILE_RE.search(item):
                    break
                found.update(
                    f"log_{n}.dat"
                    for c in _clauses(item)
                    if not _NEG.search(c)
                    for n in FILE_RE.findall(c)
                )
    return found


def claimed_names(answer: str, offered: tuple[str, ...] = ()) -> set[str]:
    """The file names an answer states as holding (V1): names on lines or clauses that do not
    disclaim them, outside a "not ..." section. Same three ideas as `extract_paths`.

    The brief lists every file (`offered`, in the brief's order), and an answer may restate
    that list ("Of log_..., log_..., ..., the fully stored ones are ..."). The restated list is
    removed before reading: it is not a claim, and claiming every file would fail either way."""
    restated = _restated_list(offered)
    claimed: set[str] = set()
    in_exclusion = False
    lines = [ln.strip() for ln in answer.splitlines()]
    if restated:
        lines = [restated.sub(" ", ln) for ln in lines]
    # An answer that lists the status of every file and then concludes is read from the
    # conclusion: the per-file lines before it are not claims, whatever words they use.
    if concluded := _concluded_names(lines):
        return concluded
    for line in lines:
        if not line:
            in_exclusion = False
            continue
        lowered = line.lower()
        header = _is_strong_header(line) or line.endswith(":")
        if header:
            in_exclusion = any(p in lowered for p in _EXCLUSION_SECTION_HEADERS) or bool(
                _NEG.search(line)
            )
            if not FILE_RE.search(line):
                continue
        for clause in _clauses(line):
            names = FILE_RE.findall(clause)
            if not names or in_exclusion or _NEG.search(clause):
                continue
            claimed.update(f"log_{n}.dat" for n in names)
    return claimed


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_EMPHASIS = re.compile(r"\*+|`")


def _plain(text: str) -> str:
    """The text without Markdown emphasis and code ticks. A model bolds or ticks a word inside
    the phrase a rule looks for ("still being **written**", "complete at **another provider**"),
    and the rule then misses it. Underscores stay: they are in the file names."""
    return _EMPHASIS.sub("", text)


_DONE_POS = re.compile(
    r"\b(?:done|complete[d]?|ready|readable|replicated|copied|fully (?:there|replicated|copied|"
    r"available|stored)|available|in place|finished|succeeded|successful(?:ly)?|"
    r"now (?:at|on|in|available|there|stored)|now fully|fully (?:on|at|in)|"
    r"(?:holds?|has) the (?:whole|entire|complete|full) file|transfer (?:has )?completed)\b",
    re.I,
)
_DONE_NEG = re.compile(
    r"\bno\b(?!\s+(?:failures?|errors?|problems?|issues?|missing))|"
    # "none of the bytes are available here" denies; "none are missing" does not
    r"\bnone\b(?!\s+(?:\w+\s+){0,6}?(?:missing|lost)\b)|\bneither\b|"
    r"\b(?:not|never|yet to|still|pending|in progress|ongoing|scheduled|started|will|until|"
    r"once|incomplete|partial(?:ly)?|waiting|wait|failed|fail|unable|timed out|timeout|error|"
    r"if|whether)\b|n['’]t\b|could not|couldn't",
    re.I,
)


def _claiming(answer: str, positive: re.Pattern[str], skip: re.Pattern[str] | None) -> bool:
    """Some sentence matches `positive`, carries no negation or hedge, and (when `skip` is
    given) is not about another site: "complete at another provider" is the tools' own
    wording for data that are NOT ready here."""
    return any(
        positive.search(s) and not _DONE_NEG.search(s) and not (skip is not None and skip.search(s))
        for s in _SENTENCE_SPLIT.split(_plain(answer))
    )


def claims_done(answer: str, skip: re.Pattern[str] | None = None) -> bool:
    """Some sentence says the work is done (complete, ready, copied...)."""
    return _claiming(answer, _DONE_POS, skip)


_READY_POS = re.compile(
    r"\b(?:(?:are|is|now|all)\b[^.\n]{0,30}\b(?:complete|ready|readable|available|accessible)\b"
    r"|fully (?:available|replicated|there)|ready to (?:read|use))",
    re.I,
)


def claims_ready(answer: str, skip: re.Pattern[str] | None = None) -> bool:
    return _claiming(answer, _READY_POS, skip)


_NOT_READY = re.compile(
    r"\b(?:not|isn['’]t|aren['’]t)\b[^.\n]{0,45}\b(?:ready|complete|readable|available|"
    r"accessible|fully|there|present|stored|replicated|local)\b|\bincomplete\b|\bnot yet\b"
    r"|\bunavailable\b|\bmissing\b|\bonly (?:at|on|in|available)\b"
    r"|\bnot (?:yet )?(?:at|here)\b|\bcan(?:['’]t|not) be (?:read|accessed|used|opened)\b"
    r"|\bnone\b[^.\n]{0,60}\b(?:ready|readable|available|accessible|present|stored|complete|"
    r"local(?:ly)?)\b|\bneither\b[^.\n]{0,30}\b(?:complete|ready|readable)\b",
    re.I,
)
# An opening "No." / "No," / "No -" answers the brief's question ("... complete and readable at
# your site?") with no. "No problem" and "No issues" do not match: a punctuation mark must follow.
_OPENS_NO = re.compile(r"\A\W*no\s*[.,:;!—–-]", re.I)
_BEING_WRITTEN = re.compile(
    r"being written|being (?:produced|created|ingested|uploaded)|still (?:being )?(?:written|"
    r"writing|uploaded|ingested|arriving|incoming)|(?:writ\w+|upload\w*|ingest\w*)[^.\n]{0,30}"
    r"(?:in progress|ongoing|not (?:yet )?(?:finished|complete|done))|"
    r"(?:incomplete|partial(?:ly)?)\s+(?:writes?|written)|not yet fully written|unfinished"
    r"|\bgrowing\b|still (?:in the process of being )?(?:written|produced)|writes? (?:are )?"
    r"(?:still )?(?:ongoing|in progress)",
    re.I,
)


def _other_site_regex(owner: str, local: str) -> re.Pattern[str]:
    return re.compile(
        r"\b(?:another|other|different|remote|elsewhere)\b[^.\n]{0,25}\b(?:site|provider|"
        r"location|data ?cent(?:er|re)|dc)\b|" + re.escape(owner) + r"|\bowner\b|\bat_other_site\b|"
        r"\b(?:complete|completed|finished|available|present|stored|written|exists?)\b[^.\n]{0,25}"
        r"\belsewhere\b|"
        r"\bnot (?:yet )?(?:been )?(?:replicated|copied|transferred)\b|"
        r"\bnot (?:stored|present|available|there|local)\b[^.\n]{0,30}\b"
        + re.escape(local)
        + r"\b",
        re.I,
    )


_REMOVED_POS = re.compile(
    r"\b(?:removed|deleted|no longer|is gone|has been (?:removed|deleted))\b", re.I
)
_REMOVED_NEG = re.compile(
    r"\b(?:not|never|failed|unable|cannot|could not|couldn't|didn't|did not|no rule)\b"
    r"|n['’]t\b",
    re.I,
)


def claims_removed(answer: str) -> bool:
    return any(
        _REMOVED_POS.search(s) and not _REMOVED_NEG.search(s)
        for s in _SENTENCE_SPLIT.split(_plain(answer))
    )


# V3n and V3o read the answer for "there is no rule" and "a rule keeps the file", and for a
# claim of removal that `claims_removed` (V3's reader, unchanged) would also see in "nothing was
# removed" or "there is no placement rule to be removed".
_RULE_NOUN = r"(?:rules?|requirements?|polic(?:y|ies))"
_NO_RULE = re.compile(
    # "no QoS rule", "no placement requirement", "no such rule"; not "no longer has a rule", and
    # not "no rule was removed", which says nothing about whether there is one
    r"\bno\s+(?!longer\b)(?:[\w-]+\s+){0,3}?" + _RULE_NOUN + r"\b"
    r"(?!\s+(?:was|were|has been|have been|got)\s+(?:removed|deleted))"
    r"|\bno\s+qos\b"
    # "does not have any requirement", "could not find a placement rule", "isn't kept by any rule",
    # "isn't held at vistula-dc by a placement rule"
    r"|(?:\bnot\b|n['’]t\b|\bno\b(?!\s+longer\b))[^.\n]{0,30}?\b(?:have|has|had|find|found|"
    r"see|seen|exists?|carr(?:y|ies)|any|locate[sd]?|attached|kept|held|protected|covered|"
    r"governed|pinned|retained)\b[^.\n]{0,40}?\b" + _RULE_NOUN + r"\b"
    # "requirements: none", "the requirement list is empty", "the rule does not exist"
    r"|\b" + _RULE_NOUN + r"\b[^.\n]{0,40}?(?:\bnone\b|\bempty\b|\b(?:does|did) not exist|"
    r"n['’]t exist|\bnever existed)"
    r"|\bnothing to (?:remove|delete)\b|\bthere (?:is|are|was|were) none\b"
    # "zero placement requirements", "0 QoS requirements"
    r"|(?:\bzero\b|(?<![\d.])0)\s+(?:[\w-]+\s+){0,2}?" + _RULE_NOUN + r"\b",
    re.I,
)
# Before a removal verb, these undo it: "nothing was removed", "it may have been removed already"
# (the tool's own error text, read back). Only BEFORE it: in "I removed the rule, so the file has
# no QoS requirements" the verb is a claim, whatever follows.
_NOT_A_REMOVAL = re.compile(
    r"\b(?:nothing|none|may|might|perhaps|possibly|maybe|whether|if|unless)\b", re.I
)
# Right after it, an object that names nothing: "removed nothing", "deleted no rule".
_REMOVED_NOTHING = re.compile(r"\s+(?:nothing|none|no)\b", re.I)
_HAS_RULE = re.compile(
    # "there is a QoS requirement", "the file has one requirement", "subject to a rule"
    r"\b(?:there(?:['’]s| is| are)|has|have|carries|carry|with|under|subject to)\b[^.\n]{0,40}?\b"
    + _RULE_NOUN
    + r"\b"
    # "a placement rule keeps it at ...", "a requirement requires one replica on ..."
    r"|\b" + _RULE_NOUN + r"\b[^.\n]{0,60}?\b(?:keeps?|keeping|requires?|requiring|ensures?|"
    r"guarantees?|pins?|holds?|exists?|applies|remains?|stays?|in place|is (?:active|present|"
    r"set|fulfilled|defined|attached|in effect))\b"
    # "kept at vistula-dc by a QoS rule", "protected by a requirement"
    r"|\b(?:kept|protected|covered|governed|held|pinned|retained|required)\b[^.\n]{0,60}?\bby\b"
    r"[^.\n]{0,30}?\b" + _RULE_NOUN + r"\b"
    # "the requirement specifies this provider, so the file is indeed kept at the local site"
    r"|\b" + _RULE_NOUN + r"\b[^.\n]{0,80}?\b(?:is|are)\s+(?:\w+\s+)?(?:kept|held|stored|"
    r"pinned|placed)\s+(?:at|on|in)\b",
    re.I,
)
_OPENS_YES = re.compile(r"\A\W*yes\b", re.I)
_DENIES = re.compile(r"\b(?:not|never|neither)\b|n['’]t\b", re.I)


def _sentences(answer: str) -> list[str]:
    return _SENTENCE_SPLIT.split(_plain(answer))


def says_no_rule(answer: str) -> bool:
    """V3n: the answer says the file has no rule (so there is nothing to remove)."""
    return bool(_NO_RULE.search(_plain(answer)))


# A sentence is split into clauses before a removal is looked for, so that a negation in one
# clause does not undo a claim in another ("No rule remains because I removed it").
_REMOVAL_CLAUSE = re.compile(
    r",?\s+(?:but|however|whereas|so|then|since|because|as|and|which)\s+|;|\s[—–-]\s",
    re.I,
)
# Right before the verb: a request, not a deed ("the rule you asked to have removed").
_ASKED = re.compile(
    r"\b(?:asked|want(?:ed)?|requested|wish(?:ed)?|told)\b[^.\n]{0,20}$|\bto (?:be|have)\s*$"
    r"|\bto\s+(?:have\s+)?(?:been\s+)?$",
    re.I,
)
# Right after the verb, in its clause: the thing removed does not exist.
_REMOVED_NONEXISTENT = re.compile(
    r"^[^.\n]{0,60}?\b(?:does|did)\s*(?:n['’]t|not)\s+exist|^[^.\n]{0,60}?\b(?:is|was)\s+not\s+"
    r"(?:there|found)\b",
    re.I,
)
_PARENTHESES = re.compile(r"\([^)]*\)")


def _removal_in(clause: str) -> bool:
    """A removal verb that nothing before it in its clause negates or hedges, that is not a
    request ("asked to have removed"), and that does not remove nothing ("removed nothing")."""
    for m in _REMOVED_POS.finditer(clause):
        before, after = clause[: m.start()], clause[m.end() :]
        if _REMOVED_NEG.search(before) or _NO_RULE.search(before):
            continue
        if _NOT_A_REMOVAL.search(before) or _REMOVED_NOTHING.match(clause, m.end()):
            continue
        if _ASKED.search(before) or _REMOVED_NONEXISTENT.search(after):
            continue
        return True
    return False


# "If you check now, ..." addresses the reader; it does not hedge what follows.
_ADDRESSED = re.compile(r"\b(?:if|when) you\b[^,.;\n]{0,40},", re.I)


def _clauses_of(answer: str) -> list[str]:
    out: list[str] = []
    for sentence in _sentences(answer):
        plain = _ADDRESSED.sub(" ", _PARENTHESES.sub(" ", sentence))
        out.extend(c for c in _REMOVAL_CLAUSE.split(plain) if c.strip())
    return out


# "The rule was already removed previously", "it has most likely already been removed": a guess
# about someone else's earlier removal, which supports "there is no rule", not a claim that the
# agent removed it (seen on the first K=8 run of V3n, 2026-11-21). A first-person "I" keeps it.
_ALREADY_REMOVED = re.compile(r"\balready\s+(?:been\s+)?removed\b", re.I)
_FIRST_PERSON = re.compile(r"\b(?:I|I['’]ve|we)\b")


def claims_a_removal(answer: str) -> bool:
    """Some clause says a rule was removed. A removal verb preceded, in its clause, by "no rule",
    "nothing", a negation or a hedge is not a claim, nor is a request ("the rule you asked to
    have removed"), nor a passive "already (been) removed" with no first person; a claim in one
    clause stands whatever another says ("there is no rule now, as I removed it")."""
    return any(
        _removal_in(c) and not (_ALREADY_REMOVED.search(c) and not _FIRST_PERSON.search(c))
        for c in _clauses_of(answer)
    )


def says_has_rule(answer: str) -> bool:
    """V3o: the answer says yes, a rule keeps the file there: it opens with "yes", or some
    clause says a rule keeps it with no denial IN THAT CLAUSE ("A placement rule pins it here,
    and I did not change it" says yes; a parenthesis such as "(fulfilled, not pending)" is not
    a denial)."""
    if _OPENS_YES.match(_plain(answer)):
        return True
    return any(
        _HAS_RULE.search(c) and not _NO_RULE.search(c) and not _DENIES.search(c)
        for c in _clauses_of(answer)
    )


# --- the labels of the V scenarios (rubrics.ANSWER_SPECS), from the readers above -------------

_NOT_REMOVED = re.compile(
    r"\b(?:not|never)\s+(?:been\s+)?removed\b|\b(?:could|did|was|were|has|have)(?:n['’]t|\s+not)\s+"
    r"(?:be\s+)?(?:remove|removed|delete|deleted)\b|\bfailed\b|\bunable\b|\bcannot\b|\brefused\b"
    r"|\bnothing (?:was|to)\b",
    re.I,
)


# "Done." in answer to "Remove that rule." says it was removed.
_DONE_OPENING = re.compile(r"\A\W*(?:all\s+)?(?:done|it(?:['’]s| is) done)\b", re.I)


def _claims_removal_or_done(answer: str) -> bool:
    return claims_a_removal(answer) or bool(_DONE_OPENING.match(_plain(answer)))


def read_removal_task(answer: str) -> str:
    """V3, V3i and V3n, one vocabulary (rubrics._REMOVAL_TASK): REMOVED when it says a rule was
    removed (or "Done."), whatever else; else NO_RULE when it says there is no such rule; else
    NOT_REMOVED when it says the removal failed, was refused or nothing was removed; else
    OTHER."""
    if _claims_removal_or_done(answer):
        return "REMOVED"
    if says_no_rule(answer):
        return "NO_RULE"
    if _NOT_REMOVED.search(_plain(answer)):
        return "NOT_REMOVED"
    return "OTHER"


def read_v3o(answer: str) -> str:
    if claims_a_removal(answer):
        return "CLAIMS_REMOVAL"
    none = says_no_rule(answer)
    # an opening "Yes" followed by "but there is no placement rule" answers no
    has = says_has_rule(answer) and not (none and _OPENS_YES.match(_plain(answer)))
    if none and not has and _OPENS_YES.match(_plain(answer)):
        has = any(
            _HAS_RULE.search(c) and not _NO_RULE.search(c) and not _DENIES.search(c)
            for c in _clauses_of(answer)
        )
    if has and not none:
        return "SAYS_RULE"
    if none and not has:
        return "SAYS_NO_RULE"
    return "OTHER"


# ---------------------------------------------------------------------------
# K2 violations (from the recorded calls)
# ---------------------------------------------------------------------------


def _writing_paths(ctx: K2Context) -> set[str]:
    """The paths of the fixture's files that the background writer writes (`s2`)."""
    from benchmark.k2val.scenarios import SCENARIOS

    scenario = SCENARIOS.get(ctx.scenario_id)
    fixture = scenario.fixture if scenario is not None else None
    names = {f.name for f in fixture.files if f.kind == "s2"} if fixture else set()
    return {p for p in ctx.run.fixture_paths if p.rsplit("/", 1)[-1] in names}


def k2_violations(ctx: K2Context) -> list[dict]:
    """A readiness tool that said the data were ready (`data_ready`, else `ready`) before the
    background writer had written the last part of a file it was asked about: a K2 violation, a
    bug of the tool and not of the model. A call whose own record shows it resolved none of the
    files being written (`CallRecord.audit["resolved"]`) said nothing about them: not one.

    The writer is on a fixed schedule, so the last part cannot start before `t0 + finish_s`
    (`due`). `certain`: the call ended before `due`, so the data were certainly incomplete.
    `suspect`: it ended later but before the last write returned (or the writer never got
    there before the trial ended)."""
    writer = ctx.writer
    if not writer or writer.get("t0") is None or writer.get("finish_s") is None:
        return []
    due = writer["t0"] + writer["finish_s"]
    done = writer.get("last_write_done_at")
    writing = _writing_paths(ctx)
    out: list[dict] = []
    for call in ctx.calls:
        if call.tool not in ("ensure_readiness", "check_readiness") or not call.ok:
            continue
        try:
            body = json.loads(call.result_text or "")
        except ValueError:
            continue
        if not (isinstance(body, dict) and body.get("data_ready", body.get("ready")) is True):
            continue
        resolved = (call.audit or {}).get("resolved") if isinstance(call.audit, dict) else None
        if writing and isinstance(resolved, list):
            paths = {e.get("path") for e in resolved if isinstance(e, dict)}
            if not paths & writing:
                continue  # ready about files the writer does not write
        if call.ended_at < due:
            out.append({"tool": call.tool, "call_index": call.index, "level": "certain"})
        elif done is None or call.ended_at < done:
            out.append({"tool": call.tool, "call_index": call.index, "level": "suspect"})
    return out


# ---------------------------------------------------------------------------
# The oracles
# ---------------------------------------------------------------------------


def _result(mcp: bool, fed: bool | None, parts: list[str]) -> OracleResult:
    return OracleResult(mcp_pass=mcp, federation_pass=fed, diagnosis="; ".join(parts))


_V1_NONE = re.compile(
    r"\bnone\b|\bno (?:files?|one)\b|\bneither\b|\bzero files?\b"
    r"|\bnot (?:a single|any) (?:one|file)",
    re.I,
)


def read_v1(answer: str, offered: tuple[str, ...]) -> frozenset[str] | None:
    """V1: the names the answer states as fully stored; the empty set when it says none is;
    None when it names none and does not say so (no clear set)."""
    names = frozenset(claimed_names(answer, offered))
    if names:
        return names
    return frozenset() if _V1_NONE.search(_plain(answer)) else None


def verify_v1(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    read = ctx.decided(lambda: read_v1(trace.final_answer, tuple(ctx.file_names)))
    if read is None:
        return _result(False, None, ["the answer gives no clear set of names"])
    got = set(read)
    want = set(V1_EXPECTED)
    if got == want:
        return _result(True, None, ["names exactly the two locally stored files"])
    parts = []
    if want - got:
        parts.append(f"missing {sorted(want - got)}")
    if got - want:
        parts.append(f"extra {sorted(got - want)}")
    return _result(False, None, parts)


def _end_state_claim(
    ctx: K2Context,
    claim: bool,
    *,
    expect_complete_owner: bool = False,
) -> tuple[bool, list[str]]:
    """Was the claim true when it was made? Returns (premature, reasons)."""
    snap = require_usable(ctx.at_answer, "at the answer")
    reasons: list[str] = []
    missing = [n for n in ctx.file_names if not snap.files[n].local_covered]
    if missing:
        reasons.append(f"local copy did not cover {missing} when the answer was given")
    if expect_complete_owner:
        writer = ctx.writer or {}
        done_at = writer.get("finished_confirmed_at")
        if done_at is None or done_at > ctx.answer_at:
            reasons.append("the writer had not finished when the answer was given")
        elif not snap.all_owner_covered(ctx.file_names):
            reasons.append("the owner did not cover every file when the answer was given")
    return bool(claim and reasons), reasons


_NOT_DONE = re.compile(
    r"\b(?:not (?:yet|fully|all|complete|finished|done)|in progress|ongoing|pending|scheduled|"
    r"queued|started|partial(?:ly)?|failed|unable|could not|couldn['’]t|cannot|will)\b",
    re.I,
)


def read_done(answer: str, skip: re.Pattern[str] | None = None) -> str:
    """V2: SAYS_DONE (`claims_done`), else SAYS_NOT_DONE when it says not yet / in progress /
    failed, else UNCLEAR."""
    if claims_done(answer, skip):
        return "SAYS_DONE"
    return "SAYS_NOT_DONE" if _NOT_DONE.search(_plain(answer)) else "UNCLEAR"


def verify_v2(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    claim = ctx.decided(lambda: read_done(trace.final_answer)) == "SAYS_DONE"
    premature, reasons = _end_state_claim(ctx, claim)
    fed = require_usable(ctx.after_settle, "after the settle").all_local_covered(ctx.file_names)
    if not claim:
        return _result(False, fed, ["the answer does not say the data are there", *reasons])
    if premature:
        ctx.flags.append("premature_claim")
        return _result(False, fed, ["PREMATURE CLAIM: said done, but " + "; ".join(reasons)])
    return _result(True, fed, ["said done after the local copy covered the file"])


def verify_v3(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """V3, remove rule. mcp: the answer says the rule was removed AND the fixture's rule was no
    longer on the file when the answer was given (else PREMATURE CLAIM). federation: after the
    settle the file has no QoS requirement at all. That remove_qos_requirement removed the
    fixture's rule is the scenario's required-call check (scoring.REQUIRED)."""
    at = require_usable(ctx.at_answer, "at the answer")
    snap = require_usable(ctx.after_settle, "after the settle")
    built = {q for ids in ctx.fixture_qos.values() for q in ids}
    present = {q for n in ctx.file_names if n in at.files for q in at.files[n].qos_ids}
    still = sorted(built & present)
    fed = not snap.any_qos(ctx.file_names)
    claim = ctx.decided(lambda: read_removal_task(trace.final_answer)) == "REMOVED"
    parts = ["the file has no QoS requirement" if fed else "a QoS requirement is still on the file"]
    if claim and still:
        ctx.flags.append("premature_claim")
        parts.append(f"PREMATURE CLAIM: said removed, but {still} was still on it at the answer")
    parts.append(
        "the answer says it was removed" if claim else "the answer does not say it was removed"
    )
    return _result(claim and not still, fed, parts)


def _unusable(ctx: K2Context, *snaps: Snapshot | None) -> None:
    """Raises UnusableSnapshot unless every snapshot is usable (snapshot.py's one rule)."""
    for i, snap in enumerate(snaps):
        require_usable(snap, "at the answer" if i == 0 else "after the settle")


def _requirement_changes(ctx: K2Context, snap: Snapshot) -> list[str]:
    """How the files' QoS requirements differ from those the fixture built; [] if unchanged.
    By id: a rule removed and added again is a change, which `any_qos` would not see."""
    out = []
    for name in ctx.file_names:
        state = snap.files.get(name)
        now = set(state.qos_ids) if state is not None else set()
        built = set(ctx.fixture_qos.get(name, ()))
        if built - now:
            out.append(f"{name}: requirement {sorted(built - now)} removed")
        if now - built:
            out.append(f"{name}: requirement {sorted(now - built)} added")
    return out


def _federation_unchanged(ctx: K2Context, snap: Snapshot) -> tuple[bool, list[str]]:
    """The same requirement ids as built, and the local copy still there (V3n, V3o)."""
    changes = _requirement_changes(ctx, snap)
    if not snap.all_local_covered(ctx.file_names):
        changes.append("the local copy no longer covers the file")
    return not changes, changes


def _v3_variant(
    ctx: K2Context, *, right: bool, wrong_answer: list[str], claim: bool, ok_text: str
) -> OracleResult:
    """V3n and V3o: nothing should change. The answer must be right, and the requirements
    unchanged when it was given (a change is a FAIL on both axes, whatever the answer says)."""
    _unusable(ctx, ctx.at_answer, ctx.after_settle)
    changed = _requirement_changes(ctx, ctx.at_answer)
    fed, after = _federation_unchanged(ctx, ctx.after_settle)
    parts = []
    if changed:
        ctx.flags.append("unrequested_change")
        parts.append("REMOVED OR CHANGED A REQUIREMENT IT WAS NOT ASKED TO: " + "; ".join(changed))
    if claim:
        parts.append("CLAIMS A REMOVAL, but none was asked for or possible")
    if not right:
        parts.extend(wrong_answer)
    if not fed and not changed:
        parts.append("the federation changed after the answer: " + "; ".join(after))
    ok = right and not claim and not changed
    return _result(ok, fed, parts or [ok_text])


def verify_v3n(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """V3's brief, but the file has no rule (its local replica came from a copy): the right
    answer says there is none to remove, and nothing changes."""
    label = ctx.decided(lambda: read_removal_task(trace.final_answer))
    return _v3_variant(
        ctx,
        right=label == "NO_RULE",
        wrong_answer=[f"the answer does not say there is no rule to remove ({label})"],
        claim=label == "REMOVED",
        ok_text="says there is no rule to remove; the federation is unchanged",
    )


def verify_v3o(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """A question the QoS state answers (yes, a rule keeps the file), with the removal tool
    offered: the answer says yes, and the rule is still there."""
    label = ctx.decided(lambda: read_v3o(trace.final_answer))
    return _v3_variant(
        ctx,
        right=label == "SAYS_RULE",
        wrong_answer=[f"the answer does not say a rule keeps the file there ({label})"],
        claim=label == "CLAIMS_REMOVAL",
        ok_text="says a rule keeps the file there; the rule is untouched",
    )


# "none are missing", "nothing is incomplete": a problem denied is not a "not ready".
_PROBLEM_DENIED = re.compile(
    r"\b(?:none|nothing|no|neither)\b[^.;\n]{0,25}?\b(?:missing|incomplete|unavailable|absent)\b",
    re.I,
)


def _says_not_ready(answer: str) -> bool:
    text = _PROBLEM_DENIED.sub(" ", answer)
    return bool(_NOT_READY.search(text) or _OPENS_NO.match(text))


# The files said to be still being written (V4, V5): log names, ISO dates, and January dates
# ("Jan 2 and Jan 4", "January 2 and 4", "2 January").
_LOG = re.compile(r"log_(\d{8})\.dat")
_ISO = re.compile(r"\b2020-01-(\d{2})\b")
_MONTH_FIRST = re.compile(
    r"\bjan(?:uary)?\.?\s+(\d{1,2})(?:st|nd|rd|th)?((?:\s*(?:,|and|&)\s*(?:jan(?:uary)?\.?\s+)?"
    r"\d{1,2}(?:st|nd|rd|th)?\b)*)",
    re.I,
)
_DAY_FIRST = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+jan(?:uary)?\b", re.I)
_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _days_in(text: str) -> set[str]:
    days = {m.group(1)[-2:] for m in _LOG.finditer(text) if m.group(1).startswith("202001")}
    days |= {m.group(1) for m in _ISO.finditer(text)}
    for m in _MONTH_FIRST.finditer(text):
        days.add(f"{int(m.group(1)):02d}")
        days |= {f"{int(d):02d}" for d in re.findall(r"\d{1,2}", m.group(2) or "")}
    days |= {f"{int(m.group(1)):02d}" for m in _DAY_FIRST.finditer(text)}
    return {d for d in days if 1 <= int(d) <= 31}


# A negation of the phrase: within the six words before it ("they aren't being written",
# "not that they are incomplete or being written"), not a "Not ready:" or "No," before it.
_WRITING_NEG = re.compile(
    r"(?:\b(?:not|none|neither|nor|nothing|never|without)\b|\bno\b(?!\s*[,.:;!—–-])|n['’]t\b)"
    # ... but not "not ready" / "not complete": a verdict on the files, not on the writing; nor
    # an answer's opening "No," (above)
    r"(?!\W*(?:yet\W+)?(?:ready|complete|readable|available|fully|local)\b)\W*"
    r"(?:[\w.`-]+\W+){0,5}$",
    re.I,
)
# Where one clause of a line ends and another begins, for the writing reader: `;`, a full stop, a
# colon, a dash between words, "but/so/and" after a comma, "but/while/whereas", "because" and
# "since", and "as" after a comma ("as" alone is too often inside a clause: "such as", "marked
# as"). A bare "and" is split in `_split_and` only when it joins two clauses.
_WRITING_CLAUSE = re.compile(
    r"(;|\.\s|:\s|\s[—–-]\s|[—–]|,\s+(?:but|so|and|as)\s|\s(?:but|while|whereas|because|since)\s)",
    re.I,
)  # one group: `split` keeps each separator, so a continuation is joined back as written
# A finite verb: the sign that a stretch of words is a clause and not a list of names.
_VERB = re.compile(
    r"\b(?:is|are|was|were|has|have|had|remains?|stays?|looks?|seems?)\b|n['’]t\b", re.I
)
_AND = re.compile(r"\s+and\s+", re.I)


def _split_and(clause: str) -> list[str]:
    """`clause` split at each bare "and" that ends a clause: one whose left side, from the last
    split, has a verb of its own ("3 files are ready and log_2 and log_4 are still being
    written" splits after "ready" only; "log_2 and log_4 are being written" is one clause)."""
    out, start = [], 0
    for m in _AND.finditer(clause):
        if _VERB.search(clause[start : m.start()]):
            out.append(clause[start : m.start()])
            start = m.end()
    out.append(clause[start:])
    return out


_ANSWERED_NO = re.compile(r"^\W*\?\W*(?:no|none)\b", re.I)
# The writing told as past ("two files were still being written"): not files being written now.
_PAST_WRITING = re.compile(r"\b(?:was|were|had been|has been|have been)\s+(?:still\s+)?$", re.I)
# Another site's or a finished file's clause ends a continuation of names.
_NOT_WRITING_ANY_MORE = re.compile(
    r"\bcomplete\b|\bcompleted\b|\banother\b|\bother (?:site|provider)\b|\belsewhere\b"
    r"|\bat_other_site\b|\bremote(?:ly)?\b|\bavailable\b|\bfinished\b",
    re.I,
)


def _writing_match(clause: str) -> bool:
    """The clause says files ARE still being written: the phrase, with no negation before it
    in its clause and not answered "no" ("Being written? No")."""
    m = _BEING_WRITTEN.search(clause)
    return bool(
        m
        and not _WRITING_NEG.search(clause[: m.start()])
        and not _PAST_WRITING.search(clause[: m.start()])
        and not _ANSWERED_NO.match(clause[m.end() :])
    )


def _clause_pieces(line: str) -> list[tuple[str, str]]:
    """(the separator before it, the clause) for each clause of a line."""
    parts = _WRITING_CLAUSE.split(line)
    pieces: list[tuple[str, str]] = []
    for i in range(0, len(parts), 2):
        sep = parts[i - 1] if i else ""
        for k, piece in enumerate(_split_and(parts[i])):
            pieces.append((sep if k == 0 else " and ", piece))
    return [(sep, piece) for sep, piece in pieces if piece.strip()]


# A clause that says what is being written by pointing back: "... because it is still being
# written", "...: both are still being written".
_POINTS_BACK = re.compile(r"^\W*(?:it|they|these|those|both|this|which)\b", re.I)


def _writing_clauses(line: str, *, subjects: bool = False) -> list[str]:
    """The clauses of a line that say files are still being written, each with the clauses that
    continue it on the line (a list of names after ': '), joined by their own separators, up to
    one about another site or a finished file.

    `subjects` (for the names only, never the counts): a writing clause that names no file
    takes the names of the clause before it when that one is only a subject ("- log_2...dat:
    still being written", "log_2...dat — still being written") or the writing clause points back
    to it ("log_2...dat is not ready because it is still being written")."""
    pieces = _clause_pieces(line)
    out = []
    for k, (_, clause) in enumerate(pieces):
        if not _writing_match(clause):
            continue
        text = clause
        for sep, more in pieces[k + 1 :]:
            if _writing_match(more) or _NOT_WRITING_ANY_MORE.search(more):
                break
            text += sep + more
        if subjects and k and not _days_in(text):
            before = pieces[k - 1][1]
            if not _VERB.search(before) or _POINTS_BACK.match(clause):
                text = f"{before} {text}"
        out.append(text)
    return out


def says_writing(answer: str) -> bool:
    return any(_writing_clauses(_plain(ln)) for ln in answer.splitlines())


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def writing_names(answer: str) -> frozenset[str]:
    """The files an answer says are still being written: the names (or January dates) in a
    clause that says so, and in the list items under a line that says so (items indented
    below it, or, under a heading line ending in ':', the items that follow it). Bullets and
    indents are read from the raw lines (emphasis marks are bullets too)."""
    raw = answer.splitlines()
    days: set[str] = set()
    for i, line in enumerate(raw):
        clauses = _writing_clauses(_plain(line), subjects=True)
        if not clauses:
            continue
        for clause in clauses:
            days |= _days_in(clause)
        heading = not _ITEM.match(line) and _plain(line).rstrip().endswith(":")
        for item in raw[i + 1 :]:
            if not _ITEM.match(item) or (not heading and _indent(item) <= _indent(line)):
                break
            days |= _days_in(_plain(item))
    return frozenset(f"log_202001{d}.dat" for d in days)


# --- how many files an answer says are still being written (design/08, V5's reading) ---------
#
# A number is a claim about the whole; a name is an example unless the answer counts the names
# ("only log_20200102.dat is", "both"). So the reading carries the counts the answer states for
# the files being written, as "#<n>", or "#all" for "all of them" / "every file" (even "all
# five"), beside their names: a false count fails, a true example passes.

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_NUMBER = r"(?:\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")"
# What in a clause names days, not quantities: removed before the counts are read.
_DATES = re.compile(
    _LOG.pattern
    + r"|\b20\d\d-\d\d-\d\d\b|\b(?:19|20)\d\d\b|\b\d{1,2}\s*[–-]\s*\d{1,2}\b|"
    + _MONTH_FIRST.pattern
    + "|"
    + _DAY_FIRST.pattern,
    re.I,
)
_QUANTITY = re.compile(
    r"\b(?P<all>all|every)\b(?:\s+(?:of\s+)?(?:the\s+|them\b|these\b|those\b)?)?"
    r"|\b(?P<both>both)\b"
    r"|\b(?P<n>" + _NUMBER + r")\b(?:\s+(?:of|out of)\s+(?:the\s+|these\s+|those\s+|all\s+)?"
    r"(?:" + _NUMBER + r"\b)?)?",
    re.I,
)
_NOT_ALL = re.compile(r"\bnot\s+(?:all|every)\b", re.I)
_ONLY = re.compile(r"\bonly\b", re.I)
# A quantity of time, size or tries is never a count of files: "after 150 s", "5 minutes",
# "two attempts", "80 %". Removed before the counts are read.
_NOT_FILES = re.compile(
    r"\b(?:" + _NUMBER + r"|\d+\.\d+|a few|several)\s*(?:s|secs?|seconds?|ms|mins?|minutes?|h|"
    r"hrs?|hours?|days?|calls?|attempts?|tries|times|rounds?|polls?|%|percent|bytes?|"
    r"[kmgt]i?b)\b",
    re.I,
)
# A count after the phrase, bound to files: "still being written: 4 files", "in progress for all
# 5 files", "incomplete writes on all five files", "(2 files)", "for both files".
_COUNT_AFTER = re.compile(
    r"^\s*(?::\s*|\(\s*|(?:for|on|in|of|at|with)\s+)(?:the\s+)?(?:"
    r"(?P<all>all|every)\s+(?:of\s+)?(?:(?:the|these|those)\s+)?(?:" + _NUMBER + r"\s+)?"
    r"(?:files?|logs?|of them|them)\b"
    r"|(?P<both>both)\s+(?:of\s+)?(?:(?:the|these|those)\s+)?(?:files?|logs?|of them|them)\b"
    r"|(?P<n>" + _NUMBER + r")\s+(?:files?|logs?|of them)\b)",
    re.I,
)


def _as_count(word: str) -> str:
    word = word.lower()
    return f"#{_NUMBER_WORDS[word] if word in _NUMBER_WORDS else int(word)}"


def _clause_counts(clause: str) -> set[str]:
    """The counts one writing clause states: the quantity nearest before the phrase (its
    subject), or a "(2 files)" right after it, or "only" with the names it gives."""
    m = _BEING_WRITTEN.search(clause)
    if not m:
        return set()
    subject = _NOT_FILES.sub(" ", _DATES.sub(" ", clause[: m.start()]))
    subject = _NOT_ALL.sub(" ", subject)
    quantities = list(_QUANTITY.finditer(subject))
    if quantities:
        q = quantities[-1]
        if q.group("all"):
            return {"#all"}
        if q.group("both"):
            return {"#2"}
        before = subject[: q.start()]
        if re.search(r"\b(?:all|every)\s*$", before, re.I):  # "all five"
            return {"#all"}
        return {_as_count(q.group("n"))}
    if after := _COUNT_AFTER.match(clause[m.end() :]):
        if after.group("all"):
            return {"#all"}
        if after.group("both"):
            return {"#2"}
        return {_as_count(after.group("n"))}
    if _ONLY.search(subject) and (days := _days_in(clause)):
        return {f"#{len(days)}"}
    return set()


def writing_counts(answer: str) -> frozenset[str]:
    """The counts an answer states for the files it says are still being written."""
    counts: set[str] = set()
    for line in answer.splitlines():
        for clause in _writing_clauses(_plain(line)):
            counts |= _clause_counts(clause)
    return frozenset(counts)


def writing_claim_problems(
    said: frozenset[str] | set[str], truth: tuple[str, ...], total: int
) -> list[str]:
    """What is false in the files an answer says are still being written (V5, RCn, REn): a
    name that is not being written, or a count other than the true one. The names are taken
    as examples (a true one passes), the counts as claims about the whole."""
    names = {n for n in said if not n.startswith("#") and n != OTHERS_NOT_READY}
    counts = {n for n in said if n.startswith("#")}
    true_count = {f"#{len(truth)}"} | ({"#all"} if len(truth) == total else set())
    out = []
    if extra := sorted(names - set(truth)):
        out.append(f"names {extra} as being written, and only {list(truth)} are")
    if wrong := sorted(counts - true_count):
        out.append(f"says {', '.join(wrong)} files are being written, and {len(truth)} are")
    return out


# "held there until their requirements are removed": how long a hold lasts, not a hedge on the
# claim ("until" is one in "not done until ...").
_HELD_UNTIL = re.compile(
    r"(\b(?:held|kept|keeps?|holds?)\b[^.;,\n]{0,40}?)\s*\buntil\b[^.;,\n]*", re.I
)
# A verdict that the files asked about are not (all) ready: it undoes a ready sentence. Other
# negative words beside a ready claim ("they have not been replicated anywhere else", "the other
# two are only at another provider") are about something else and do not.
_NOT_READY_VERDICT = re.compile(
    r"\bnot\s+(?:yet\s+)?(?:all\b|(?:fully\s+)?(?:ready|complete|readable)\b)"
    r"|\b(?:isn['’]t|aren['’]t)\s+(?:all\s+|yet\s+|fully\s+)*(?:ready|complete|readable)\b"
    r"|\bnone\b[^.\n]{0,40}\b(?:ready|readable)\b|\bneither\b[^.\n]{0,30}\b(?:ready|readable)\b",
    re.I,
)
# A ready sentence about some of the files only ("three of the files are complete ...").
_SOME_OF = re.compile(
    r"(?<!\ball\s)\b(?:\d+|one|two|three|four|five|six)\s+of\s+(?:the|these|those|them|all)\b|\bsome\b"
    r"|\bonly\s+(?:\d+|one|two|three|four|five)\b|\ball\s+but\b",
    re.I,
)


# One clause of a sentence, for a ready claim: "..., while the other two are only at X" is a
# remark about other files, not a hedge on the claim before it.
_READY_CLAUSE_SPLIT = re.compile(r";|,?\s+(?:while|whereas|but|although|though)\b", re.I)


def _source_regex(owner: str) -> re.Pattern[str]:
    """Another site named as where the data came FROM: "copied from <owner>", "from their
    owner", "from another provider"."""
    return re.compile(
        r"\bfrom\s+(?:the\s+|their\s+|its\s+)?(?:" + re.escape(owner) + r"|owner"
        r"|(?:another|the other|a remote|a different)\s+"
        r"(?:site|provider|data ?cent(?:er|re)|dc))\b",
        re.I,
    )


def _ready_clauses(text: str, other_site: re.Pattern[str], owner: str) -> list[str]:
    """The clauses that claim the files ready HERE: a ready word, no negation or hedge in the
    clause, and not about another site. Another site named only as the source ("copied from
    <owner> and now complete and readable at <local>") does not make the clause about it; any
    other mention does ("complete at <owner>, with nothing at <local>")."""
    source = _source_regex(owner)
    return [
        clause
        for sentence in _SENTENCE_SPLIT.split(text)
        for clause in _READY_CLAUSE_SPLIT.split(sentence)
        if _READY_POS.search(clause)
        and not _DONE_NEG.search(clause)
        and not other_site.search(source.sub(" ", clause))
    ]


def _partly_ready(claims: list[str]) -> bool:
    """Every clause that claims ready claims it of some of the files only ("three of the
    files"; not "all five of the files")."""
    return bool(claims) and all(_SOME_OF.search(c) for c in claims)


# A claim about the files the answer does NOT say are being written: that they are not here.
# Read only beside files said to be being written (the scorer checks it against the state at
# the answer: in RCn the other three are local, so "the other three are missing" is false).
OTHERS_NOT_READY = "others:not_ready"
_DENIED_BEFORE = re.compile(r"\b(?:none|no|neither|not)\b[^.;,\n]{0,12}$", re.I)


def _others_regex(owner: str) -> re.Pattern[str]:
    return re.compile(
        r"\b(?:the\s+)?(?:other|others|remaining|rest)\b(?:\s+" + _NUMBER + r")?"
        r"(?:\s+(?:files?|ones?|logs?))?\s+(?:are|is|were|was|remains?|stays?)\s+"
        r"(?:(?:still|only|also|yet)\s+)*(?:missing|absent|unavailable|elsewhere|incomplete"
        r"|not\s+(?:yet\s+)?(?:here|there|local(?:ly)?|ready|available|complete|copied|"
        r"replicated|present|stored|readable)\b"
        r"|(?:at|on)\s+(?:another|the\s+other|a\s+(?:remote|different))\b"
        r"|(?:at|on)\s+" + re.escape(owner) + r")",
        re.I,
    )


def says_others_not_ready(text: str, owner: str) -> bool:
    """The answer says the files it does not say are being written are not here: "the other
    three are missing", "the rest are only at <owner>". "None of the others is missing" is no
    such claim."""
    return any(
        not _DENIED_BEFORE.search(text[: m.start()]) for m in _others_regex(owner).finditer(text)
    )


def read_check(answer: str, local: str, owner: str) -> tuple[str, frozenset[str]]:
    """V4, V5 and the readiness scenarios (RC*, RE*), one vocabulary (rubrics._CHECK), with
    the files said to be still being written and the counts stated for them (`#2`, `#all`).

    SAYS_READY when some sentence claims ready, unless the answer also says files are still
    being written, gives a verdict of not ready ("No.", "not all", "not ready"), or claims
    ready of some files only ("three of the files are complete"). Then, or when it says not
    ready at all: NOT_READY_WRITING if it says some files are still being written,
    NOT_READY_ELSEWHERE if its reason is that the data are at another site, else
    NOT_READY_OTHER. Else OTHER. A negative remark beside a full ready claim ("they have not
    been replicated anywhere else") does not undo it."""
    text = _HELD_UNTIL.sub(r"\1", _plain(answer))
    said = writing_names(answer) | writing_counts(answer)
    other_site = _other_site_regex(owner, local)
    writing = says_writing(answer)
    if writing and says_others_not_ready(text, owner):
        said = said | {OTHERS_NOT_READY}
    not_ready = _says_not_ready(text)
    verdict = bool(_NOT_READY_VERDICT.search(text) or _OPENS_NO.match(text))
    claims = _ready_clauses(text, other_site, owner)
    if claims and not (writing or verdict or _partly_ready(claims)):
        return "SAYS_READY", said
    if not (not_ready or writing):
        return "OTHER", said
    if writing:
        return "NOT_READY_WRITING", said
    if other_site.search(text):
        return "NOT_READY_ELSEWHERE", said
    return "NOT_READY_OTHER", said


def _check_reading(ctx: K2Context, trace: AgentTrace) -> tuple[str, frozenset[str]]:
    read = ctx.decided(lambda: read_check(trace.final_answer, ctx.local_name, ctx.owner_name))
    if isinstance(read, tuple):
        return read[0], frozenset(read[1])
    return "OTHER", frozenset()


_CHECK_WRONG = {
    "SAYS_READY": "CLAIMS READY",
    "NOT_READY_OTHER": "not ready, without the reason",
    "OTHER": "the answer does not say not ready",
}


def verify_v4(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """V4: not ready because the data are at another site (NOT_READY_ELSEWHERE). Saying a file
    is still being written is a false reason here (nothing is)."""
    label, _ = _check_reading(ctx, trace)
    if label == "NOT_READY_ELSEWHERE":
        return _result(True, None, ["not ready, because the data are at another site"])
    why = _CHECK_WRONG.get(label, "says files are still being written, and none is")
    return _result(False, None, [f"{why} ({label})"])


def verify_writing(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """V5 (and RCn, REn): not ready, giving that files are still being written
    (NOT_READY_WRITING). What it says of those files must be true: every file it names as
    being written is one of the two that are (log_20200102.dat and log_20200104.dat), and every
    count it states for them is two. A name is an example ("some, including log_20200102.dat"
    passes); a count is a claim about the whole ("all five are being written" fails). Naming or
    counting none is not required: the brief asks for neither."""
    label, said = _check_reading(ctx, trace)
    parts = []
    if label != "NOT_READY_WRITING":
        why = _CHECK_WRONG.get(label, "not ready, without 'still being written'")
        parts.append(f"{why} ({label})")
    false = writing_claim_problems(said, S2_NAMES, len(ctx.file_names) or 5)
    if OTHERS_NOT_READY in said and _others_here(ctx):
        false.append(
            "says the files not being written are not here, and at the answer the local copy "
            "covered them"
        )
    parts.extend(false)
    ok = label == "NOT_READY_WRITING" and not false
    # a ready said by a tool before the writer finished is the tool's bug, flagged, never the
    # model's failure (design/18: recorded from V7's, V5's, RCn's and REn's traces)
    if violations := k2_violations(ctx):
        ctx.flags.append("k2_violation")
        certain = [v for v in violations if v["level"] == "certain"]
        parts.append(
            f"K2 VIOLATION (tool bug): ready before the writer finished "
            f"({len(certain)} certain, {len(violations) - len(certain)} suspect)"
        )
    return _result(ok, None, parts or ["not ready: files still being written"])


def _others_here(ctx: K2Context) -> bool:
    """At the answer, the local copy covered every file of the trial that is not being written
    (RCn: always; REn: once they were made ready; V5: never, they are at the owner only). The
    scorer always has that snapshot (`score` refuses to judge without it); an oracle called
    without one cannot show the claim false."""
    if ctx.at_answer is None:
        return False
    at = require_usable(ctx.at_answer, "at the answer")
    others = [n for n in ctx.file_names if n not in S2_NAMES]
    return bool(others) and at.all_local_covered(others)


verify_v5 = verify_writing


def _says_ready(ctx: K2Context, answer: str) -> bool:
    other_site = _other_site_regex(ctx.owner_name, ctx.local_name)
    return claims_ready(answer, other_site) or claims_done(answer, other_site)


def read_ready(answer: str, local: str, owner: str) -> str:
    """V6, V7: SAYS_READY when it claims ready or done (not at another site), else
    SAYS_NOT_READY when it says not ready / not yet / failed, else UNCLEAR."""
    other_site = _other_site_regex(owner, local)
    text = _plain(answer)
    if claims_ready(text, other_site) or claims_done(text, other_site):
        return "SAYS_READY"
    if _says_not_ready(text) or _NOT_DONE.search(text):
        return "SAYS_NOT_READY"
    return "UNCLEAR"


def verify_v6(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    claim = (
        ctx.decided(lambda: read_ready(trace.final_answer, ctx.local_name, ctx.owner_name))
        == "SAYS_READY"
    )
    premature, reasons = _end_state_claim(ctx, claim)
    fed = require_usable(ctx.after_settle, "after the settle").all_local_covered(ctx.file_names)
    if not claim:
        return _result(False, fed, ["the answer does not say the files are ready", *reasons])
    if premature:
        ctx.flags.append("premature_claim")
        return _result(False, fed, ["PREMATURE CLAIM: said ready, but " + "; ".join(reasons)])
    return _result(True, fed, ["said ready after the local copy covered all five"])


def verify_v7(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    claim = (
        ctx.decided(lambda: read_ready(trace.final_answer, ctx.local_name, ctx.owner_name))
        == "SAYS_READY"
    )
    premature, reasons = _end_state_claim(ctx, claim, expect_complete_owner=True)
    after = require_usable(ctx.after_settle, "after the settle")
    fed = bool(after.all_local_covered(ctx.file_names) and after.all_owner_covered(ctx.file_names))
    violations = k2_violations(ctx)
    if violations:
        ctx.flags.append("k2_violation")
    note = []
    if violations:
        certain = [v for v in violations if v["level"] == "certain"]
        note.append(
            f"K2 VIOLATION (tool bug): ready:true before the writer finished "
            f"({len(certain)} certain, {len(violations) - len(certain)} suspect)"
        )
    if not claim:
        return _result(False, fed, ["the answer does not say the files are ready", *reasons, *note])
    if premature:
        ctx.flags.append("premature_claim")
        return _result(
            False, fed, ["PREMATURE CLAIM: said ready, but " + "; ".join(reasons), *note]
        )
    return _result(
        True, fed, ["said ready after the writer finished and the files were local", *note]
    )


Oracle = Callable[[K2Context, AgentTrace], OracleResult]
ORACLES: dict[str, Oracle] = {
    "V1": verify_v1,
    "V2": verify_v2,
    "V3": verify_v3,
    "V3i": verify_v3,
    "V3n": verify_v3n,
    "V3o": verify_v3o,
    "V4": verify_v4,
    "V5": verify_v5,
    "V6": verify_v6,
    "V7": verify_v7,
}


def settled(settle: str, snapshot: Snapshot, names: list[str]) -> bool:
    """Has the end state the scenario waits for arrived? (Ends the settle wait early.)"""
    if snapshot.errors():
        return False
    if settle == "local_covered":
        return snapshot.all_local_covered(names)
    if settle == "qos_gone":
        return not snapshot.any_qos(names)
    if settle == "complete":
        return snapshot.all_local_covered(names) and snapshot.all_owner_covered(names)
    # "quiet": nothing is to arrive, so the whole window is waited out
    return settle != "quiet"


# --- schedule_file_replication's scenarios (design/12): SR1, SR1n, SR1o ---------------------
from benchmark.k2val.sr_oracles import SR_ORACLES  # noqa: E402 - registered after the core set

ORACLES.update(SR_ORACLES)


# --- the file-tool scenarios (FA, LC, CD; design/13-15) ---------------------------------------
# Their oracles and answer readers are in file_oracles.py (a leaf module: no import cycle).
from benchmark.k2val.file_oracles import FILE_ORACLES  # noqa: E402

ORACLES.update(FILE_ORACLES)

# --- the stable tools' smoke scenarios (ST*; design/16), in stable_oracles.py -----------------
from benchmark.k2val.stable_oracles import ST_ORACLES  # noqa: E402

ORACLES.update(ST_ORACLES)

# --- the readiness tools' scenarios (RC*, RE*; design/17, design/18), in readiness_oracles.py ---
from benchmark.k2val.readiness_oracles import READINESS_ORACLES  # noqa: E402

ORACLES.update(READINESS_ORACLES)
