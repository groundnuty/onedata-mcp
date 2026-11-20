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
_COUNT_TWO = re.compile(r"\b(?:two|2)\s+(?:of\s+(?:the\s+)?)?(?:\w+\s+){0,3}files?\b", re.I)


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
    r"|\bnothing to (?:remove|delete)\b|\bthere (?:is|are|was|were) none\b",
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
    r"[^.\n]{0,30}?\b" + _RULE_NOUN + r"\b",
    re.I,
)
_OPENS_YES = re.compile(r"\A\W*yes\b", re.I)
_DENIES = re.compile(r"\b(?:not|never|neither)\b|n['’]t\b", re.I)


def _sentences(answer: str) -> list[str]:
    return _SENTENCE_SPLIT.split(_plain(answer))


def says_no_rule(answer: str) -> bool:
    """V3n: the answer says the file has no rule (so there is nothing to remove)."""
    return bool(_NO_RULE.search(_plain(answer)))


def _removal_in(sentence: str) -> bool:
    """A removal verb that nothing before it negates or hedges, and that does not remove
    nothing ("removed nothing")."""
    for m in _REMOVED_POS.finditer(sentence):
        before = sentence[: m.start()]
        if _REMOVED_NEG.search(before) or _NO_RULE.search(before):
            continue
        if _NOT_A_REMOVAL.search(before) or _REMOVED_NOTHING.match(sentence, m.end()):
            continue
        return True
    return False


def claims_a_removal(answer: str) -> bool:
    """Some sentence says a rule was removed. Unlike `claims_removed` (V3), a removal verb
    preceded by "no rule", "nothing", a negation or a hedge is not a claim; one followed by
    "the file has no QoS requirements now" still is."""
    return any(_removal_in(s) for s in _sentences(answer))


def says_has_rule(answer: str) -> bool:
    """V3o: the answer says yes, a rule keeps the file there."""
    if _OPENS_YES.match(_plain(answer)):
        return True
    return any(
        _HAS_RULE.search(s) and not _NO_RULE.search(s) and not _DENIES.search(s)
        for s in _sentences(answer)
    )


# ---------------------------------------------------------------------------
# K2 violations (from the recorded calls)
# ---------------------------------------------------------------------------


def k2_violations(ctx: K2Context) -> list[dict]:
    """A readiness tool that answered `ready: true` before the background writer had written
    the last part of a file: a K2 violation, a bug of the tool and not of the model.

    The writer is on a fixed schedule, so the last part cannot start before `t0 + finish_s`
    (`due`). `certain`: the call ended before `due`, so the data were certainly incomplete.
    `suspect`: it ended later but before the last write returned (or the writer never got
    there before the trial ended)."""
    writer = ctx.writer
    if not writer or writer.get("t0") is None or writer.get("finish_s") is None:
        return []
    due = writer["t0"] + writer["finish_s"]
    done = writer.get("last_write_done_at")
    out: list[dict] = []
    for call in ctx.calls:
        if call.tool not in ("ensure_readiness", "check_readiness") or not call.ok:
            continue
        try:
            body = json.loads(call.result_text or "")
        except ValueError:
            continue
        if not (isinstance(body, dict) and body.get("ready") is True):
            continue
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


def verify_v1(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    got = claimed_names(trace.final_answer, tuple(ctx.file_names))
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


def verify_v2(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    claim = claims_done(trace.final_answer)
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
    claim = claims_removed(trace.final_answer)
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
    answer = trace.final_answer
    return _v3_variant(
        ctx,
        right=says_no_rule(answer),
        wrong_answer=["the answer does not say there is no rule to remove"],
        claim=claims_a_removal(answer),
        ok_text="says there is no rule to remove; the federation is unchanged",
    )


def verify_v3o(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """A question the QoS state answers (yes, a rule keeps the file), with the removal tool
    offered: the answer says yes, and the rule is still there."""
    answer = trace.final_answer
    return _v3_variant(
        ctx,
        right=says_has_rule(answer),
        wrong_answer=["the answer does not say a rule keeps the file there"],
        claim=claims_a_removal(answer),
        ok_text="says a rule keeps the file there; the rule is untouched",
    )


def _says_not_ready(answer: str) -> bool:
    return bool(_NOT_READY.search(answer) or _OPENS_NO.match(answer))


def verify_v4(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    answer = _plain(trace.final_answer)
    other_site = _other_site_regex(ctx.owner_name, ctx.local_name)
    not_ready = _says_not_ready(answer)
    reason = bool(other_site.search(answer))
    ready_claim = claims_ready(answer, other_site)
    parts = []
    if ready_claim:
        parts.append("CLAIMS READY")
    if not not_ready:
        parts.append("the answer does not say not ready")
    if not reason:
        parts.append("no reason meaning 'at another site/provider'")
    ok = not_ready and reason and not ready_claim
    return _result(ok, None, parts or ["not ready, because the data are at another site"])


def verify_v5(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    answer = _plain(trace.final_answer)
    not_ready = _says_not_ready(answer)
    reason = bool(_BEING_WRITTEN.search(answer))
    names = all(n in answer for n in S2_NAMES) or bool(_COUNT_TWO.search(answer))
    ready_claim = claims_ready(answer, _other_site_regex(ctx.owner_name, ctx.local_name))
    parts = []
    if ready_claim:
        parts.append("CLAIMS READY")
    if not not_ready:
        parts.append("the answer does not say not ready")
    if not reason:
        parts.append("a plain 'not ready' without the reason 'still being written'")
    if not names:
        parts.append("does not name or count the two files being written")
    ok = not_ready and reason and names and not ready_claim
    return _result(ok, None, parts or ["not ready, two files still being written"])


def _says_ready(ctx: K2Context, answer: str) -> bool:
    other_site = _other_site_regex(ctx.owner_name, ctx.local_name)
    return claims_ready(answer, other_site) or claims_done(answer, other_site)


def verify_v6(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    claim = _says_ready(ctx, trace.final_answer)
    premature, reasons = _end_state_claim(ctx, claim)
    fed = require_usable(ctx.after_settle, "after the settle").all_local_covered(ctx.file_names)
    if not claim:
        return _result(False, fed, ["the answer does not say the files are ready", *reasons])
    if premature:
        ctx.flags.append("premature_claim")
        return _result(False, fed, ["PREMATURE CLAIM: said ready, but " + "; ".join(reasons)])
    return _result(True, fed, ["said ready after the local copy covered all five"])


def verify_v7(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    claim = _says_ready(ctx, trace.final_answer)
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
