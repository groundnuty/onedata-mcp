"""The scenarios of the validation panel (design/08): V1..V7, plus V3n and V3o.

A scenario is a brief, a fixture and a tool set; the oracle lives in `oracles.py`. Briefs name
the directory and the dates and never a tool. The domain is "instrument logs", on purpose far
from the agent study's own prompts.

V3n and V3o complete the scenario coverage design/10 asks of `remove_qos_requirement`, with V3 as
the task that needs it: V3n is the "no, because" state (V3's brief, but the file has no rule),
V3o a question the QoS state answers with the tool offered but not needed. They are outside the
`V1..V7` range, so name them: `V3,V3n,V3o`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from benchmark.k2val.fixtures import LARGE_FILE_SIZE, FileSpec, FixtureSpec

SYSTEM_PROMPT_TEMPLATE = (
    "You are an assistant with access to tools of a federated data management system "
    "(Onedata). Your site is the provider `{local}`. Use the tools to complete the task, "
    "then give a short final answer."
)

V1_DAYS = ("20200101", "20200102", "20200103", "20200104", "20200105", "20200106")
FIVE_DAYS = ("20200101", "20200102", "20200103", "20200104", "20200105")
S2_DAYS = ("20200102", "20200104")  # the two files still being written in V5 and V7

BRIEF_CHECK = (
    "Are the log files in `{dir}` for 1–5 January 2020 complete and readable at your site? "
    "If not, why not?"
)
BRIEF_ENSURE = (
    "Make the log files in `{dir}` for 1–5 January 2020 complete and readable at your "
    "site, and tell me when they are."
)
# V3 and V3n share it word for word: only the state differs (a rule, or none).
BRIEF_REMOVE = "A placement rule keeps `{dir}/log_20200107.dat` at {local}. Remove that rule."

# What the federation axis waits for after the final answer, within the settle window.
# "quiet": nothing should change, so the whole window is waited out, and a transfer or rule
# started just before the answer has time to show on the federation axis.
Settle = Literal["none", "local_covered", "qos_gone", "complete", "quiet"]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    tier: Literal["K1", "K2"]
    brief: str
    fixture: FixtureSpec
    settle: Settle = "none"
    # The tools offered, when not the tier's set (the file-tool scenarios, design/13-15).
    tools: tuple[str, ...] | None = None
    # Nothing should change (design/10, gate 4): a successful write fails the trial, and the
    # federation is compared with the start state at the answer and after the settle window,
    # which is waited out in full (settle "quiet").
    restraint: bool = False
    # The settle window of this scenario, in seconds; None: `[run] settle_s`.
    settle_s: float | None = None
    # Tools that must never be offered with this scenario (a harness error if they are): a tool
    # that would answer it without the one under promotion.
    excluded_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.restraint and self.settle != "quiet":
            raise ValueError(f"{self.id}: a restraint scenario waits out its window (quiet)")

    def render_brief(
        self,
        *,
        directory: str,
        local: str,
        ids: Mapping[str, str] | None = None,
        qos: Mapping[str, str] | None = None,
    ) -> str:
        """`{dir}`, `{dirname}` (its last segment), `{local}`, and `{files}`: the fixture's file
        names, for a brief that must name what the offered tools cannot list. `{ids[<path>]}`:
        the file id of a fixture entry or log file, `{ids[.]}` the trial directory's,
        `{ids[space]}` the space's (when the fixture reads it); `{qos[<file>]}`: the id of the
        rule the fixture made on a file (all known only once it is built; before that, a
        placeholder)."""
        files = ", ".join(f.name for f in self.fixture.files)
        known = _IdsOrPlaceholder(ids or {})
        rules = _IdsOrPlaceholder(qos or {})
        return self.brief.format(
            dir=directory,
            dirname=directory.rsplit("/", 1)[-1],
            local=local,
            files=files,
            ids=known,
            qos=rules,
        )


class _IdsOrPlaceholder(dict):
    def __missing__(self, path: str) -> str:
        return f"<id of {path}>"


def _five(kinds: dict[str, str] | None = None, finish: float | None = None) -> FixtureSpec:
    kinds = kinds or {}
    return FixtureSpec(
        tuple(FileSpec(d, kinds.get(d, "s0")) for d in FIVE_DAYS),  # type: ignore[arg-type]
        writer_finish_s=finish,
    )


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in (
        Scenario(
            "V1",
            "attributes",
            "K1",
            # The K1 set has no listing tool, so the brief names the six files.
            "Which of these files in `{dir}` are fully stored at your site {local}: {files}? "
            "List the names of those that are.",
            FixtureSpec(
                tuple(
                    FileSpec(d, "s0_local" if d in ("20200102", "20200105") else "s0")
                    for d in V1_DAYS
                )
            ),
            settle="quiet",
            restraint=True,
            settle_s=60.0,
        ),
        Scenario(
            "V2",
            "replicate",
            "K1",
            "Copy `{dir}/log_20200105.dat` to your site {local} and tell me when its data are "
            "fully there.",
            FixtureSpec((FileSpec("20200105", "s0"),)),
            settle="local_covered",
        ),
        Scenario(
            "V3",
            "remove rule",
            "K1",
            BRIEF_REMOVE,
            FixtureSpec((FileSpec("20200107", "s0_qos"),)),
            settle="qos_gone",
        ),
        Scenario(
            "V3n",
            "remove rule, none there",
            "K1",
            # The brief's premise is half true: the file IS at the local site, but because it was
            # copied there, not because a rule keeps it. The right answer: there is no rule.
            BRIEF_REMOVE,
            FixtureSpec((FileSpec("20200107", "s0_local"),)),
            settle="quiet",
            restraint=True,
            settle_s=60.0,
        ),
        Scenario(
            "V3o",
            "ask about rule",
            "K1",
            # Read the QoS state to answer; the removal tool is offered but not needed.
            "Does a placement rule keep `{dir}/log_20200107.dat` at your site {local}?",
            FixtureSpec((FileSpec("20200107", "s0_qos"),)),
            settle="quiet",
            restraint=True,
            settle_s=60.0,
        ),
        # V3i: V3's task with the rule given by its id only (the id form of `qos_id`).
        Scenario(
            "V3i",
            "remove rule, by id",
            "K1",
            "The placement rule with the id `{qos[log_20200107.dat]}` keeps a file at {local}. "
            "Remove that rule.",
            FixtureSpec((FileSpec("20200107", "s0_qos"),)),
            settle="qos_gone",
        ),
        # V4 and V5 ask; nothing should change. ensure_readiness, offered in the K2 set, is a
        # write tool (it places QoS rules): a successful call fails them.
        Scenario(
            "V4",
            "check, remote",
            "K2",
            BRIEF_CHECK,
            _five(),
            settle="quiet",
            restraint=True,
            settle_s=60.0,
        ),
        Scenario(
            "V5",
            "check, arriving",
            "K2",
            BRIEF_CHECK,
            _five({d: "s2" for d in S2_DAYS}, finish=20 * 60.0),
            settle="quiet",
            restraint=True,
            settle_s=60.0,
        ),
        Scenario("V6", "ensure, remote", "K2", BRIEF_ENSURE, _five(), settle="local_covered"),
        Scenario(
            "V7",
            "ensure, arriving",
            "K2",
            BRIEF_ENSURE,
            _five({d: "s2" for d in S2_DAYS}, finish=6 * 60.0),
            settle="complete",
        ),
    )
}


# ---------------------------------------------------------------------------------------------
# schedule_file_replication's scenarios (design/12-promotion-schedule-file-replication.md, gate 4)
#
# One file, `log_20200110.dat`, written at the owner only (kind s0), in every SR scenario, so
# neither the directory (opaque) nor the file tells a model which scenario it is in. The full K1
# set is offered in each. The oracles are in `sr_oracles.py`; each one's docstring gives the
# brief's gist, the right answer and the pass and fail rules.
#
# SR1   needs it. "Copy it to your site, set up no placement rule, tell me when it is there."
#       No other K1 tool makes a copy without a placement rule (add_file_qos_requirement makes
#       one; adding a rule and removing it later is still setting one up), so SR1 REQUIRES the
#       tool. A large file, so that a "done" said before the blocks arrive can be seen; and a
#       claim of completion passes only if the agent read the block map after scheduling.
# SR1n  "no, because": SR1's brief with a site that does not support the space (`oder-dc` is
#       not in the federation at all). Right: it cannot be done, and why; nothing is copied.
# SR1o  offered, not needed: "has it been copied to your site?" Right: no. Nothing is copied.
#
# SR1i  SR1's task with the FILE given by its id only (the file argument's id form): it
#       requires a successful call that passes that id.
#
# Not built: a variant giving the TARGET provider by id. The tool takes only an id there, so the
# name form (SR1, SR1n) is the one with something to resolve, and every successful call passes
# an id: inapplicable, recorded in design/12 with the other variants the protocol asks about.
# ---------------------------------------------------------------------------------------------

SR_DAY = "20200110"
SR_FILE = "log_20200110.dat"
SR_ELSEWHERE = "oder-dc"  # a site name that no provider of the federation has
_BRIEF_COPY = (
    "Make a copy of `{dir}/log_20200110.dat` at {where}, without setting up any placement rule "
    "for it, and tell me when its data are fully there."
)
SR_SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in (
        Scenario(
            "SR1",
            "copy without a rule",
            "K1",
            _BRIEF_COPY.replace("{where}", "your site {local}"),
            FixtureSpec((FileSpec(SR_DAY, "s0", LARGE_FILE_SIZE),)),
            settle="local_covered",
        ),
        Scenario(
            "SR1i",
            "copy without a rule, file by id",
            "K1",
            "Make a copy of the file with the id `{ids[log_20200110.dat]}` at your site {local}, "
            "without setting up any placement rule for it, and tell me when its data are fully "
            "there.",
            FixtureSpec((FileSpec(SR_DAY, "s0", LARGE_FILE_SIZE),)),
            settle="local_covered",
        ),
        Scenario(
            "SR1n",
            "copy to a site outside the space",
            "K1",
            _BRIEF_COPY.replace("{where}", f"the site {SR_ELSEWHERE}"),
            FixtureSpec((FileSpec(SR_DAY, "s0"),)),
            settle="quiet",
            restraint=True,
            settle_s=60.0,
        ),
        Scenario(
            "SR1o",
            "ask whether copied",
            "K1",
            "Has `{dir}/log_20200110.dat` been copied to your site {local}?",
            FixtureSpec((FileSpec(SR_DAY, "s0"),)),
            settle="quiet",
            restraint=True,
            settle_s=60.0,
        ),
    )
}
SCENARIOS.update(SR_SCENARIOS)


# --- the file-tool scenarios (FA, LC, CD; design/13-15) ---------------------------------------
# Defined, with their answers and rules, in file_scenarios.py (a leaf module: no import cycle).
from benchmark.k2val.file_scenarios import FILE_SCENARIOS  # noqa: E402

SCENARIOS.update(
    {
        s.id: Scenario(
            s.id,
            s.title,
            "K1",
            s.brief,
            s.fixture,
            settle="quiet",  # every one is read again after its window
            tools=s.tools,
            restraint=s.restraint,
            settle_s=s.settle_s,
            excluded_tools=s.excluded_tools,
        )
        for s in FILE_SCENARIOS.values()
    }
)

# --- the readiness tools' scenarios (RC*, RE*; design/17, design/18) ---------------------------
# In readiness_scenarios.py (a leaf module). RCo and REo take V1's and SR1's brief and fixture
# word for word (`same_as`); the others are offered their own sets (RC_TOOLS, RE_TOOLS).
from benchmark.k2val.readiness_scenarios import READINESS_SCENARIOS  # noqa: E402

for _r in READINESS_SCENARIOS.values():
    _base = SCENARIOS[_r.same_as] if _r.same_as else None
    SCENARIOS[_r.id] = Scenario(
        _r.id,
        _r.title,
        "K2",
        _base.brief if _base else _r.brief,
        _base.fixture if _base else _r.fixture,  # type: ignore[arg-type]
        settle=_r.settle,
        tools=_r.tools,
        restraint=_r.restraint,
        settle_s=_r.settle_s,
        excluded_tools=_r.excluded_tools,
    )

# --- the stable tools' smoke scenarios (ST*; design/16) ---------------------------------------
# PPAM's scenarios ported to the panel, in stable_scenarios.py (a leaf module: no import cycle).
from benchmark.k2val.stable_scenarios import ST_SCENARIOS  # noqa: E402

SCENARIOS.update(
    {
        s.id: Scenario(
            s.id,
            s.title,
            "K1",
            s.brief,
            s.fixture,
            settle=s.settle,
            tools=s.tools,
            restraint=s.restraint,
            settle_s=s.settle_s,
            excluded_tools=s.excluded_tools,
        )
        for s in ST_SCENARIOS.values()
    }
)


# The tool each scenario exercises (required, or offered and not needed), independently of the
# configured tool sets: a run whose offered set lacks it is a harness error before dispatch.
TOOL_UNDER_TEST: dict[str, str] = {
    **dict.fromkeys(("V3", "V3i", "V3n", "V3o"), "remove_qos_requirement"),
    **dict.fromkeys(("SR1", "SR1i", "SR1n", "SR1o"), "schedule_file_replication"),
    # V1 is get_file_attributes' offered-but-not-needed trial (design/13)
    **dict.fromkeys(("V1", "FA", "FAi", "FAn"), "get_file_attributes"),
    **dict.fromkeys(("LC", "LCn", "LCo", "LCp", "LCi"), "list_children"),
    **dict.fromkeys(("CD", "CDn", "CDo", "CDp"), "create_directory"),
    **dict.fromkeys(("V4", "V5"), "check_readiness"),
    **dict.fromkeys(("V6", "V7"), "ensure_readiness"),
    **{s.id: s.tool for s in READINESS_SCENARIOS.values()},
    **{s.id: s.tool for s in ST_SCENARIOS.values()},
}


def parse_scenarios(text: str) -> list[str]:
    """`V1..V7`, `V2,V5`, `V1..V3,V7` -> ordered, unique scenario ids."""
    out: list[str] = []
    for part in (p.strip() for p in text.split(",") if p.strip()):
        m = re.fullmatch(r"V(\d+)\.\.V(\d+)", part)
        ids = [f"V{i}" for i in range(int(m[1]), int(m[2]) + 1)] if m else [part]
        for sid in ids:
            if sid not in SCENARIOS:
                raise ValueError(f"unknown scenario {sid!r}; known: {sorted(SCENARIOS)}")
            if sid not in out:
                out.append(sid)
    return out
