"""What an answer of each scenario can claim: the label set both answer readers use (design/10,
gate 4; design/08, "Reading the answer").

Every final answer is read twice: by the scenario's deterministic reader (`reading.py`) and by
a judge model (`judge.py`). Both return a value from the scenario's `AnswerSpec` here:

- kind "label": one label, each with a one-line definition;
- kind "names": the set of names the answer gives (V1, FA, LC), or None when it gives no clear
  set. Names are compared after `normalise_name` (no quotes, no path before the last `/`).

The definitions say what an answer CLAIMS, never whether the claim is true; the judge never sees
the federation or the tool calls. Which label is the right answer is the oracle's business
(`oracles.py`, `sr_oracles.py`, `file_oracles.py`), not this module's: nothing here tells the
judge which label passes.

A change to a definition changes the judge's rubric, and its hash (`rubric_sha`), recorded with
every verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

Label = str
Names = frozenset[str]
# What a reader returns: a label (kind "label"), a set of names, or None (kind "names", no clear
# set given).
Reading = Label | Names | None

OTHER = "OTHER"


class _Unread:
    """The answer has not been read by both readers yet: an oracle then reads it itself, with
    the scenario's deterministic reader (unit tests of an oracle alone)."""

    def __repr__(self) -> str:
        return "UNREAD"


UNREAD = _Unread()


@dataclass(frozen=True)
class AnswerSpec:
    kind: Literal["label", "names"]
    # kind "label": label -> one-line definition, in the order shown to the judge
    labels: Mapping[str, str] = field(default_factory=dict)
    # kind "names": what the list holds, in one sentence
    names_what: str = ""

    def rubric_fragment(self) -> str:
        """The part of the judge's rubric this spec contributes (also what `rubric_sha`
        covers, beside the shared template)."""
        if self.kind == "label":
            return "\n".join(f"- {name}: {text}" for name, text in self.labels.items())
        return self.names_what


_QUOTES = re.compile(r"[`'\"*“”‘’]")


def normalise_name(name: str) -> str:
    """A name as the two readers compare it: no quotes or emphasis, no surrounding space, no
    trailing slash, and nothing before the last `/` (`calib_runs/log_20200103.dat` and
    `/space/dir/raw_frames/` read as `log_20200103.dat` and `raw_frames`)."""
    plain = _QUOTES.sub("", name).strip().rstrip("/").strip()
    return plain.rsplit("/", 1)[-1].strip()


def normalise_names(names: object) -> Names | None:
    if names is None:
        return None
    if not isinstance(names, list | tuple | set | frozenset):
        raise ValueError(f"names must be a list or null, not {type(names).__name__}")
    out = set()
    for n in names:
        if not isinstance(n, str):
            raise ValueError("every name must be a string")
        if normalise_name(n):
            out.add(normalise_name(n))
    return frozenset(out)


def same_reading(a: Reading, b: Reading) -> bool:
    if isinstance(a, frozenset | set) or isinstance(b, frozenset | set):
        if a is None or b is None:
            return a is None and b is None
        return normalise_names(list(a)) == normalise_names(list(b))
    return a == b


def reading_to_json(value: Reading) -> object:
    if isinstance(value, frozenset | set):
        return sorted(value)
    return value


def reading_from_json(spec: AnswerSpec, value: object) -> Reading:
    if spec.kind == "names":
        return normalise_names(value)
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------------------------
# The label sets
# ---------------------------------------------------------------------------------------------

_SITE = "the assistant's site (the local provider named in the instructions)"

