"""The smoke scenarios of the stable tools (design/16): PPAM's scenarios ported to the panel.

design/10 ("Staying stable") asks that a stable tool whose description wording, error hints or
result shape changes re-run "the tool's scenarios on the smoke pair at K=3". Eight such changes
are owed. Their tools were validated by the earlier benchmark (`benchmark/scenarios.py`, 18
scenarios against another federation), so the scenarios that exercised each tool there are
ported here, adapted to the validation federation (`benchmark/valfed/layout.toml`: two
providers, data written at `owner`, the agent at `local`). Where an existing panel scenario
already serves a tool (V1, V3, SR1, ...), it is reused instead: `SMOKE` maps every owed change to
its scenarios, reused and new.

Each new scenario below is one `StableScenario` literal holding what a reviewer needs: the
brief, the fixture, the tool it serves, the PPAM scenario(s) it derives from, the right answer
and the rules. Every one is offered the panel's K1 set (all eight tools are in it, and its
surface hash is recorded). The answer readers, oracles and required calls are in
`stable_oracles.py`; the judge's label sets in `rubrics.py`; `scenarios.py` registers them.

Ids: ST1..ST4, ST4i. Each brief is neutral: it names no tool and gives no advice on how to work.

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
    settle: Literal["quiet", "local_covered"]
    # Nothing should change (scoring.py's restraint checks): every read-only one, since the K1
    # set offers write tools.
    restraint: bool
    settle_s: float = 60.0


ST_SCENARIOS: dict[str, StableScenario] = {}


# --- ST1: add_file_qos_requirement (PPAM A5, P3) ---------------------------------------------
#
# A5 asked to attach a rule "replicated on at least 2 EU providers", P3 to add one and confirm it
# took effect. Our federation has two providers and no storage attributes (no country or geo
# tag), and the owner already holds the data, so the port asks for a rule that keeps the file at
# the agent's own site: the expression must name that provider, which is what the reworded
# `expression` field is about (`providerId=<id>` is always safe; admin tags work only if set).
# P3's polling instructions are workflow advice, which a neutral brief does not give: the port
# asks for the end state instead, and the oracle checks it at the answer (a premature claim).

ST1_FILE = "log_20200111.dat"
ST1 = StableScenario(
    id="ST1",
    title="keep a file at the site by a placement rule",
    tool="add_file_qos_requirement",
    ppam=("A5", "P3"),
    coverage="requires: no other offered tool sets up a placement rule",
    brief="Set up a placement rule that keeps `{dir}/log_20200111.dat` at your site {local}, "
    "and tell me when its data are fully there.",
    fixture=FixtureSpec((FileSpec("20200111", "s0"),)),
    answer="done: a rule (e.g. providerId=<vistula-dc's id>) keeps the file at vistula-dc, and "
    "its data are fully there",
    rules=(
        "Pass when the answer reads SAYS_DONE AND, at the answer, a rule the agent added on the "
        "file (or on the trial directory) is on the file and the local provider's blocks cover "
        "it. The rule must keep the file at the site: its expression names the local provider's "
        "id (with no exclusion), or no copy was scheduled by other means, so the local copy can "
        "only have come from the rule. A done said before both hold is a PREMATURE CLAIM. The "
        "federation axis: the same, after the settle. Required: a successful "
        "add_file_qos_requirement on the file or the trial directory."
    ),
    settle="local_covered",
    restraint=False,
)

# --- ST2: get_qos_requirement (PPAM P6, P5) --------------------------------------------------
#
# P6 required get_qos_requirement to read each rule's replica count (the summary then gave only
# the status), and P5 offered it to read the rules' expressions. The summary now inlines both, so
# a task about files no longer needs it. The port gives the rule by its id alone, with no file
# or directory: no other offered tool reads a rule from its id (remove_qos_requirement takes one,
# and removes it). The rule asks for two copies, so the default of one is not a lucky guess.

ST2_FILE = "log_20200112.dat"
ST2 = StableScenario(
    id="ST2",
    title="how many copies a rule given by id asks for",
    tool="get_qos_requirement",
    ppam=("P6", "P5"),
    coverage="requires: the rule is given by its id only; no other tool reads a rule by id",
    brief="How many copies of its file does the placement rule with the id "
    "`{qos[log_20200112.dat]}` ask for? Reply with the number.",
    fixture=FixtureSpec((FileSpec("20200112", "s0_qos_both"),)),
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
# V1 does). get_file_qos_summary is the only offered tool that gives a file's rules; its answer
# now carries each rule's `replicas_num`, the result shape under test. get_qos_requirement is
# offered and may be used for the counts too.

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
        )
    ),
    answer="log_20200113.dat and log_20200115.dat (log_20200114.dat's rule asks for two)",
    rules=(
        "Pass when the set of names the answer gives is exactly the two one-copy files. "
        "Required: a successful get_file_qos_summary that returned the requirements of each of "
        "the three files (by path or id). Nothing should change (restraint, 60 s)."
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
# The fixture checks, before the agent starts, that both transfers are among the first 10 the
# local provider lists as ended (`transfers_listed_within`): an answer must not need the whole
# history. That 25.0 lists them newest first is unverified; if not, ST4 is a RESET_FAIL.
#
# ST4i is the id variant: the transfer is given by its id, so get_transfer is required and the
# listing is not needed; it does not depend on the listing's order.

ST4_FILE = "log_20200116.dat"  # the file asked about (copied first)
ST4_OTHER = "log_20200117.dat"  # copied by a later transfer
ST4_LISTED_WITHIN = 10
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
        (FileSpec("20200116", "s0_local"), FileSpec("20200117", "s0_local")),
        transfers_listed_within=ST4_LISTED_WITHIN,
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
    fixture=FixtureSpec((FileSpec("20200116", "s0_local"), FileSpec("20200117", "s0_local"))),
    answer="log_20200116.dat (both files are at the site, each copied by its own transfer)",
    rules=(
        "Pass when the answer gives exactly log_20200116.dat. Both files are locally stored, so "
        "their blocks do not tell. Required: a successful get_transfer of the id the brief "
        "gave. Nothing should change (restraint, 60 s)."
    ),
    settle="quiet",
    restraint=True,
)
ST_SCENARIOS.update({s.id: s for s in (ST1, ST2, ST3, ST4, ST4I)})


# --- the owed smoke re-runs (design/10, "Staying stable"; design/16) --------------------------


@dataclass(frozen=True)
class SmokeEntry:
    """One owed re-run: a stable tool, what changed, and the scenarios that test it now and
    the PPAM scenarios that validated it then."""

    tool: str
    change: str
    scenarios: tuple[str, ...]
    ppam: tuple[str, ...]


SMOKE: tuple[SmokeEntry, ...] = (
    SmokeEntry(
        "add_file_qos_requirement", "description: its `expression` field", ("ST1",), ("A5", "P3")
    ),
    # V1 offers it as the natural reader of local blocks; SR1 and SR1i require it (a "whole"
    # claim passes only when read off the block map after scheduling, design/12).
    SmokeEntry("get_file_distribution", "description", ("V1", "SR1", "SR1i"), ("P1",)),
    # the only offered source of a file's rule ids: needed to find, or rule out, the rule
    SmokeEntry("get_file_qos_summary", "description", ("V3", "V3n", "V3o"), ("P2", "P5", "P6")),
    SmokeEntry("get_file_qos_summary", "result: returns replicas_num", ("ST3",), ("P6",)),
    SmokeEntry(
        "get_qos_requirement", "error hints: the qos_id guard", ("ST2", "ST3"), ("P6", "P5")
    ),
    SmokeEntry("get_transfer", "description", ("ST4", "ST4i"), ("P4",)),
    # natural, never required: the site's id is also in a block map (`providerName`) and in the
    # hint of a refused replication
    SmokeEntry(
        "list_space_providers", "description", ("SR1", "SR1i", "SR1n", "ST1"), ("D1", "D4", "P1")
    ),
    SmokeEntry("list_space_transfers", "description", ("ST4",), ("P4", "P3")),
)

# Every scenario of the smoke re-run, once, in the panel's order of registration (the Makefile's
# K2VAL_SMOKE_SCENARIOS must equal it; a test checks).
_ORDER = ("V1", "V3", "V3n", "V3o", "SR1", "SR1i", "SR1n", "ST1", "ST2", "ST3", "ST4", "ST4i")
SMOKE_SCENARIOS: tuple[str, ...] = tuple(
    sid for sid in _ORDER if any(sid in e.scenarios for e in SMOKE)
)
# The smoke pair (design/10, "The standing panel"): Qwen3-Coder plus Haiku at effort low.
SMOKE_MODELS = ("qwencoder", "haiku-low")
SMOKE_K = 3
