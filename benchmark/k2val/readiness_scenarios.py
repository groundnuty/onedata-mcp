"""The scenarios of the readiness tools' promotion (design/17 `check_readiness`, design/18
`ensure_readiness`).

V4-V7 (design/08) stay as they were, as supporting evidence: they offer the K2 set, where the
general read tools (`get_file_distribution`, `get_file_attributes`) answer V4 and V5 too, and
`add_file_qos_requirement` or `schedule_file_replication` do V6 and V7, so none of them
REQUIRES a readiness tool (design/10, gate 4). The scenarios here do, by what they offer:

- `RC_TOOLS` (check_readiness): no tool that reads where a file's blocks are, its local
  replica, its QoS state or its transfers, and not ensure_readiness. Two write tools stay, so
  that the restraint checks mean something.
- `RE_TOOLS` (ensure_readiness): no tool that makes a copy or a rule
  (`add_file_qos_requirement`, `schedule_file_replication`), none that reads the state above,
  and not check_readiness: only ensure_readiness can make the data ready, and only its answer
  says whether they are, and why not.

Each scenario is one `ReadinessScenario` literal holding what a reviewer needs: the brief, the
fixture, the tools, the coverage kind design/10 asks for, the right answer and the rules. The
answer readers and oracles are in `readiness_oracles.py`; `scenarios.py` registers these
scenarios. Ids: RC, RCn, RCo, RCp, RCi (check_readiness); RE, REn, REo, REp
(ensure_readiness).

Briefs are neutral: they name no tool and give no advice on how to work. Identical briefs share
one vocabulary (`rubrics._CHECK`, with V4 and V5), whose passing label is never listed first.

The id form of the extent is a space's id: a dataset may be given as a space by its id, or as a
directory path whose first segment is the space's id (a file's or a directory's id is not taken,
and gets a hint). RCi gives the trial directory so, `/<space id>/<dir>`; the extent is shared by
both tools, so one id variant serves both. The other variants: a date range over a directory
(RC, RCn, RE, REn) and files given one by one by absolute path (RCp, REp), where the files not
asked about are in another state, so that only a scoped extent gives the right answer.

This module imports only the fixture types, so the registries can import it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from benchmark.k2val.fixtures import FileSpec, FixtureSpec

# The tools that read what readiness is made of (blocks, the local replica, QoS state,
# transfers) or list files: withheld from both requiring sets.
READS_THAT_ANSWER = (
    "get_file_attributes",
    "get_file_distribution",
    "get_file_qos_summary",
    "get_qos_requirement",
    "get_transfer",
    "list_children",
    "list_files_recursively",
    "list_space_transfers",
)
RC_TOOLS = (
    "add_file_qos_requirement",
    "check_readiness",
    "get_file_metadata",
    "list_space_providers",
    "schedule_file_replication",
)
RC_EXCLUDED = (*READS_THAT_ANSWER, "ensure_readiness")
RE_TOOLS = ("ensure_readiness", "get_file_metadata", "list_space_providers")
RE_EXCLUDED = (
    *READS_THAT_ANSWER,
    "add_file_qos_requirement",
    "check_readiness",
    "schedule_file_replication",
)

FIVE = ("20200101", "20200102", "20200103", "20200104", "20200105")
WRITING = ("20200102", "20200104")  # the files still being written in RCn and REn (as V5's)
ASKED = ("20200101", "20200103", "20200105")  # the files RCp and REp ask about
# REn's writer finishes long after any trial ends (20 min wall cap): the files cannot become
# complete while the agent works. The teardown stops it.
WRITER_AFTER_THE_TRIAL_S = 40 * 60.0

# V4's and V5's brief, word for word (a test checks): RC and RCn share their vocabulary.
BRIEF_CHECK = (
    "Are the log files in `{dir}` for 1–5 January 2020 complete and readable at your site? "
    "If not, why not?"
)
# RC's brief with the directory given by its space's id (RCi).
BRIEF_CHECK_BY_ID = (
    "Are the log files in `/{ids[space]}/{dirname}` for 1–5 January 2020 complete and readable "
    "at your site? If not, why not?"
)
BRIEF_CHECK_FILES = (
    "Are `{dir}/log_20200101.dat`, `{dir}/log_20200103.dat` and `{dir}/log_20200105.dat` "
    "complete and readable at your site? If not, why not?"
)
# A bound on the wait, so that a state that cannot become ready (REn) ends the trial; the same
# words in RE, so that the brief does not tell the two apart.
BRIEF_ENSURE = (
    "Make the log files in `{dir}` for 1–5 January 2020 complete and readable at your site, "
    "waiting no more than five minutes in all. Then tell me whether they are, and if not, why "
    "not."
)
BRIEF_ENSURE_FILES = (
    "Make `{dir}/log_20200101.dat`, `{dir}/log_20200103.dat` and `{dir}/log_20200105.dat` "
    "complete and readable at your site, waiting no more than five minutes in all. Then tell "
    "me whether they are, and if not, why not."
)


@dataclass(frozen=True)
class ReadinessScenario:
    """One scenario of the readiness tools, with what a reviewer needs beside it."""

    id: str
    title: str
    tool: str  # the tool whose promotion it serves (TOOL_UNDER_TEST)
    # what design/10 asks of a tool's scenarios: "requires", "no, because",
    # "offered, not needed", "variant: ...", and for ensure_readiness the end state
    coverage: str
    answer: str  # the right answer, in words
    rules: str  # how the oracle passes or fails a trial
    # The brief and fixture, or `same_as`: those of an earlier scenario, word for word
    brief: str = ""
    fixture: FixtureSpec | None = None
    same_as: str | None = None
    tools: tuple[str, ...] | None = None  # None: the panel's K2 set
    excluded_tools: tuple[str, ...] = ()
    settle: Literal["quiet", "local_covered"] = "quiet"
    restraint: bool = False
    settle_s: float | None = 60.0
    # the files the brief asks about (None: every file of the fixture)
    asked: tuple[str, ...] | None = None


def _files(**kinds: str) -> tuple[FileSpec, ...]:
    """The five days, each `s0` unless named: `_files(d20200102="s2")`."""
    return tuple(FileSpec(d, kinds.get(f"d{d}", "s0")) for d in FIVE)  # type: ignore[arg-type]


def _names(days: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"log_{d}.dat" for d in days)


READINESS_SCENARIOS: dict[str, ReadinessScenario] = {}

# --- check_readiness (design/17) ---------------------------------------------------------------

_CHECK_REQUIRED = (
    "Required: a successful check_readiness whose extent selects every file asked about (by "
    "the tool's own rules: a dataset, file names or paths, date ranges)."
)
_RESTRAINT = (
    " Nothing should change (restraint, 60 s): a write fails the trial, and the files' local "
    "blocks and rules are compared with the start at the answer and after the window."
)

RC = ReadinessScenario(
    id="RC",
    title="are the files ready (they are)",
    tool="check_readiness",
    coverage="requires: no other offered tool reads blocks, replicas, QoS state or transfers",
    answer="yes: all five are complete and readable at vistula-dc",
    rules="Pass when the answer reads SAYS_READY. " + _CHECK_REQUIRED + _RESTRAINT,
    brief=BRIEF_CHECK,
    fixture=FixtureSpec(_files(**{f"d{d}": "s0_local" for d in FIVE})),
    tools=RC_TOOLS,
    excluded_tools=RC_EXCLUDED,
    restraint=True,
)
RCN = ReadinessScenario(
    id="RCn",
    title="are the files ready (two are still being written)",
    tool="check_readiness",
    coverage="no, because: two files are still being written at their owner",
    answer="no: log_20200102.dat and log_20200104.dat are still being written (the other three "
    "are ready)",
    rules=(
        "Pass when the answer reads NOT_READY_WRITING and what it says of the files being "
        "written is true: a file it names is one of the two, a count it states is two (V5's "
        "rule: a name is an example, a count a claim about the whole). " + _CHECK_REQUIRED
    )
    + _RESTRAINT,
    brief=BRIEF_CHECK,
    fixture=FixtureSpec(
        _files(
            d20200101="s0_local",
            d20200102="s2",
            d20200103="s0_local",
            d20200104="s2",
            d20200105="s0_local",
        ),
        writer_finish_s=20 * 60.0,
    ),
    tools=RC_TOOLS,
    excluded_tools=RC_EXCLUDED,
    restraint=True,
)
RCO = ReadinessScenario(
    id="RCo",
    title="which files are fully stored (the tool offered, not needed)",
    tool="check_readiness",
    coverage="offered, not needed: get_file_attributes or get_file_distribution answers it per "
    "file; check_readiness answers for the whole set, and lists at most three examples per "
    "reason (four files are not local)",
    answer="log_20200102.dat and log_20200105.dat (V1's right answer)",
    rules="V1's: the set of names the answer gives is exactly the two locally stored files."
    + _RESTRAINT
    + " ensure_readiness is offered: a call of it is such a write.",
    same_as="V1",
    restraint=True,
)
RCP = ReadinessScenario(
    id="RCp",
    title="are three files given by path ready (they are; the other two are not)",
    tool="check_readiness",
    coverage="variant: files given one by one by absolute path; the two not asked about are at "
    "another site only, so an extent of the whole directory or the five days answers not ready",
    answer="yes: the three files asked about are complete and readable at vistula-dc",
    rules="Pass when the answer reads SAYS_READY. " + _CHECK_REQUIRED + _RESTRAINT,
    brief=BRIEF_CHECK_FILES,
    fixture=FixtureSpec(
        _files(d20200101="s0_local", d20200103="s0_local", d20200105="s0_local"),
    ),
    tools=RC_TOOLS,
    excluded_tools=RC_EXCLUDED,
    restraint=True,
    asked=_names(ASKED),
)
RCI = ReadinessScenario(
    id="RCi",
    title="are the files ready, the directory given under its space's id (they are)",
    tool="check_readiness",
    coverage="variant: the dataset given by its space's id, `/<space id>/<dir>` (the id form of "
    "`extent`; a file's or directory's id is not taken)",
    answer="yes: all five are complete and readable at vistula-dc",
    rules=(
        "Pass when the answer reads SAYS_READY. Required: a successful check_readiness whose "
        "extent gives the dataset by the space's id (not its name) and that established every "
        "file asked about." + _RESTRAINT
    ),
    brief=BRIEF_CHECK_BY_ID,
    fixture=FixtureSpec(_files(**{f"d{d}": "s0_local" for d in FIVE}), space_by_id=True),
    tools=RC_TOOLS,
    excluded_tools=RC_EXCLUDED,
    restraint=True,
)
READINESS_SCENARIOS.update({s.id: s for s in (RC, RCN, RCO, RCP, RCI)})

# --- ensure_readiness (design/18) --------------------------------------------------------------

_ENSURE_REQUIRED = (
    "Required: a successful ensure_readiness whose extent selects every file asked about."
)
# The brief's bound on the wait, scored in RE, REn and REp alike (`scoring.waited_s`).
_WAIT = (
    " The wait: the union of the agent's ensure_readiness calls' recorded work intervals (calls "
    "that overlap counted once) is at most the brief's five minutes plus a tolerance of 20 s, "
    "once (one final read-back, which the tool takes after its deadline_s); more fails "
    "(WAITED TOO LONG)."
)
_END_STATE = (
    " At the answer, every file asked about is covered by the local blocks and held there: a "
    "QoS requirement it did not have at the start, put on the file itself (not inherited) and "
    "keeping a copy at your provider, read from the rule and the storages its expression "
    "matches (the hold); a ready said before is a PREMATURE CLAIM. The federation axis: the "
    "same, read again after the whole 60 s quiet window."
)

RE = ReadinessScenario(
    id="RE",
    title="make the files ready (they can be)",
    tool="ensure_readiness",
    coverage="requires: no other offered tool makes a copy or a rule, or reads the state",
    answer="yes: all five are now complete and readable at vistula-dc (and held there)",
    rules="Pass when the answer reads SAYS_READY. " + _ENSURE_REQUIRED + _END_STATE + _WAIT,
    brief=BRIEF_ENSURE,
    # the snapshots read each rule on a file: whether it is on the file and keeps a copy here
    fixture=FixtureSpec(_files(), rules_in_snapshots=True),
    tools=RE_TOOLS,
    excluded_tools=RE_EXCLUDED,
)
REN = ReadinessScenario(
    id="REn",
    title="make the files ready (two are still being written)",
    tool="ensure_readiness",
    coverage="no, because: two files are still being written, and stay so for the whole trial",
    answer="no: log_20200102.dat and log_20200104.dat are still being written, so they cannot "
    "be made complete within the five minutes (the other three can be, and are)",
    rules=(
        "Pass when the answer reads NOT_READY_WRITING, with what it says of the files being "
        "written true (V5's rule) and what it says of the other three true. A ready is a wrong "
        "answer. "
        + _ENSURE_REQUIRED
        + " The federation axis, read at the answer (the writes go on, so not after a window): "
        "the three complete files covered by the local blocks and held there (as RE's), and "
        "the two being written still incomplete at their owner." + _WAIT + " Writes stay inside "
        "the trial (the shared check)."
    ),
    brief=BRIEF_ENSURE,
    fixture=FixtureSpec(
        _files(d20200102="s2", d20200104="s2"),
        writer_finish_s=WRITER_AFTER_THE_TRIAL_S,
        rules_in_snapshots=True,
    ),
    tools=RE_TOOLS,
    excluded_tools=RE_EXCLUDED,
)
REO = ReadinessScenario(
    id="REo",
    title="copy a file without a placement rule (the tool offered, not needed)",
    tool="ensure_readiness",
    coverage="offered, not needed: schedule_file_replication makes the copy; ensure_readiness "
    "would place a QoS requirement (a hold), which the brief rules out",
    answer="SR1's: the data are fully at vistula-dc, read from the block map, with no rule",
    rules="SR1's (design/12): SAYS_WHOLE once the local blocks cover the file, read after the "
    "last schedule; no QoS requirement on the file at the answer or after the window. "
    "Required: schedule_file_replication of the file to the local provider.",
    same_as="SR1",
    settle="local_covered",
    settle_s=None,
)
REP = ReadinessScenario(
    id="REp",
    title="make three files given by path ready (the other two stay as they are)",
    tool="ensure_readiness",
    coverage="variant: files given one by one by absolute path; the end state also asks that "
    "the two files not asked about are left alone (no copy, no hold)",
    answer="yes: the three files asked about are now complete and readable at vistula-dc",
    rules="Pass when the answer reads SAYS_READY. "
    + _ENSURE_REQUIRED
    + _END_STATE
    + " The two files not asked about keep their start state (no local blocks, no rule) at the "
    "answer and after the window." + _WAIT,
    brief=BRIEF_ENSURE_FILES,
    fixture=FixtureSpec(_files(), rules_in_snapshots=True),
    tools=RE_TOOLS,
    excluded_tools=RE_EXCLUDED,
    asked=_names(ASKED),
)
READINESS_SCENARIOS.update({s.id: s for s in (RE, REN, REO, REP)})
