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

from benchmark.k2val.fixtures import FileSpec, FixtureSpec

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
Settle = Literal["none", "local_covered", "qos_gone", "complete"]


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

    def render_brief(
        self, *, directory: str, local: str, ids: Mapping[str, str] | None = None
    ) -> str:
        """`{dir}`, `{local}`, and `{files}`: the fixture's file names, for a brief that must
        name what the offered tools cannot list. `{ids[<path>]}`: the file id of a fixture entry
        (known only once it is built; before that, a placeholder)."""
        files = ", ".join(f.name for f in self.fixture.files)
        known = _IdsOrPlaceholder(ids or {})
        return self.brief.format(dir=directory, local=local, files=files, ids=known)


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
        ),
        Scenario(
            "V3o",
            "ask about rule",
            "K1",
            # Read the QoS state to answer; the removal tool is offered but not needed.
            "Does a placement rule keep `{dir}/log_20200107.dat` at your site {local}?",
            FixtureSpec((FileSpec("20200107", "s0_qos"),)),
        ),
        Scenario("V4", "check, remote", "K2", BRIEF_CHECK, _five()),
        Scenario(
            "V5",
            "check, arriving",
            "K2",
            BRIEF_CHECK,
            _five({d: "s2" for d in S2_DAYS}, finish=20 * 60.0),
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


# --- the file-tool scenarios (FA, LC, CD; design/13-15) ---------------------------------------
# Defined, with their answers and rules, in file_scenarios.py (a leaf module: no import cycle).
from benchmark.k2val.file_scenarios import FILE_SCENARIOS  # noqa: E402

SCENARIOS.update(
    {
        s.id: Scenario(s.id, s.title, "K1", s.brief, s.fixture, tools=s.tools)
        for s in FILE_SCENARIOS.values()
    }
)


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
