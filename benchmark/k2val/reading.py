"""The deterministic reader of every scenario, in one shape: `read_answer(sid, answer, ...)`
returns a value of the scenario's `AnswerSpec` (`rubrics.py`): a label, or a set of names
(None: no clear set). The judge (`judge.py`) returns the same kind of value; `scoring.py` puts
the two together (design/10, gate 4: "Each final answer is read twice").

The readers themselves are the regular expressions next to each oracle (`oracles.py`,
`sr_oracles.py`, `file_oracles.py`); this module only adapts their arguments, so a reader can
also run offline on a stored artefact (`rejudge.py`), and says which reading a right answer has
(`right_reading`), where that does not depend on more than the label.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from benchmark.k2val import (
    file_oracles,
    file_scenarios,
    oracles,
    readiness_oracles,
    sr_oracles,
    stable_oracles,
)
from benchmark.k2val.rubrics import ANSWER_SPECS, Reading
from benchmark.k2val.scenarios import SCENARIOS, V1_DAYS

DEFAULT_LOCAL = "vistula-dc"
DEFAULT_OWNER = "nordic-marine-dc"
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_PATH_IN_BRIEF = re.compile(r"`(/[^`]+?)(?:/log_\d{8}\.dat|/archive|/2020/01|/reports/[^`]*)?`")


def _who(row: dict[str, Any] | None, local: str | None) -> SimpleNamespace:
    """What a reader needs beside the answer: the sites, the offered file names, the trial
    directory; from the artefact's `oracle_context` when it has one, else the defaults."""
    row = row or {}
    meta = row.get("oracle_context") or {}
    sid = row.get("scenario_id", "")
    files = meta.get("file_names") or [f.name for f in SCENARIOS[sid].fixture.files] if sid else []
    directory = meta.get("directory") or row.get("directory") or ""
    if not directory and (m := _PATH_IN_BRIEF.search(row.get("brief") or "")):
        directory = m.group(1)
    return SimpleNamespace(
        local_name=meta.get("local_name") or local or DEFAULT_LOCAL,
        owner_name=meta.get("owner_name") or DEFAULT_OWNER,
        file_names=list(files),
        directory=directory,
        # what the brief gave in backticks (an id, a path): cited, never an entry's name
        cited=tuple(_BACKTICKED.findall(row.get("brief") or "")),
        dir_id=meta.get("dir_id") or "",
    )


Reader = Callable[[str, SimpleNamespace], Reading]

_V1_NAMES = tuple(f"log_{d}.dat" for d in V1_DAYS)

READERS: dict[str, Reader] = {
    "V1": lambda a, w: oracles.read_v1(a, tuple(w.file_names) or _V1_NAMES),
    "V2": lambda a, w: oracles.read_done(a),
    **dict.fromkeys(("V3", "V3i", "V3n"), lambda a, w: oracles.read_removal_task(a)),
    "V3o": lambda a, w: oracles.read_v3o(a),
    **dict.fromkeys(("V4", "V5"), lambda a, w: oracles.read_check(a, w.local_name, w.owner_name)),
    "V6": lambda a, w: oracles.read_ready(a, w.local_name, w.owner_name),
    "V7": lambda a, w: oracles.read_ready(a, w.local_name, w.owner_name),
    **dict.fromkeys(
        ("SR1", "SR1i"), lambda a, w: sr_oracles.read_copy_answer(a, w.local_name, w.owner_name)
    ),
    "SR1n": lambda a, w: sr_oracles.read_refusal(a),
    "SR1o": lambda a, w: sr_oracles.read_copied_question(a, w.local_name, w.owner_name),
    **dict.fromkeys(("FA", "FAi", "FAn"), lambda a, w: file_oracles.fa_names(a)),
    **dict.fromkeys(
        ("LC", "LCn", "LCp", "LCi"),
        lambda a, w: file_oracles.names_claimed(
            a,
            file_scenarios.LC_DIRS,
            (
                *[p for p in w.directory.split("/") if p],
                *[c.strip().strip("/") for c in w.cited if "/" not in c.strip().strip("/")],
                *([w.dir_id] if w.dir_id else []),
                w.local_name,
                w.owner_name,
            ),
        ),
    ),
    "LCo": lambda a, w: file_oracles.log_names_claimed(a),
    **{
        sid: (lambda a, w, t=target: file_oracles.cd_label(a, t))
        for sid, target in file_oracles.CD_TARGETS.items()
    },
    # the stable tools' smoke scenarios (design/16)
    **stable_oracles.ST_READERS,
    # the readiness tools' scenarios (design/17, design/18)
    **readiness_oracles.READINESS_READERS,
}


