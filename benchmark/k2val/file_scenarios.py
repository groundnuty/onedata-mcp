"""The scenarios of the three file tools' promotion (design/13, design/14, design/15).

Each scenario below is one `FileScenario` literal that holds everything a reviewer needs: the
brief, the fixture, the tools offered, the right answer, the pass and fail rules, and the end
state the federation must show. The answer readers and the oracles are in `file_oracles.py`;
`scenarios.py` registers these scenarios in `SCENARIOS` (its last block). Ids:

- FA*: `get_file_attributes` (design/13). Offered the K1 set, which has no listing tool, so no
  other tool can read a file's permissions.
- LC*: `list_children` (design/14). Offered FILE_TOOLS: the only other listing tool,
  `list_files_recursively`, lists files only, so it cannot show an empty directory.
- CD*: `create_directory` (design/15). Offered FILE_TOOLS, which withholds `delete_file` and
  `move_file`: with them, `create_file` of a file under the new directory followed by its
  deletion (or its move out) would also leave an empty directory.

Every brief is neutral: it names no tool and gives no advice on how to work. The fixtures are
small files and directories (`EntrySpec`) under the trial's own directory; the trial starts only
once the local provider (the agent's site) shows them all as built, and that tree is recorded as
the baseline. The federation axis compares the trial directory's tree at the answer, and after
it, with that baseline.

This module imports only the fixture types, so the registries can import it without a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from benchmark.k2val.fixtures import EntrySpec, FixtureSpec

# The file-tool set of the LC and CD scenarios. Withheld on purpose: delete_file and move_file
# (see CD above), and every tool that answers nothing these scenarios ask.
FILE_TOOLS = (
    "create_directory",
    "create_file",
    "download_file",
    "get_file_attributes",
    "list_children",
    "list_files_recursively",
)


@dataclass(frozen=True)
class FileScenario:
    """One scenario of the three file tools, with what a reviewer needs beside it."""

    id: str
    title: str
    tool: str  # the tool whose promotion it serves
    # what design/10 asks of the tool's scenarios: "requires", "no, because",
    # "offered, not needed", or "variant: ..."
    coverage: str
    brief: str
    fixture: FixtureSpec
    tools: tuple[str, ...] | None  # None: the panel's K1 set
    answer: str  # the right answer, in words (for the reviewer and a rubric judge)
    rules: str  # how the oracle passes or fails a trial
    # What the scenario's answer reader must return for a right answer: a file name or "none"
    # (FA), a set of names (LC), or "created" / "not_created" (CD).
    expect: str | frozenset[str]
    # The end state: entries the agent must add to the trial directory (path -> REG or DIR),
    # and the text a file it adds must hold (stripped). Anything else added, removed or changed
    # fails the federation axis.
    added: Mapping[str, str] = field(default_factory=dict)
    added_content: Mapping[str, str] = field(default_factory=dict)


# Every scenario of this module, by id; each family's section below adds its own.
FILE_SCENARIOS: dict[str, FileScenario] = {}


def _log(day: str, mode: str | None = None, *, under: str = "") -> EntrySpec:
    path = f"{under}/log_{day}.dat" if under else f"log_{day}.dat"
    return EntrySpec(path, "file", f"instrument log {day}\n".encode(), mode)


def _dir(path: str) -> EntrySpec:
    return EntrySpec(path, "dir")