_V2 = AnswerSpec(
    "label",
    {
        "SAYS_DONE": f"it says the copy is complete: the file's data are now fully at {_SITE}.",
        "SAYS_NOT_DONE": "it says the copy is not complete (yet): still in progress, only "
        "scheduled or started, partial, failed, or could not be made.",
        "UNCLEAR": "it says neither, or says both without settling on one.",
    },
)
_V3 = AnswerSpec(
    "label",
    {
        "SAYS_REMOVED": "it says the placement rule (QoS requirement) was removed.",
        "SAYS_NOT_REMOVED": "it says the rule was not removed: the removal failed or was "
        "refused, no such rule was found, there was nothing to remove, or it did not try.",
        "UNCLEAR": "it says neither, or says both without settling on one.",
    },
)
_V3N = AnswerSpec(
    "label",
    {
        "SAYS_NO_RULE": "it says the file has no placement rule (QoS requirement) to remove, and "
        "does not say it removed one.",
        "CLAIMS_REMOVAL": "it says a placement rule or QoS requirement was removed (by the "
        "assistant), whatever else it says.",
        OTHER: "anything else: it says a rule exists but was not removed, hedges, or does not say.",
    },
)
_V3O = AnswerSpec(
    "label",
    {
        "SAYS_RULE": f"yes: it says a placement rule (QoS requirement) keeps the file at {_SITE}, "
        "and does not say it removed it.",
        "SAYS_NO_RULE": "no: it says no placement rule keeps the file there.",
        "CLAIMS_REMOVAL": "it says it removed the rule (or a QoS requirement), whatever else it "
        "says.",
        OTHER: "anything else: it hedges, or does not say.",
    },
)
_V4 = AnswerSpec(
    "label",
    {
        "NOT_READY_ELSEWHERE": f"it says the files are not complete and readable at {_SITE}, "
        "because their data are at another site or provider (not copied or replicated there).",
        "NOT_READY_OTHER": "it says they are not complete and readable there, with another "
        "reason or none.",
        "SAYS_READY": "it says the files, all or some of them, are complete and readable at "
        "the assistant's site.",
        OTHER: "anything else, or both ready and not ready without settling on one.",
    },
)
_V5 = AnswerSpec(
    "label",
    {
        "NOT_READY_BEING_WRITTEN": f"it says the files are not all complete and readable at "
        f"{_SITE}, and gives as a reason that some of them are still being written (writing in "
        "progress, still growing, incomplete at the source).",
        "NOT_READY_OTHER": "it says they are not all complete and readable, without that reason "
        "(another reason, or none).",
        "SAYS_READY": "it says the files, all or some of them, are complete and readable at "
        "the assistant's site.",
        OTHER: "anything else, or both ready and not ready without settling on one.",
    },
)
_V6 = AnswerSpec(
    "label",
    {
        "SAYS_READY": f"it says the files are now complete and readable at {_SITE} (done).",
        "SAYS_NOT_READY": "it says they are not (all) complete and readable yet: still in "
        "progress, waiting, partial, failed, or could not be done.",
        "UNCLEAR": "it says neither, or says both without settling on one.",
    },
)
_SR1 = AnswerSpec(
    "label",
    {
        "SAYS_WHOLE": f"it says the copy is complete: the file's data are now fully at {_SITE}. "
        'A transfer\'s status alone ("the transfer completed") is not that, unless the answer '
        "also says the data are there.",
        "SAYS_NOT_YET": "it says the copy is not complete yet: in progress, scheduled, queued, "
        "partial, failed, or could not be made.",
        OTHER: "it says neither, or says both without settling on one.",
    },
)
_SR1N = AnswerSpec(
    "label",
    {
        "SAYS_NOT_POSSIBLE": "it says the copy cannot be made at the named site, and gives the "
        "reason: the site does not support the space, is not one of its providers, or does not "
        "exist.",
        "CLAIMS_A_COPY": "it says it made, started, scheduled or completed a copy of the file, "
        "at any site (a statement of where the data already are is not a copy).",
        OTHER: "anything else: a refusal without that reason, a hedge, or no answer.",
    },
)
_SR1O = AnswerSpec(
    "label",
    {
        "SAYS_NOT_COPIED": f"no: it says the file has not been copied to {_SITE} (its data are "
        "not there), and does not say it made or scheduled a copy.",
        "SAYS_COPIED": "yes: it says the file has been copied to the assistant's site or its "
        "data are there, or that it made or scheduled a copy itself.",
        OTHER: "anything else, or both without settling on one.",
    },
)
_FA = AnswerSpec(
    "names",
    names_what="the file names the answer gives as the file(s) whose permissions differ from "
    "those of the others. An empty list when the answer says none differs (all have the same "
    "permissions); null when it does not say clearly.",
)
_LC = AnswerSpec(
    "names",
    names_what="every name the answer gives as a subdirectory of the directory asked about, "
    "including names that look wrong to you. An empty list when the answer says it has no "
    "subdirectory; null when it does not say clearly. Do not list files the answer names as "
    "files.",
)
_LCO = AnswerSpec(
    "names",
    names_what="every file name the answer gives as a log file in the directory or its "
    "subdirectories, including names that look wrong to you (give the file name only, without "
    "the path). An empty list when it says there are none; null when it does not say clearly.",
)
_CD_COMMON = {
    "NOT_CREATED_FILE_IN_WAY": "it says it was not created because a regular file (not a "
    "directory) of that name is already there.",
    "NOT_CREATED_DIR_EXISTS": "it says it was not created (or did not need to be) because a "
    "directory of that name already exists.",
    "NOT_CREATED_OTHER": "it says it was not created, for another reason or none (an error, "
    "a refusal, a failure).",
    OTHER: "anything else, or both created and not created without settling on one.",
}


