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


# --- FA: get_file_attributes (design/13) --------------------------------------------------------

FA_DAYS = ("20200101", "20200102", "20200103", "20200104")
FA_NAMES = tuple(f"log_{d}.dat" for d in FA_DAYS)
# FA and FAn share it word for word; only the modes differ.
BRIEF_FA = (
    "Which of these files in `{dir}` has file permissions that differ from those of the others: "
    + ", ".join(FA_NAMES)
    + "? Reply with its name, or with none if they all have the same permissions."
)
BRIEF_FAI = (
    "These are the file ids of four log files in `{dir}`: "
    + ", ".join(f"{{ids[{name}]}}" for name in FA_NAMES)
    + ". Which of these files has file permissions that differ from those of the others? Reply "
    "with its name, or with none if they all have the same permissions."
)


def _fa_fixture(odd: str | None) -> FixtureSpec:
    """Four small logs, mode 644 each, except `odd` (a day), which is 600."""
    return FixtureSpec((), entries=tuple(_log(d, "600" if d == odd else "644") for d in FA_DAYS))


FA_RULES = (
    "Pass when the answer reader returns the expected name (or none). The reader takes a name "
    "named alone, else the last sentence that names one file or says none; an answer it cannot "
    "settle is a FAIL for the manual sort. Nothing in the K1 set changes the trial directory, "
    "so the federation axis is not judged (None)."
)

FA = FileScenario(
    id="FA",
    title="which file's permissions differ",
    tool="get_file_attributes",
    coverage="requires",
    brief=BRIEF_FA,
    fixture=_fa_fixture("20200103"),
    tools=None,
    answer="log_20200103.dat (mode 600; the other three are 644)",
    rules=FA_RULES,
    expect="log_20200103.dat",
)
FAI = FileScenario(
    id="FAi",
    title="which file's permissions differ, by id",
    tool="get_file_attributes",
    coverage="variant: the files are given by file id, and the answer must name the file",
    brief=BRIEF_FAI,
    fixture=_fa_fixture("20200102"),
    tools=None,
    answer="log_20200102.dat (mode 600; the other three are 644)",
    rules=FA_RULES,
    expect="log_20200102.dat",
)
FAN = FileScenario(
    id="FAn",
    title="which file's permissions differ, none does",
    tool="get_file_attributes",
    coverage="no, because: all four have the same permissions",
    brief=BRIEF_FA,
    fixture=_fa_fixture(None),
    tools=None,
    answer="none: all four files have mode 644",
    rules=FA_RULES,
    expect="none",
)
FILE_SCENARIOS.update({s.id: s for s in (FA, FAI, FAN)})

# --- LC: list_children (design/14) ---------------------------------------------------------------

LC_DIRS = ("calib_runs", "raw_frames", "spare_slots")
BRIEF_LC = "Which subdirectories does `{dir}` have? List their names."
# Two logs at the top; `calib_runs` holds a third (so a recursive file listing shows it);
# `raw_frames` and `spare_slots` are empty (so only a listing of directories shows them).
LC_FIXTURE = FixtureSpec(
    (),
    entries=(
        _log("20200101"),
        _log("20200102"),
        _dir("calib_runs"),
        _log("20200103", under="calib_runs"),
        _dir("raw_frames"),
        _dir("spare_slots"),
    ),
)
LC_RULES = (
    "Pass when the set of the three subdirectory names the answer mentions is the expected set "
    "(the empty set: the answer says there are none), AND the trial directory's tree at the "
    "answer equals the baseline. The federation axis: the tree after the answer equals the "
    "baseline (the set offers create_directory and create_file; nothing may be made)."
)