def read_answer(
    sid: str, answer: str, row: dict[str, Any] | None = None, *, local: str | None = None
) -> Reading:
    """The deterministic reading of `answer` in scenario `sid`."""
    row = {"scenario_id": sid, **(row or {})}
    return READERS[sid](answer, _who(row, local))


def read_in_context(sid: str, answer: str, ctx: Any) -> Reading:
    """The deterministic reading during a trial, from its `K2Context`."""
    who = SimpleNamespace(
        local_name=ctx.local_name,
        owner_name=ctx.owner_name,
        file_names=list(ctx.file_names),
        directory=ctx.directory,
        cited=(),
        dir_id=ctx.dir_id,
    )
    return READERS[sid](answer, who)


_RIGHT: dict[str, Reading] = {
    "V1": frozenset({"log_20200102.dat", "log_20200105.dat"}),
    "V2": "SAYS_DONE",
    "V3": "REMOVED",
    "V3i": "REMOVED",
    "V3n": "NO_RULE",
    "V3o": "SAYS_RULE",
    "V4": ("NOT_READY_ELSEWHERE", frozenset()),
    "V5": ("NOT_READY_WRITING", frozenset({"log_20200102.dat", "log_20200104.dat"})),
    "V6": "SAYS_READY",
    "V7": "SAYS_READY",
    "SR1": "SAYS_WHOLE",
    "SR1i": "SAYS_WHOLE",
    "SR1n": "SAYS_NOT_POSSIBLE",
    "SR1o": "SAYS_NOT_COPIED",
    # None for ST4: its right reading is the trial's own transfer id
    **stable_oracles.ST_RIGHT,
    **readiness_oracles.READINESS_RIGHT,  # type: ignore[dict-item]
}


def right_reading(sid: str) -> Reading:
    """The reading of a right answer (the oracle may ask more: the state when it was given)."""
    if sid in _RIGHT:
        return _RIGHT[sid]
    spec = file_scenarios.FILE_SCENARIOS[sid]
    if sid.startswith("FA"):
        return file_oracles.fa_names_of(str(spec.expect))
    return spec.expect


def is_right(sid: str, value: Reading) -> bool | None:
    """Whether a reading is a right answer's, as far as the label goes (the oracle may ask
    more: the state when it was given). V5: the right label, and whatever it says of the files
    being written true: each file it names is one of the two, each count it states is two
    (`oracles.writing_claim_problems`); naming or counting none is right (the brief asks for
    neither). None where the right reading depends on the trial (ST4: its own transfer id), so
    it cannot be told from the label."""
    from benchmark.k2val.rubrics import same_reading

    if sid in _RIGHT and _RIGHT[sid] is None:
        return None
    if sid == "V5":
        return (
            isinstance(value, tuple)
            and value[0] == "NOT_READY_WRITING"
            and not oracles.writing_claim_problems(frozenset(value[1]), oracles.S2_NAMES, 5)
        )
    if sid == "V4":
        return isinstance(value, tuple) and value[0] == "NOT_READY_ELSEWHERE"
    if (right := readiness_oracles.is_right(sid, value)) is not None:
        return right
    return same_reading(value, right_reading(sid))


def _check_registry() -> None:
    missing = set(SCENARIOS) - set(READERS)
    unspecified = set(SCENARIOS) - set(ANSWER_SPECS)
    if missing or unspecified:
        raise RuntimeError(f"no reader for {sorted(missing)}; no answer spec for {unspecified}")


_check_registry()
