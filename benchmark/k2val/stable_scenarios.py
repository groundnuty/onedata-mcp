"""The smoke scenarios of the stable tools (design/16): PPAM's scenarios ported to the panel.

design/10 ("Staying stable") asks that a stable tool whose description wording, error hints or
result shape changes re-run "the tool's scenarios on the smoke pair at K=3". Eight such changes
are owed. Their tools were validated by the earlier benchmark (`benchmark/scenarios.py`, 18
scenarios against another federation), so the scenarios that exercised each tool there are
ported here, adapted to the validation federation (`benchmark/valfed/layout.toml`: two
providers, data written at `owner`, the agent at `local`). `SMOKE` maps every owed change to its
scenarios: DIRECT ones, where the task requires the tool, and SUPPORTING ones (existing panel
scenarios), where the tool is only the natural one or the oracle asks for its call.

Each new scenario below is one `StableScenario` literal holding what a reviewer needs: the
brief, the fixture, the tool it serves, the PPAM scenario(s) it derives from, the right answer
and the rules. All but ST7 are offered the panel's K1 set (all eight tools are in it, and its
surface hash is recorded); ST7 is offered `ST7_TOOLS`, which leaves out every other tool that
names a provider. The answer readers, oracles, required calls and shape checks are in
`stable_oracles.py`; the judge's label sets in `rubrics.py`; `scenarios.py` registers them.

Ids: ST1..ST7, ST4i. Each brief is neutral: it names no tool and gives no advice on how to work.

This module imports only the fixture types, so the registries can import it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from benchmark.k2val.fixtures import FileSpec, FixtureSpec


@dataclass(frozen=True)
class StableScenario:
    """One ported scenario, with what a reviewer needs beside it."""

    id: str
    title: str
    tool: str  # the stable tool whose smoke re-run it serves (TOOL_UNDER_TEST)
    ppam: tuple[str, ...]  # the earlier benchmark's scenario(s) it is ported from
    # what design/10 asks of a tool's scenarios: "requires", or "variant: ..."
    coverage: str
    brief: str
    fixture: FixtureSpec
    answer: str  # the right answer, in words
    rules: str  # how the oracle passes or fails a trial
    # "quiet": the whole settle window is waited out, then the federation is read again
    settle: Literal["quiet", "none"]
    # Nothing should change (scoring.py's restraint checks): every read-only one offered a set
    # with write tools.
    restraint: bool
    settle_s: float | None = 60.0
    tools: tuple[str, ...] | None = None  # None: the panel's K1 set
    excluded_tools: tuple[str, ...] = ()


ST_SCENARIOS: dict[str, StableScenario] = {}


# --- ST1: add_file_qos_requirement (PPAM A5, P3) ---------------------------------------------
#
# A5 asked to attach a rule "replicated on at least 2 EU providers", P3 to add one and confirm it
# took effect. Our federation has two providers and no storage attributes (no country or geo
# tag), and the owner already holds the data, so the port asks for a rule that keeps the file at
# the agent's own site: what the reworded `expression` field is about (`providerId=<id>` is
# always safe; admin tags work only if set). P3's polling instructions are workflow advice, which
# a neutral brief does not give: the port asks for the end state, judged at the answer (a
# premature claim) and after the whole settle window. Whether a rule keeps a copy at the site is
# judged by its meaning: each rule on the file is evaluated against the space's storages
# (`evaluate_qos_expression`, read into every snapshot: `rules_in_snapshots`).

ST1_FILE = "log_20200111.dat"
ST1 = StableScenario(
    id="ST1",
    title="keep a file at the site by a placement rule",
    tool="add_file_qos_requirement",
    ppam=("A5", "P3"),
    coverage="requires: no other offered tool sets up a placement rule",
    brief="Set up a placement rule that keeps `{dir}/log_20200111.dat` at your site {local}, "
    "and tell me when its data are fully there.",
    fixture=FixtureSpec((FileSpec("20200111", "s0"),), rules_in_snapshots=True),
    answer="done: a rule (e.g. providerId=<vistula-dc's id>) keeps the file at vistula-dc, and "
    "its data are fully there",
    rules=(
        "Pass when the answer reads SAYS_DONE AND, at the answer, a rule on the file that was "
        "not there at the start keeps a copy at the site, and the local blocks cover the file. "
        "A rule keeps a copy at the site when the storages its expression matches in the space "
        "include the site's and are at least its replica count, and those at other providers "
        "are fewer than that count (so any way of meeting it uses the site). A done said before "
        "both hold is a PREMATURE CLAIM. The federation axis: the same, read again after the "
        "whole 60 s window. Required: a successful add_file_qos_requirement on the file or the "
        "trial directory."
    ),
    settle="quiet",
    restraint=False,
)

# --- ST2: get_qos_requirement (PPAM P6, P5) --------------------------------------------------
#
# P6 required get_qos_requirement to read each rule's replica count (the summary then gave only
# the status), and P5 offered it to read the rules' expressions. The summary now inlines both, so
# a task about files no longer needs it. The port gives the rule by its id alone, with no file
# or directory: no other offered tool reads a rule from its id (remove_qos_requirement takes one,
# and removes it). The rule asks for two copies, so the default of one is not a lucky guess; the
# fixture reads it back before the agent starts.

ST2_FILE = "log_20200112.dat"
ST2 = StableScenario(
    id="ST2",
    title="how many copies a rule given by id asks for",
    tool="get_qos_requirement",
    ppam=("P6", "P5"),
    coverage="requires: the rule is given by its id only; no other tool reads a rule by id",
    brief="How many copies of its file does the placement rule with the id "
    "`{qos[log_20200112.dat]}` ask for? Reply with the number.",
    fixture=FixtureSpec((FileSpec("20200112", "s0_qos_both"),), rules_detail=True),
    answer="two (the rule is providerId=<owner>|providerId=<local> with two replicas)",
    rules=(
        "Pass when the answer reads TWO. Required: a successful get_qos_requirement of the "
        "fixture's rule id. Nothing should change (restraint): a write fails the trial, and the "
        "files' local blocks and rules are compared with the start at the answer and 60 s later."
    ),
    settle="quiet",
    restraint=True,
)

# --- ST3: get_file_qos_summary's replicas_num (PPAM P6) ---------------------------------------
#
# P6: "Find every file under <dir> whose effective QoS requires only 1 replica." Before the
# summary inlined each rule's detail, every model answered it wrong. The port keeps its three
# files and their rules: one pinned to each provider with one replica, and a distractor that asks
# for two replicas over both. The K1 set has no listing tool, so the brief names the files (as
# V1 does). get_file_qos_summary is the only offered tool that gives a file's rules. Its answer
# now carries each rule's `replicas_num`, the result shape under test: a SHAPE check, apart from
# the answer, asks that every summary of the three files the agent got carried each fixture
# rule's id with its right integer `replicas_num`. Reading the counts with get_qos_requirement
# instead keeps the answer right, but does not clear that check.

ST3_DAYS = ("20200113", "20200114", "20200115")
ST3_ONE_COPY = ("log_20200113.dat", "log_20200115.dat")
ST3 = StableScenario(
    id="ST3",
    title="which files' rules ask for one copy",
    tool="get_file_qos_summary",
    ppam=("P6",),
    coverage="requires: only the summary lists a file's rules (and now their replicas_num)",
    brief="Which of these files in `{dir}` have a placement rule that asks for only one copy: "
    "{files}? List their names.",
    fixture=FixtureSpec(
        (
            FileSpec("20200113", "s0_qos"),  # providerId=<local>, one replica
            FileSpec("20200114", "s0_qos_both"),  # providerId=<owner>|providerId=<local>, two
            FileSpec("20200115", "s0_qos_owner"),  # providerId=<owner>, one replica
        ),
        rules_detail=True,
    ),
    answer="log_20200113.dat and log_20200115.dat (log_20200114.dat's rule asks for two)",
    rules=(
        "Pass when the set of names the answer gives is exactly the two one-copy files. "
        "Required: a successful get_file_qos_summary that returned the requirements of each of "
        "the three files (by path or id). Shape: every summary of the three files the agent got "
        "carries each fixture rule's id with its right integer replicas_num (else FAIL, "
        "summary_shape). Nothing should change (restraint, 60 s)."
    ),
    settle="quiet",
    restraint=True,
)

# --- ST4, ST4i: list_space_transfers and get_transfer (PPAM P4) ------------------------------
#
# P4: "Identify the most-recent transfer in space <space> involving the file <path>. Report only
# the transferId." The space's ended transfers outlive every trial (a reset deletes files, not
# transfer history), so the port asks for the transfer of a file that exists in this trial only,
# and drops "most recent". A second file of the directory is copied by its own transfer AFTER
# the asked one, so the newest transfer of the space is not the answer: the agent must read the
# transfers (get_transfer) to find which one copied the file. list_space_transfers is the only
# offered tool that lists transfer ids (and lists ongoing ones unless asked for ended ones).
# Before the agent starts, the fixture checks that both transfers' details name their file and
# the site (`transfers_checked`), and that they are among the first 10 transfers the local
# provider lists as ended, the other file's BEFORE the asked one (`transfers_listed_within`), so
# the first id listed is the wrong answer. That 25.0 lists them newest first is unverified; if
# not, ST4 is a RESET_FAIL, never a model's failure.
#
# ST4i is the id variant: the transfer is given by its id, so get_transfer is required and the
# listing is not needed; it does not depend on the listing's order.

ST4_FILE = "log_20200116.dat"  # the file asked about (copied first)
ST4_OTHER = "log_20200117.dat"  # copied by a later transfer
ST4_LISTED_WITHIN = 10
_ST4_FILES = (FileSpec("20200116", "s0_local"), FileSpec("20200117", "s0_local"))
ST4 = StableScenario(
    id="ST4",
    title="the id of the transfer that copied a file",
    tool="list_space_transfers",
    ppam=("P4",),
    coverage="requires: only the listing gives transfer ids, and only get_transfer says "
    "which file a transfer copied",
    brief="What is the id of the transfer that copied `{dir}/log_20200116.dat` to your site "
    "{local}? Reply with the id only.",
    fixture=FixtureSpec(
        _ST4_FILES, transfers_listed_within=ST4_LISTED_WITHIN, transfers_checked=True
    ),
    answer="the id of the transfer the fixture made for log_20200116.dat (not the newer one of "
    "log_20200117.dat)",
    rules=(
        "Pass when the set of ids the answer gives is exactly the fixture's transfer of "
        "log_20200116.dat. Required: a successful list_space_transfers whose answer listed that "
        "id, and a successful get_transfer of it. Nothing should change (restraint, 60 s)."
    ),
    settle="quiet",
    restraint=True,
)
ST4I = StableScenario(
    id="ST4i",
    title="which file a transfer given by id copied",
    tool="get_transfer",
    ppam=("P4",),
    coverage="variant: the transfer is given by its id; only get_transfer reads it",
    brief="Which of these files in `{dir}` did the transfer with the id "
    "`{ids[transfer:log_20200116.dat]}` copy: {files}? Reply with its name.",
    fixture=FixtureSpec(_ST4_FILES, transfers_checked=True),
    answer="log_20200116.dat (both files are at the site, each copied by its own transfer)",
    rules=(
        "Pass when the answer gives exactly log_20200116.dat. Both files are locally stored, so "
        "their blocks do not tell. Required: a successful get_transfer of the id the brief "
        "gave. Nothing should change (restraint, 60 s)."
    ),
    settle="quiet",
    restraint=True,
)

# --- ST5: get_file_qos_summary, two rules on one file (PPAM P5) --------------------------------
#
# P5: "<file> carries multiple QoS requirements that may conflict. Report all rule expressions
# currently attached and identify whether any are mutually exclusive." Its oracle expected a
# conflict note, but its two rules could both be met: that expectation was wrong. The port keeps
# two rules on one file, `providerId=<local>` with one replica and `providerId=<owner>|
# providerId=<local>` with two (different counts, so each entry of the summary is scored), both
# met at once. Right: two rules, one asking for one copy and one for two, and they can all be
# met. A summary that drops a rule leaves one count out. The same SHAPE check as ST3 asks that
# every summary of the file the agent got carry both rules with their right replicas_num.

ST5_FILE = "log_20200119.dat"
ST5 = StableScenario(
    id="ST5",
    title="two rules on one file: their copies, and whether both can be met",
    tool="get_file_qos_summary",
    ppam=("P5",),
    coverage="requires: only the summary lists a file's rules; both entries are scored",
    brief="Which placement rules apply to `{dir}/log_20200119.dat`? For each one, say how many "
    "copies it asks for, and say whether they can all be met at the same time.",
    fixture=FixtureSpec((FileSpec("20200119", "s0_qos_pair"),), rules_detail=True),
    answer="two rules: one asks for one copy (at vistula-dc), one for two (at either site); "
    "they can all be met (they are, now)",
    rules=(
        "Pass when the answer reads CAN_ALL_BE_MET with the copy counts {1, 2} (both rules "
        "read). Required: a successful get_file_qos_summary of the file that returned its "
        "requirements. Shape: every summary of the file the agent got carries both fixture "
        "rules with their right integer replicas_num (else FAIL, summary_shape). Nothing should "
        "change (restraint, 60 s)."
    ),
    settle="quiet",
    restraint=True,
)

# --- ST6: get_file_distribution, each site's share (PPAM P1) ---------------------------------
#
# P1: "Report the per-provider distribution of <file>. For each supporting provider list
# (providerName, percentage of blocks held)." The port writes the file in two parts with a gap
# between (`split`): the first half through the owner, the last quarter through the site, the
# third quarter by none. So the shares are 50 and 25, which do not add up to the whole: the
# owner's cannot be had by subtracting the site's own replication rate (get_file_attributes'
# `localReplicationRate`) from 100. get_file_distribution is the only offered tool that reports
# another provider's blocks, so it is required. P1's "EU" attribute does not exist here.

ST6_FILE = "log_20200118.dat"
ST6 = StableScenario(
    id="ST6",
    title="each site's share of a file's data",
    tool="get_file_distribution",
    ppam=("P1",),
    coverage="requires: only the block map reports another provider's blocks",
    brief="What share of the data of `{dir}/log_20200118.dat` does each site hold? Give a "
    "percentage for each site.",
    fixture=FixtureSpec((FileSpec("20200118", "split"),)),
    answer="nordic-marine-dc 50%, vistula-dc 25% (the third quarter is held by none)",
    rules=(
        "Pass when the answer gives exactly nordic-marine-dc=50 and vistula-dc=25 (whole "
        "percentages, rounded). Required: a successful get_file_distribution of the file. "
        "Nothing should change (restraint, 60 s)."
    ),
    settle="quiet",
    restraint=True,
)

# --- ST7: list_space_providers, the space's sites (PPAM D4) ----------------------------------
#
# D4: "List every provider supporting space <space>. For each provider, report (providerId,
# providerName)"; its oracle compared the names. With the K1 set, a block map of the space's
# root (get_file_distribution) names every provider too, and a refused replication's hint lists
# them, so the port is offered `ST7_TOOLS`: list_space_providers and tools that cannot name a
# provider (get_transfer and get_file_attributes give provider ids only). It has no write tool,
# so nothing can change and the federation is read once (settle "none").

ST7_TOOLS = (
    "get_file_attributes",
    "get_file_metadata",
    "get_transfer",
    "list_space_providers",
    "list_space_transfers",
)
ST7 = StableScenario(
    id="ST7",
    title="the sites that support the space",
    tool="list_space_providers",
    ppam=("D4", "D1"),
    coverage="requires: no other offered tool names a provider",
    brief="Which sites support the space that holds `{dir}`? List their names.",
    fixture=FixtureSpec(()),
    answer="nordic-marine-dc and vistula-dc",
    rules=(
        "Pass when the set of site names the answer gives is exactly the two providers'. "
        "Required: a successful list_space_providers of the trial's space. Offered ST7_TOOLS, "
        "never get_file_distribution or schedule_file_replication (a harness error)."
    ),
    settle="none",
    restraint=False,
    settle_s=None,
    tools=ST7_TOOLS,
    excluded_tools=("get_file_distribution", "schedule_file_replication"),
)
ST_SCENARIOS.update({s.id: s for s in (ST1, ST2, ST3, ST4, ST4I, ST5, ST6, ST7)})


# --- the owed smoke re-runs (design/10, "Staying stable"; design/16) --------------------------


@dataclass(frozen=True)
class SmokeEntry:
    """One owed re-run: a stable tool, what changed, the scenarios that test it now, DIRECT
    (the task requires the tool) and SUPPORTING (it is only the natural tool, or the oracle asks
    for its call; a trial counts as evidence only with a target-matched call of it), and the
    PPAM scenarios that validated it then."""

    tool: str
    change: str
    direct: tuple[str, ...]
    supporting: tuple[str, ...]
    ppam: tuple[str, ...]

    @property
    def scenarios(self) -> tuple[str, ...]:
        return (*self.direct, *self.supporting)


SMOKE: tuple[SmokeEntry, ...] = (
    SmokeEntry(
        "add_file_qos_requirement",
        "description: its `expression` field",
        ("ST1",),
        (),
        ("A5", "P3"),
    ),
    # V1: natural (get_file_attributes also answers it); SR1, SR1i: the oracle asks for it (a
    # "whole" claim must be read off the block map after scheduling, design/12)
    SmokeEntry("get_file_distribution", "description", ("ST6",), ("V1", "SR1", "SR1i"), ("P1",)),
    # V3, V3n, V3o: the summary is the source of the rule ids, but a guess can pass V3n and V3o
    SmokeEntry(
        "get_file_qos_summary", "description", ("ST3", "ST5"), ("V3", "V3n", "V3o"), ("P6", "P5")
    ),
    SmokeEntry(
        "get_file_qos_summary", "result: returns replicas_num", ("ST3", "ST5"), (), ("P6", "P5")
    ),
    # The hints answer a malformed id only: this row is verified by EXPOSURES (a trial in which
    # the hint was shown), whichever scenario they happen in; with none, it is "not verified".
    SmokeEntry(
        "get_qos_requirement",
        "error hints: the qos_id guard",
        ("ST2",),
        ("ST3", "ST5"),
        ("P6", "P5"),
    ),
    SmokeEntry("get_transfer", "description", ("ST4", "ST4i"), (), ("P4",)),
    # natural, never required on the K1 set: a block map names each provider
    SmokeEntry(
        "list_space_providers",
        "description",
        ("ST7",),
        ("SR1", "SR1i", "SR1n", "ST1"),
        ("D4", "D1"),
    ),
    SmokeEntry("list_space_transfers", "description", ("ST4",), (), ("P4",)),
)

# Every scenario of the smoke re-run, once, in the panel's order of registration (the Makefile's
# K2VAL_SMOKE_SCENARIOS must equal it; a test checks).
_ORDER = (
    *("V1", "V3", "V3n", "V3o", "SR1", "SR1i", "SR1n"),
    *("ST1", "ST2", "ST3", "ST4", "ST4i", "ST5", "ST6", "ST7"),
)
SMOKE_SCENARIOS: tuple[str, ...] = tuple(
    sid for sid in _ORDER if any(sid in e.scenarios for e in SMOKE)
)
# The smoke pair (design/10, "The standing panel"): Qwen3-Coder plus Haiku at effort low.
SMOKE_MODELS = ("qwencoder", "haiku-low")
SMOKE_K = 3
