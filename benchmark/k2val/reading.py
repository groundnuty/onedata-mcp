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

from benchmark.k2val import file_oracles, file_scenarios, oracles, sr_oracles
from benchmark.k2val.rubrics import ANSWER_SPECS, Reading
from benchmark.k2val.scenarios import SCENARIOS, V1_DAYS

DEFAULT_LOCAL = "vistula-dc"
DEFAULT_OWNER = "nordic-marine-dc"
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
    )


Reader = Callable[[str, SimpleNamespace], Reading]

_V1_NAMES = tuple(f"log_{d}.dat" for d in V1_DAYS)

READERS: dict[str, Reader] = {
    "V1": lambda a, w: oracles.read_v1(a, tuple(w.file_names) or _V1_NAMES),
    "V2": lambda a, w: oracles.read_done(a),
    "V3": lambda a, w: oracles.read_v3(a),
    "V3n": lambda a, w: oracles.read_v3n(a),
    "V3o": lambda a, w: oracles.read_v3o(a),
    "V4": lambda a, w: oracles.read_check(a, w.local_name, w.owner_name, writing=False),
    "V5": lambda a, w: oracles.read_check(a, w.local_name, w.owner_name, writing=True),
    "V6": lambda a, w: oracles.read_ready(a, w.local_name, w.owner_name),
    "V7": lambda a, w: oracles.read_ready(a, w.local_name, w.owner_name),
    "SR1": lambda a, w: sr_oracles.read_copy_answer(a),
    "SR1i": lambda a, w: sr_oracles.read_copy_answer(a),
    "SR1n": lambda a, w: sr_oracles.read_refusal(a),
    "SR1o": lambda a, w: sr_oracles.read_copied_question(a, w.local_name, w.owner_name),
    **dict.fromkeys(("FA", "FAi", "FAn"), lambda a, w: file_oracles.fa_names(a)),
    **dict.fromkeys(
        ("LC", "LCn", "LCp", "LCi"),
        lambda a, w: file_oracles.names_claimed(
            a,
            file_scenarios.LC_DIRS,
            (*[p for p in w.directory.split("/") if p], w.local_name, w.owner_name),
        ),
    ),
    "LCo": lambda a, w: file_oracles.log_names_claimed(a),
    **dict.fromkeys(("CD", "CDn", "CDo", "CDp"), lambda a, w: file_oracles.cd_label(a)),
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
    )
    return READERS[sid](answer, who)


_RIGHT: dict[str, Reading] = {
    "V1": frozenset({"log_20200102.dat", "log_20200105.dat"}),
    "V2": "SAYS_DONE",
    "V3": "SAYS_REMOVED",
    "V3n": "SAYS_NO_RULE",
    "V3o": "SAYS_RULE",
    "V4": "NOT_READY_ELSEWHERE",
    "V5": "NOT_READY_BEING_WRITTEN",
    "V6": "SAYS_READY",
    "V7": "SAYS_READY",
    "SR1": "SAYS_WHOLE",
    "SR1i": "SAYS_WHOLE",
    "SR1n": "SAYS_NOT_POSSIBLE",
    "SR1o": "SAYS_NOT_COPIED",
}


def right_reading(sid: str) -> Reading:
    """The reading of a right answer (the oracle may ask more: the state when it was given)."""
    if sid in _RIGHT:
        return _RIGHT[sid]
    spec = file_scenarios.FILE_SCENARIOS[sid]
    if sid.startswith("FA"):
        return file_oracles.fa_names_of(str(spec.expect))
    return spec.expect


def _check_registry() -> None:
    missing = set(SCENARIOS) - set(READERS)
    unspecified = set(SCENARIOS) - set(ANSWER_SPECS)
    if missing or unspecified:
        raise RuntimeError(f"no reader for {sorted(missing)}; no answer spec for {unspecified}")


_check_registry()