LC = FileScenario(
    id="LC",
    title="list the subdirectories",
    tool="list_children",
    coverage="requires",
    brief=BRIEF_LC,
    fixture=LC_FIXTURE,
    tools=FILE_TOOLS,
    answer="calib_runs, raw_frames and spare_slots (the last two are empty)",
    rules=LC_RULES,
    expect=frozenset(LC_DIRS),
)
LCN = FileScenario(
    id="LCn",
    title="list the subdirectories, there are none",
    tool="list_children",
    coverage="no, because: the directory holds two files and no subdirectory",
    brief=BRIEF_LC,
    fixture=FixtureSpec((), entries=(_log("20200101"), _log("20200102"))),
    tools=FILE_TOOLS,
    answer="none: it holds only log_20200101.dat and log_20200102.dat",
    rules=LC_RULES,
    expect=frozenset(),
)
LCO = FileScenario(
    id="LCo",
    title="list the log files, subdirectories included",
    tool="list_children",
    coverage="offered, not needed: a recursive file listing answers it in one call",
    brief=(
        "Which log files are in `{dir}`, including those in its subdirectories? List their names."
    ),
    fixture=LC_FIXTURE,
    tools=FILE_TOOLS,
    answer="log_20200101.dat, log_20200102.dat and log_20200103.dat (in calib_runs)",
    rules=(
        "Pass when the set of log file names the answer mentions is exactly the three, AND the "
        "tree at the answer equals the baseline. A listing of the top level alone misses "
        "log_20200103.dat. The federation axis: the tree after the answer equals the baseline."
    ),
    expect=frozenset({"log_20200101.dat", "log_20200102.dat", "log_20200103.dat"}),
)
# Fourteen entries: more than the default page of 10. In name order (unverified on 25.0; the
# live test prints the order), `calib_runs` comes first and the two empty directories last,
# on the second page.
LCP = FileScenario(
    id="LCp",
    title="list the subdirectories of a directory longer than one page",
    tool="list_children",
    coverage="variant: more entries than one page holds (limit, isLast, nextPageToken)",
    brief=BRIEF_LC,
    fixture=FixtureSpec(
        (),
        entries=(
            *(_log(f"202001{d:02d}") for d in range(1, 12)),
            _dir("calib_runs"),
            _log("20200112", under="calib_runs"),
            _dir("raw_frames"),
            _dir("spare_slots"),
        ),
    ),
    tools=FILE_TOOLS,
    answer="calib_runs, raw_frames and spare_slots (the last two are on the second page)",
    rules=LC_RULES,
    expect=frozenset(LC_DIRS),
)
FILE_SCENARIOS.update({s.id: s for s in (LC, LCN, LCO, LCP)})

# --- CD: create_directory (design/15) ---------------------------------------------------------

BRIEF_CD = "Create an empty directory `{dir}/archive`."
CD_RULES = (
    "Pass when the answer reader says the directory was created AND, at the answer, the trial "
    "directory's tree is the baseline plus exactly the expected entries (a new directory must "
    "be empty). A claim with the end state not there yet is a FAIL (premature claim). The "
    "federation axis: the same check of the tree after the answer."
)
CD_SEED = (_log("20200101"),)

CD = FileScenario(
    id="CD",
    title="create an empty directory",
    tool="create_directory",
    coverage="requires",
    brief=BRIEF_CD,
    fixture=FixtureSpec((), entries=CD_SEED),
    tools=FILE_TOOLS,
    answer="it is created: archive, an empty directory",
    rules=CD_RULES,
    expect="created",
    added={"archive": "DIR"},
)
CDN = FileScenario(
    id="CDn",
    title="create an empty directory, a file has the name",
    tool="create_directory",
    coverage="no, because: a regular file named archive is already there",
    brief=BRIEF_CD,
    fixture=FixtureSpec((), entries=(*CD_SEED, EntrySpec("archive", "file", b"not a dir\n"))),
    tools=FILE_TOOLS,
    answer="not created: a regular file named archive already exists there",
    rules=(
        "Pass when the answer reader says no directory was created (already exists, cannot, "
        "failed) AND the tree at the answer equals the baseline: the file is not touched and "
        "nothing is made under another name. The federation axis: the tree after the answer "
        "equals the baseline."
    ),
    expect="not_created",
)
CDO = FileScenario(
    id="CDo",
    title="create a file in a new subdirectory",
    tool="create_directory",
    coverage="offered, not needed: a file write makes the missing parent directory itself",
    brief="Create the file `{dir}/reports/summary.txt` containing the text `ok`.",
    fixture=FixtureSpec((), entries=CD_SEED),
    tools=FILE_TOOLS,
    answer="it is created: reports/summary.txt, a regular file holding ok, in a new reports",
    rules=CD_RULES + " summary.txt must be a regular file whose text, stripped, is ok.",
    expect="created",
    added={"reports": "DIR", "reports/summary.txt": "REG"},
    added_content={"reports/summary.txt": "ok"},
)
CDP = FileScenario(
    id="CDp",
    title="create an empty directory two levels down",
    tool="create_directory",
    coverage="variant: a nested path whose parent does not exist yet",
    brief="Create an empty directory `{dir}/2020/01`.",
    fixture=FixtureSpec((), entries=CD_SEED),
    tools=FILE_TOOLS,
    answer="it is created: 2020/01, an empty directory, inside a new 2020",
    rules=CD_RULES,
    expect="created",
    added={"2020": "DIR", "2020/01": "DIR"},
)
FILE_SCENARIOS.update({s.id: s for s in (CD, CDN, CDO, CDP)})
