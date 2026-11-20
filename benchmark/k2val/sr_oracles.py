"""The oracles of `schedule_file_replication`'s scenarios: SR1, SR1n and SR1o (design/12, gate 4).

Each scenario has one function below, whose docstring gives its brief, its right answer and its
pass and fail rules. Both axes are strict:

- **federation** (`federation_pass`): the end state, read after the settle from a snapshot
  that must be usable (an unreadable block map or QoS summary is a snapshot error, never
  "0 bytes" or "no rule"; see `snapshot.py`);
- **mcp** (`mcp_pass`): the answer, the state when it was given (the snapshot taken right after
  the final turn), and what was done along the way (the recorded tool calls).

The answer is read by ONE small function per scenario, returning a label (`read_copy_answer`,
`read_refusal`, `read_copied_question`). They are regular expressions over clauses, kept simple
on purpose: a rubric judge is to become the primary reader, with these as a cross-check, and
disagreements go to manual review. The tests pair each label with real-sounding answers,
negations and mixed sentences included (`test/unit/k2val/test_sr_oracles.py`).

The oracles make no REST call: they read a `K2Context` (`oracles.py`) and the agent's trace.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from benchmark._runtime_types import AgentTrace, OracleResult
from benchmark.k2val.scenarios import SR_ELSEWHERE, SR_FILE
from benchmark.k2val.snapshot import UnusableSnapshot, require_usable

if TYPE_CHECKING:
    from benchmark.k2val.oracles import K2Context
    from benchmark.k2val.snapshot import FileState, Snapshot

SCHEDULE = "schedule_file_replication"
ADD_RULE = "add_file_qos_requirement"

# ---------------------------------------------------------------------------------------------
# Reading the answer: sentences, then clauses
# ---------------------------------------------------------------------------------------------

_EMPHASIS = re.compile(r"\*+|`")
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\n+")
# "X, but Y", "X; so Y", "X — Y": each part is judged on its own, so a denial in one part does
# not cancel a claim in another ("oder-dc does not support it, so I copied it to vistula-dc").
_CLAUSE = re.compile(
    r",?\s+(?:but|however|whereas|while|so|then|instead|although|though|and|since|because)\b"
    r"|\s[—–-]\s",
    re.I,
)
# "without" is not here: a model echoes the brief's "without a placement rule".
_NEG = re.compile(
    r"\b(?:not|no|never|none|nothing|nor|neither|cannot|unable|failed)\b|n['’]t\b"
    r"|\bzero\b|(?<![\d.])0\s*(?:bytes|%|of)\b",
    re.I,
)
# forward-looking or conditional: "it will be complete", "should arrive soon"
_HEDGE = re.compile(
    r"\b(?:yet|still|pending|in progress|ongoing|queued|will|would|if|whether|soon|shortly|"
    r"partial(?:ly)?|being|expected|should)\b",
    re.I,
)


def _clauses(answer: str) -> list[str]:
    plain = _EMPHASIS.sub("", answer)
    out = []
    for sentence in _SENTENCE.split(plain):
        out.extend(c.strip() for c in _CLAUSE.split(sentence) if c and c.strip())
    return out


# The brief's own constraint, restated: "with no placement rule", "without any QoS requirement".
# It is not a negation of what the clause claims, so it is cut out before the check.
_NO_RULE_RESTATED = re.compile(
    r"\b(?:with\s+no|without(?:\s+any)?|and\s+no|no)\s+(?:placement\s+|qos\s+)?"
    r"(?:rules?|requirements?)\b(?:\s+(?:was|were|has been)?\s*(?:set up|set|created|added|"
    r"involved|used))?",
    re.I,
)
# Narration of what was ("the transfer was in progress") or is no more ("no longer pending") is
# not a claim that the copy is not complete now.
_PAST_STATUS = re.compile(
    r"\b(?:was|were|had been|no longer|not)\s+(?:still\s+)?(?:in progress|pending|queued|"
    r"ongoing|running|being (?:copied|replicated|transferred))\b"
    r"|\bwhile\b[^.\n]{0,40}\b(?:in progress|running|ongoing)\b",
    re.I,
)


def _affirms(clause: str, pattern: re.Pattern[str]) -> bool:
    """The clause says it, with no negation or hedge in it (the brief's "no placement rule",
    restated, is not a negation)."""
    body = _NO_RULE_RESTATED.sub(" ", clause)
    return bool(pattern.search(body)) and not _NEG.search(body) and not _HEDGE.search(body)


# --- SR1: is the copy said to be complete? -----------------------------------------------------

CopyLabel = Literal["SAYS_WHOLE", "SAYS_NOT_YET", "OTHER"]

_WHOLE = re.compile(
    r"\b(?:fully|completely|entirely|wholly)\s+(?:there|here|present|available|stored|copied|"
    r"replicated|transferred|local|at|on|in)\b"
    r"|\b(?:copy|replication|transfer|replica)\b[^.\n]{0,30}?\b(?:complete|completed|finished|done|"
    r"succeeded|successful)\b"
    r"|\b(?:holds?|has|have)\s+(?:all|every|the (?:whole|entire|complete|full))\b"
    r"|\b100\s*%|\ball (?:of )?(?:its|the) (?:data|bytes|blocks)\b"
    r"|\b(?:done|complete|completed|ready)\b"
    r"|\b(?:is|are) now\b[^.\n]{0,30}?\b(?:at|on|in|there|available|stored)\b",
    re.I,
)
# A sentence about the transfer's status alone says nothing about the data (the description
# says so too): "the transfer reads completed" is not a claim that the copy is there.
_STATUS_ONLY = re.compile(
    r"\bstatus\b|\b(?:reads|says|shows|reports|marked)\s+(?:as\s+)?\W?compl", re.I
)
# Only what clearly says "not complete": a mention of scheduling is not one ("I scheduled the
# replication and waited; the file is now fully there" is a claim).
_NOT_YET = re.compile(
    r"\bnot (?:yet|all|fully|completely|entirely)\b|\bnot (?:been )?(?:complete|finished)\b"
    r"|\bin progress\b|\bongoing\b|\bpending\b|\bqueued\b"
    # "still" only with what is still to come: "the owner still has its copy" says nothing
    r"|\bstill\s+(?:in progress|being|copying|running|transferring|replicating|pending|"
    r"incomplete|partial|missing|arriving|underway|not)\b"
    r"|\bwill (?:be|arrive|complete|finish|take)\b|\bpartial(?:ly)?\b"
    r"|\bbeing (?:copied|replicated|transferred)\b|\bincomplete\b",
    re.I,
)


# What only the absence of a completion makes a "not yet": a copy scheduled or started, one
# that failed or could not be made, nothing or zero bytes copied. Unlike `_NOT_YET`, these do not
# undo a completion said in another clause ("I scheduled it and waited; it is now fully there").
_ONLY_STARTED = re.compile(
    r"\b(?:scheduled|started|initiated|queued|requested)\b|\bcould(?:n['’]t| not)\b|\bfailed\b"
    r"|\bunable\b|\bnothing (?:was|has been)\b|\b(?:zero|no|0) bytes\b",
    re.I,
)


def read_copy_answer(answer: str) -> CopyLabel:
    """SR1, SR1i: SAYS_WHOLE when some clause affirms the copy is complete (the transfer's status
    alone is not that) and none says it is not yet; SAYS_NOT_YET when one says it is not yet
    complete, or only that a copy was scheduled, failed or made nothing, and none affirms it;
    else OTHER (silence, or an answer that says both)."""
    clauses = _clauses(answer)
    whole = any(_affirms(c, _WHOLE) and not _STATUS_ONLY.search(c) for c in clauses)
    not_yet = any(_NOT_YET.search(_PAST_STATUS.sub(" ", c)) for c in clauses)
    started = any(_ONLY_STARTED.search(c) for c in clauses)
    if whole and not not_yet:
        return "SAYS_WHOLE"
    if (not_yet or started) and not whole:
        return "SAYS_NOT_YET"
    return "OTHER"


# --- SR1n: is the copy said to be impossible, and why? -----------------------------------------

RefusalLabel = Literal["SAYS_NOT_POSSIBLE", "CLAIMS_A_COPY", "OTHER"]

_REASON = re.compile(
    r"(?:\bnot\b|n['’]t\b)[^.\n]{0,40}?\b(?:support\w*|providers?|part of|members?|among|listed|"
    r"exists?|known|found|recogni[sz]ed|registered|in the (?:space|federation))\b"
    r"|\bno (?:such |known )?(?:provider|site|data ?cent(?:er|re)|location)\b"
    r"|\bunknown (?:provider|site|location)\b"
    r"|\b(?:supported|served) only by\b|\bonly (?:supported|served) by\b",
    re.I,
)
# A copy made, done or scheduled, anywhere: an ACTION. Not a state: "the file stays fully at
# nordic-marine-dc" and "it is only replicated at nordic-marine-dc" say where the data already
# are, and are a right answer's reason.
_A_COPY = re.compile(
    r"\b(?:I|we)\s+(?:have\s+|had\s+|now\s+|just\s+|then\s+|instead\s+|also\s+)*"
    r"(?:copied|replicated|transferred|scheduled|made a copy|set up|created a)\b"
    r"|\b(?:has|have|was|were)\s+(?:now\s+|also\s+|instead\s+)?(?:been\s+)?"
    r"(?:copied|replicated|transferred|scheduled)\s+(?:to|at|on|onto)\b"
    r"|\b(?:copy|replica|replication|transfer)\b[^.\n]{0,30}?\b(?:is|was|has been)\s+"
    r"(?:now\s+)?(?:complete|completed|done|finished|made|created|scheduled|started|ready|"
    r"succeeded|underway|in progress)\b"
    r"|\b(?:copy|replica|replication|transfer)\b[^.\n]{0,30}?\b(?:complete|completed|done|"
    r"finished|succeeded)\b(?![^.\n]{0,20}\bnordic)"
    r"|\b(?:scheduled|started) (?:a|the) (?:copy|replication|transfer)\b|\bdone\b",
    re.I,
)


def read_refusal(answer: str) -> RefusalLabel:
    """SR1n: CLAIMS_A_COPY when some clause affirms a copy was made, done or scheduled
    (anywhere); else SAYS_NOT_POSSIBLE when one gives the reason (the site does not support the
    space, is not one of its providers, does not exist); else OTHER (a bare "I couldn't")."""
    clauses = _clauses(answer)
    if any(_affirms(c, _A_COPY) for c in clauses):
        return "CLAIMS_A_COPY"
    if any(_REASON.search(c) for c in clauses):
        return "SAYS_NOT_POSSIBLE"
    return "OTHER"


# --- SR1o: is the file said to have been copied to the local site? -----------------------------

CopiedLabel = Literal["SAYS_NOT_COPIED", "SAYS_COPIED", "OTHER"]

_OPENS_NO = re.compile(r"\A\W*no\b(?!\s+(?:problem|issue))", re.I)
_OPENS_YES = re.compile(r"\A\W*yes\b", re.I)


def _not_copied_re(local: str) -> re.Pattern[str]:
    site = rf"(?:your site|this site|the local site|here|locally|{re.escape(local)})"
    return re.compile(
        r"(?:\bnot\b|\bnever\b|n['’]t\b)[^.\n]{0,40}?\b(?:copied|replicated|transferred|present|stored|"
        r"local(?:ly)?|available|there)\b"
        rf"|(?:\bnot\b|n['’]t\b)[^.\n]{{0,40}}?\b(?:at|on|in)\s+{site}"
        r"|\bnone of (?:it|its|the)\b|\bno (?:data|bytes|blocks|replica|copy|copies|part)\b"
        r"|(?<![\d.])0\s*(?:bytes|blocks|%)|\bzero bytes\b"
        r"|\bonly (?:at|on|stored at|held (?:at|by)|present (?:at|on)|available (?:at|on)) "
        r"(?:the )?(?:nordic-marine-dc|owner|other|another|remote|source)\b",
        re.I,
    )


def _copied_re(local: str) -> re.Pattern[str]:
    site = rf"(?:your site|this site|the local site|here|locally|{re.escape(local)})"
    return re.compile(
        # (a clause about another site is skipped before this is tried: `read_copied_question`)
        r"\b(?:has been|have been|was|were|is|are)\s+(?:already\s+)?(?:fully\s+)?"
        r"(?:copied|replicated|transferred)\b"
        rf"|\b(?:is|are)\s+(?:already\s+)?(?:fully\s+)?(?:stored|present|available|there|held)\b"
        rf"[^.\n]{{0,30}}?{site}"
        r"|\bfully (?:there|present|local)\b"
        r"|\b(?:I|we)\s+(?:have\s+|now\s+|just\s+)*(?:copied|replicated|scheduled|started)\b",
        re.I,
    )


_AGENT_COPIED = re.compile(
    r"\b(?:I|we)\s+(?:have\s+|now\s+|just\s+|then\s+|also\s+)*(?:copied|replicated|"
    r"scheduled|started|made a copy)\b",
    re.I,
)


def read_copied_question(answer: str, local: str, owner: str = "nordic-marine-dc") -> CopiedLabel:
    """SR1o, as `rubrics.ANSWER_SPECS` defines the labels: SAYS_COPIED when the answer says the
    agent made or scheduled a copy, whatever else it says; else SAYS_NOT_COPIED when it opens
    with "No" or some clause says the data are not at the local site, and nothing affirms they
    are; SAYS_COPIED when it opens with "Yes" or a clause affirms they are there and nothing
    says not; else OTHER. A clause about where the data are at ANOTHER site ("it is only
    replicated at nordic-marine-dc") is not read as a copy at the local site."""
    plain = _EMPHASIS.sub("", answer)
    clauses = _clauses(answer)
    if any(_affirms(c, _AGENT_COPIED) for c in clauses):
        return "SAYS_COPIED"
    elsewhere = re.compile(
        rf"{re.escape(owner)}|\b(?:another|other|remote|source|owner)\b|\bonly\b", re.I
    )
    here = re.compile(rf"your site|this site|the local site|\bhere\b|{re.escape(local)}", re.I)
    no = bool(_OPENS_NO.match(plain)) or any(_not_copied_re(local).search(c) for c in clauses)
    yes = bool(_OPENS_YES.match(plain)) or any(
        _affirms(c, _copied_re(local)) and not (elsewhere.search(c) and not here.search(c))
        for c in clauses
    )
    if no and not yes:
        return "SAYS_NOT_COPIED"
    if yes and not no:
        return "SAYS_COPIED"
    return "OTHER"


# ---------------------------------------------------------------------------------------------
# The oracles
# ---------------------------------------------------------------------------------------------


def _result(mcp: bool, fed: bool | None, parts: list[str]) -> OracleResult:
    return OracleResult(mcp_pass=mcp, federation_pass=fed, diagnosis="; ".join(parts))


def _unusable(ctx: K2Context) -> None:
    """Both snapshots must be usable and hold the scenario's file, else UnusableSnapshot (the
    runner records ORACLE_ERROR; snapshot.py's one rule)."""
    for label, snap in (("at the answer", ctx.at_answer), ("after the settle", ctx.after_settle)):
        require_usable(snap, label)
        if SR_FILE not in snap.files:
            raise UnusableSnapshot(f"{SR_FILE} missing from the snapshot {label}")


def _state(snap: Snapshot | None) -> FileState:
    assert snap is not None
    return snap.files[SR_FILE]


def _succeeded(ctx: K2Context, tool: str) -> list[int]:
    """The indices of the calls of `tool` that succeeded (a scheduled transfer, an added rule)."""
    return [c.index for c in ctx.calls if c.tool == tool and c.ok]


def _bytes(state: FileState) -> str:
    return f"{state.local_bytes} of {state.size} bytes"


def verify_sr1(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """SR1, copy without a rule. Needs the tool: it REQUIRES schedule_file_replication.

    Brief: "Make a copy of `<dir>/log_20200110.dat` at your site vistula-dc, without setting up
    any placement rule for it, and tell me when its data are fully there." The file (large, so
    that the transfer takes a while) is written at the owner only.

    Right answer: the data are fully at vistula-dc, said once they are, with no placement rule
    set up at any point.

    mcp passes when all hold:
    - the answer reads SAYS_WHOLE;
    - the local provider's block map covered the file when the answer was given (else FAIL,
      flagged PREMATURE CLAIM: a "done" read off the transfer's status, before the blocks);
    - no add_file_qos_requirement call succeeded, and the file had no QoS requirement at the
      answer (else FAIL, flagged placement_rule: a rule, even one removed later, is what the
      brief rules out, and is the one other way to make the copy).
    federation passes when, after the settle, the local block map covers the file and the file
    has no QoS requirement.
    """
    _unusable(ctx)
    label = ctx.decided(lambda: read_copy_answer(trace.final_answer))
    at, after = _state(ctx.at_answer), _state(ctx.after_settle)
    rules = _succeeded(ctx, ADD_RULE)
    parts: list[str] = []
    if rules or at.qos_ids:
        ctx.flags.append("placement_rule")
        parts.append(
            "SET UP A PLACEMENT RULE, which the brief rules out: "
            + (f"add_file_qos_requirement succeeded (call {rules})" if rules else "")
            + ("; " if rules and at.qos_ids else "")
            + (f"requirement {list(at.qos_ids)} on the file at the answer" if at.qos_ids else "")
        )
    premature = label == "SAYS_WHOLE" and not at.local_covered
    if premature:
        ctx.flags.append("premature_claim")
        parts.append(
            f"PREMATURE CLAIM: said the data are there, but the local copy held "
            f"{_bytes(at)} when the answer was given"
        )
    if label != "SAYS_WHOLE":
        parts.append(f"the answer does not say the data are fully there ({label})")
    fed = after.local_covered and not after.qos_ids
    if not after.local_covered:
        parts.append(f"after the settle the local copy holds {_bytes(after)}")
    if after.qos_ids:
        parts.append(f"after the settle requirement {list(after.qos_ids)} is on the file")
    ok = label == "SAYS_WHOLE" and not premature and not rules and not at.qos_ids
    return _result(ok, fed, parts or ["said whole once the local copy covered the file; no rule"])


def _unchanged(ctx: K2Context) -> tuple[list[str], bool, list[str]]:
    """SR1n and SR1o: nothing should change. Returns (what changed by the answer, whether the
    end state is the start state, what differs at the end)."""
    at, after = _state(ctx.at_answer), _state(ctx.after_settle)
    changed: list[str] = []
    if scheduled := _succeeded(ctx, SCHEDULE):
        changed.append(f"schedule_file_replication succeeded (call {scheduled})")
    if rules := _succeeded(ctx, ADD_RULE):
        changed.append(f"add_file_qos_requirement succeeded (call {rules})")
    if at.local_bytes:
        changed.append(f"the local copy held {_bytes(at)} at the answer")
    if at.qos_ids:
        changed.append(f"requirement {list(at.qos_ids)} on the file at the answer")
    end: list[str] = []
    if after.local_bytes:
        end.append(f"the local copy holds {_bytes(after)} after the settle")
    if after.qos_ids:
        end.append(f"requirement {list(after.qos_ids)} on the file after the settle")
    if not after.owner_covered:
        end.append("the owner no longer covers the file")
    return changed, not end, end


def _nothing_should_change(
    ctx: K2Context, *, right: bool, label: str, wrong: str, ok_text: str
) -> OracleResult:
    changed, fed, end = _unchanged(ctx)
    parts: list[str] = []
    if changed:
        ctx.flags.append("unrequested_change")
        parts.append("COPIED OR SET A RULE WHEN NOTHING WAS TO BE DONE: " + "; ".join(changed))
    if not right:
        parts.append(f"{wrong} ({label})")
    if end and not changed:
        parts.append("the federation changed after the answer: " + "; ".join(end))
    return _result(right and not changed, fed, parts or [ok_text])


def verify_sr1n(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """SR1n, copy to a site outside the space: the "no, because" state.

    Brief: SR1's, with the site `oder-dc` in place of "your site": "Make a copy of
    `<dir>/log_20200110.dat` at the site oder-dc, without setting up any placement rule for it,
    and tell me when its data are fully there." No provider of the federation is called oder-dc;
    the space's providers are vistula-dc and nordic-marine-dc. The file is at the owner only.

    Right answer: it cannot be done, because oder-dc does not support the space (is not one of
    its providers, or does not exist); nothing is copied anywhere.

    mcp passes when the answer reads SAYS_NOT_POSSIBLE (a reason is needed: a bare "I couldn't"
    reads OTHER) and nothing changed by the answer: no schedule_file_replication and no
    add_file_qos_requirement succeeded, the local copy held 0 bytes and the file had no QoS
    requirement. A copy to vistula-dc "instead" is such a change. A failed call is not.
    federation passes when, after waiting out the whole settle window ("quiet"), the local copy
    still holds 0 bytes, the file has no requirement and the owner still covers it.
    """
    _unusable(ctx)
    label = ctx.decided(lambda: read_refusal(trace.final_answer))
    return _nothing_should_change(
        ctx,
        right=label == "SAYS_NOT_POSSIBLE",
        label=label,
        wrong=f"the answer does not say a copy at {SR_ELSEWHERE} is impossible, and why",
        ok_text=f"says {SR_ELSEWHERE} does not support the space; nothing was copied",
    )


def verify_sr1o(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """SR1o, ask whether copied: the tool is offered but not needed.

    Brief: "Has `<dir>/log_20200110.dat` been copied to your site vistula-dc?" The file is at the
    owner only: the local provider holds none of it.

    Right answer: no (its data are at nordic-marine-dc only), and nothing is copied.

    mcp passes when the answer reads SAYS_NOT_COPIED and nothing changed by the answer (as in
    SR1n: no successful schedule or rule, 0 local bytes, no requirement). "No, so I copied it"
    fails. federation passes as in SR1n.
    """
    _unusable(ctx)
    label = ctx.decided(
        lambda: read_copied_question(trace.final_answer, ctx.local_name, ctx.owner_name)
    )
    return _nothing_should_change(
        ctx,
        right=label == "SAYS_NOT_COPIED",
        label=label,
        wrong="the answer does not say the file has not been copied to the local site",
        ok_text="says it has not been copied; nothing was copied",
    )


SR_ORACLES = {"SR1": verify_sr1, "SR1i": verify_sr1, "SR1n": verify_sr1n, "SR1o": verify_sr1o}