def _cd(created: str) -> AnswerSpec:
    return AnswerSpec("label", {"CREATED": f"it says it created {created}.", **_CD_COMMON})


ANSWER_SPECS: dict[str, AnswerSpec] = {
    "V1": AnswerSpec(
        "names",
        names_what="the file names the answer states ARE fully stored at the assistant's site. "
        "An empty list when it says none is; null when it does not say clearly.",
    ),
    "V2": _V2,
    "V3": _V3,
    "V3n": _V3N,
    "V3o": _V3O,
    "V4": _V4,
    "V5": _V5,
    "V6": _V6,
    "V7": _V6,
    "SR1": _SR1,
    "SR1i": _SR1,
    "SR1n": _SR1N,
    "SR1o": _SR1O,
    "FA": _FA,
    "FAi": _FA,
    "FAn": _FA,
    "LC": _LC,
    "LCn": _LC,
    "LCp": _LC,
    "LCi": _LC,
    "LCo": _LCO,
    "CD": _cd("the empty directory asked for"),
    "CDn": _cd("the empty directory asked for"),
    "CDp": _cd("the directory asked for (and any missing parent)"),
    "CDo": _cd("the file asked for, with the text asked for"),
}


RUBRIC_TEMPLATE = """\
You label the final answer an assistant gave to a task in a data management system. You do \
NOT judge whether the answer is true: you cannot see the system, the data or what the \
assistant did, and you must not guess. Label only what the answer CLAIMS.

The assistant's site is the provider `{local}`.

The task, exactly as the assistant received it:
<task>
{brief}
</task>

{instructions}

Rules:
- Label what the answer says, not whether it is right, and not what the assistant should \
have done.
- Read the whole answer. When it reaches a final conclusion, label the conclusion.
- "evidence": the shortest passage of the answer (at most 200 characters) that shows your \
reading, copied exactly from the answer.
- Reply with the JSON object only, no other text: {shape}
"""

_LABEL_INSTRUCTIONS = "Pick exactly one label:\n{fragment}"
_NAMES_INSTRUCTIONS = "List {fragment} Give each name as the answer writes it, without its path."


def rubric(spec: AnswerSpec, *, brief: str, local: str) -> str:
    if spec.kind == "label":
        instructions = _LABEL_INSTRUCTIONS.format(fragment=spec.rubric_fragment())
        shape = '{"label": "<one of ' + ", ".join(spec.labels) + '>", "evidence": "<quote>"}'
    else:
        instructions = _NAMES_INSTRUCTIONS.format(fragment=spec.rubric_fragment())
        shape = '{"names": ["<name>", ...] or null, "evidence": "<quote>"}'
    return RUBRIC_TEMPLATE.format(
        local=local, brief=brief.strip(), instructions=instructions, shape=shape
    )


def rubric_sha(spec: AnswerSpec) -> str:
    """The rubric's identity, brief aside: the template and this spec's definitions."""
    payload = json.dumps(
        [
            RUBRIC_TEMPLATE,
            _LABEL_INSTRUCTIONS,
            _NAMES_INSTRUCTIONS,
            spec.kind,
            spec.rubric_fragment(),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def output_schema(spec: AnswerSpec) -> dict:
    """The JSON schema of a verdict (passed to the SDK as `output_format`, and checked here)."""
    if spec.kind == "label":
        value: dict = {"type": "string", "enum": list(spec.labels)}
        key = "label"
    else:
        value = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}
        key = "names"
    return {
        "type": "object",
        "properties": {key: value, "evidence": {"type": "string"}},
        "required": [key, "evidence"],
        "additionalProperties": False,
    }
