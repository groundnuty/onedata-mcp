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
from benchmark.k2val.scenarios import S2_DAYS
from benchmark.k2val.snapshot import Snapshot
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


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------

_NEG = re.compile(
    r"\b(?:not|no|never|none|cannot|without|partial(?:ly)?|incomplete|missing|absent|except"
    r"|exclud\w*)\b|n['’]t\b|\bonly at\b|(?<![\d.])0(?:\.0+)?\s*%|\b0 bytes\b|\bzero\b",
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


def claimed_names(answer: str, offered: tuple[str, ...] = ()) -> set[str]:
    """The file names an answer states as holding (V1): names on lines or clauses that do not
    disclaim them, outside a "not ..." section. Same three ideas as `extract_paths`.

    The brief lists every file (`offered`, in the brief's order), and an answer may restate
    that list ("Of log_..., log_..., ..., the fully stored ones are ..."). The restated list is
    removed before reading: it is not a claim, and claiming every file would fail either way."""
    restated = _restated_list(offered)
    claimed: set[str] = set()
    in_exclusion = False
    for raw in answer.splitlines():
        line = raw.strip()
        if restated:
            line = restated.sub(" ", line)
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

_DONE_POS = re.compile(
    r"\b(?:done|complete[d]?|ready|readable|replicated|copied|fully (?:there|replicated|copied|"
    r"available|stored)|available|in place|finished|succeeded|successful(?:ly)?|"
    r"now (?:at|on|in|available|there|stored))\b",
    re.I,
)
_DONE_NEG = re.compile(
    r"\b(?:not|no|never|yet to|still|pending|in progress|ongoing|scheduled|started|will|until|"
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
        for s in _SENTENCE_SPLIT.split(answer)
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
    r"|\bunavailable\b|\bmissing\b|\bonly (?:at|on|in|available)\b",
    re.I,
)
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
        _REMOVED_POS.search(s) and not _REMOVED_NEG.search(s) for s in _SENTENCE_SPLIT.split(answer)
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
    snap = ctx.at_answer
    if snap is None or snap.errors():
        ctx.flags.append("snapshot_error")
        return False, [
            "no usable snapshot at the answer: " + "; ".join(snap.errors() if snap else ["none"])
        ]
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
    fed = ctx.after_settle.all_local_covered(ctx.file_names) if ctx.after_settle else False
    if "snapshot_error" in ctx.flags:
        return _result(False, None, reasons)
    if not claim:
        return _result(False, fed, ["the answer does not say the data are there", *reasons])
    if premature:
        ctx.flags.append("premature_claim")
        return _result(False, fed, ["PREMATURE CLAIM: said done, but " + "; ".join(reasons)])
    return _result(True, fed, ["said done after the local copy covered the file"])


def verify_v3(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    snap = ctx.after_settle
    if snap is None or snap.errors():
        ctx.flags.append("snapshot_error")
        return _result(
            False, None, ["no usable snapshot: " + "; ".join(snap.errors() if snap else ["none"])]
        )
    fed = not snap.any_qos(ctx.file_names)
    claim = claims_removed(trace.final_answer)
    parts = ["the file has no QoS requirement" if fed else "a QoS requirement is still on the file"]
    parts.append(
        "the answer says it was removed" if claim else "the answer does not say it was removed"
    )
    return _result(claim, fed, parts)


def verify_v4(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    answer = trace.final_answer
    other_site = _other_site_regex(ctx.owner_name, ctx.local_name)
    not_ready = bool(_NOT_READY.search(answer))
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
    answer = trace.final_answer
    not_ready = bool(_NOT_READY.search(answer))
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
    fed = ctx.after_settle.all_local_covered(ctx.file_names) if ctx.after_settle else False
    if "snapshot_error" in ctx.flags:
        return _result(False, None, reasons)
    if not claim:
        return _result(False, fed, ["the answer does not say the files are ready", *reasons])
    if premature:
        ctx.flags.append("premature_claim")
        return _result(False, fed, ["PREMATURE CLAIM: said ready, but " + "; ".join(reasons)])
    return _result(True, fed, ["said ready after the local copy covered all five"])


def verify_v7(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    claim = _says_ready(ctx, trace.final_answer)
    premature, reasons = _end_state_claim(ctx, claim, expect_complete_owner=True)
    after = ctx.after_settle
    fed = bool(
        after
        and after.all_local_covered(ctx.file_names)
        and after.all_owner_covered(ctx.file_names)
    )
    violations = k2_violations(ctx)
    if violations:
        ctx.flags.append("k2_violation")
    if "snapshot_error" in ctx.flags:
        return _result(False, None, reasons)
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
    return True
