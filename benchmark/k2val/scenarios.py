"""The seven scenarios of the validation panel (design/08).

A scenario is a brief, a fixture and a tool set; the oracle lives in `oracles.py`. Briefs name
the directory and the dates and never a tool. The domain is "instrument logs", on purpose far
from the agent study's own prompts.
"""

from __future__ import annotations

import re
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

    def render_brief(self, *, directory: str, local: str) -> str:
        """`{dir}`, `{local}`, and `{files}`: the fixture's file names, for a brief that must
        name what the offered tools cannot list."""
        files = ", ".join(f.name for f in self.fixture.files)
        return self.brief.format(dir=directory, local=local, files=files)


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
            "A placement rule keeps `{dir}/log_20200107.dat` at {local}. Remove that rule.",
            FixtureSpec((FileSpec("20200107", "s0_qos"),)),
            settle="qos_gone",
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
