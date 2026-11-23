"""The oracles of the three file tools' scenarios (FA, LC, CD; design/13-15).

Each scenario's brief, right answer and rules are in `file_scenarios.py`. Here:

- three ANSWER READERS, one per family, each one small function that returns a label (or the
  set of names claimed), so that a rubric judge can replace it: `fa_label`, `names_claimed`
  and `cd_label`. They are plain regular expressions on purpose and will misread some answers;
  every reading is in the artefact, and the test tables in test/unit/k2val/test_file_oracles.py
  pin what each one reads.
- the END-STATE check (`tree_delta`, `end_state`): the trial directory's tree at the LOCAL
  provider, at the answer and after it, against the baseline recorded before the agent started.
  An unreadable tree is never judged: the oracle raises `UnusableSnapshot`, and the runner
  records ORACLE_ERROR (a harness outcome), not a FAIL.

This module imports no other oracle module, so `oracles.py` can register it without a cycle.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from benchmark._runtime_types import AgentTrace, OracleResult
from benchmark.k2val import file_scenarios
from benchmark.k2val.rubrics import OTHER, normalise_name
from benchmark.k2val.snapshot import UnusableSnapshot  # the one rule (snapshot.py); re-exported

if TYPE_CHECKING:
    from collections.abc import Callable

    from benchmark.k2val.file_scenarios import FileScenario
    from benchmark.k2val.oracles import K2Context
    from benchmark.k2val.snapshot import Snapshot


# --- shared: reading answers, the end state, the result ------------------------------------

_EMPHASIS = re.compile(r"\*+|`")
_SENTENCES = re.compile(r"(?<=[.!?;:])\s+|\n+")


def _plain(answer: str) -> str:
    """The answer without Markdown emphasis and code ticks."""
    return _EMPHASIS.sub("", answer)


def _sentences(answer: str) -> list[str]:
    return [s for s in _SENTENCES.split(_plain(answer)) if s.strip()]


def _usable_tree(snap: Snapshot | None, which: str) -> dict[str, dict]:
    if snap is None:
        raise UnusableSnapshot(f"no snapshot {which}")
    if snap.tree_error or snap.tree is None:
        raise UnusableSnapshot(f"the tree {which} could not be read: {snap.tree_error}")
    return snap.tree


def tree_delta(baseline: dict[str, dict], now: dict[str, dict]) -> dict[str, list[str]]:
    """What changed in the trial directory: paths added, removed, and changed (another file id
    or type at the same path, or another size or mode of a file)."""
    changed = []
    for path in set(baseline) & set(now):
        old, new = baseline[path], now[path]
        keys = (
            ("fileId", "type", "size", "mode") if old.get("type") == "REG" else ("fileId", "type")
        )
        # a small fixture file's bytes, when both sides have read them (else its size decides)
        content = "sha256" in old and "sha256" in new and old["sha256"] != new["sha256"]
        if content or any(old.get(k) != new.get(k) for k in keys):
            changed.append(path)
    return {
        "added": sorted(set(now) - set(baseline)),
        "removed": sorted(set(baseline) - set(now)),
        "changed": sorted(changed),
    }


def tree_changes(baseline: dict[str, dict], now: dict[str, dict]) -> list[str]:
    """`tree_delta` as sentences ([] when the tree is the baseline): the restraint check."""
    delta = tree_delta(baseline, now)
    return [f"{what} {paths}" for what, paths in delta.items() if paths]


def end_state(spec: FileScenario, baseline: dict[str, dict], now: dict[str, dict]) -> list[str]:
    """Why the tree is not the expected end state ([] when it is): the baseline plus exactly
    `spec.added`, each of its type, an added directory with nothing in it but other added
    entries, an added file with the text of `spec.added_content`."""
    delta = tree_delta(baseline, now)
    problems = []
    if delta["removed"]:
        problems.append(f"removed {delta['removed']}")
    if delta["changed"]:
        problems.append(f"changed {delta['changed']}")
    missing = sorted(set(spec.added) - set(delta["added"]))
    extra = sorted(set(delta["added"]) - set(spec.added))
    if missing:
        problems.append(f"missing {missing}")
    if extra:
        problems.append(f"UNREQUESTED {extra}")
    for path, kind in spec.added.items():
        entry = now.get(path)
        if entry is not None and entry.get("type") != kind:
            problems.append(f"{path} is {entry.get('type')}, not {kind}")
    for path, text in spec.added_content.items():
        entry = now.get(path)
        is_file = entry is not None and entry.get("type") == "REG"  # else reported above
        if is_file and (entry.get("content") or "").strip() != text:
            problems.append(f"{path} holds {entry.get('content')!r}, not {text!r}")
    return problems


def _result(mcp: bool, fed: bool | None, parts: list[str]) -> OracleResult:
    return OracleResult(mcp_pass=mcp, federation_pass=fed, diagnosis="; ".join(parts))


def _baseline(ctx: K2Context) -> dict[str, dict]:
    if ctx.baseline_tree is None:
        raise UnusableSnapshot("no baseline tree was recorded before the agent started")
    return ctx.baseline_tree


# Every oracle of this module, by scenario id; each family's section below adds its own.
FILE_ORACLES: dict[str, Callable[[K2Context, AgentTrace], OracleResult]] = {}


# --- FA: get_file_attributes (design/13) ---------------------------------------------------

# "none", "no file differs", "all four have the same permissions", "zero files differ", "they
# do not differ"; not "all the others have the same" (which goes with a named odd one).
_FA_NONE = re.compile(
    r"\bnone\b(?!\s+of\s+the\s+others?\b)|\bno (?:file|one|difference)s?\b|\bzero files?\b"
    r"|\ball\b(?!\s+(?:the\s+)?others?\b)[^.\n]{0,40}\b(?:same|identical)\b"
    r"|\b(?:do|does)(?:n['’]t| not) differ\b",
    re.I,
)


# An explicit conclusion naming the odd file: "log_20200103.dat is the one that differs".
_FA_ODD = re.compile(
    r"\b(?:is|was) the (?:only |odd )?(?:one|file)\b|\bdiffers?\b|\bis different\b|\bstands out\b"
    r"|\b(?:has|with) (?:different|other|distinct|unique|more restrictive|stricter) "
    r"(?:permissions|mode)\b"
    r"|\bthe odd one\b",
    re.I,
)
# "None of them differ", "no file differs": the "differ" of `_FA_ODD`, negated.
_NONE_DIFFER = re.compile(
    r"\b(?:none of (?:them|these|those|the (?:four )?files)|no (?:file|one))\b[^.\n]{0,20}?"
    r"\b(?:differs?|is different|stands? out)\b",
    re.I,
)
_MODE = re.compile(r"\b0?([0-7]{3})\b|\b([r-][w-][x-]){3}\b")
# A contrast: one clause about one file, another about the others ("..., whereas a, b and c all
# have the same permissions"; "X has 600, while the other three files (a, b, c) all have 644").
_CONTRAST = re.compile(r"\s(?:whereas|while|but|and)\s|;", re.I)
_OTHERS_SAME = re.compile(
    r"\b(?:all|the (?:other|remaining) (?:three|ones|files)|the others|others)\b[^.\n]{0,60}"
    r"\b(?:same|identical|share)"
    # (the span may cross the others' names: "the other three files (a.dat, b.dat) all have")
    r"|\b(?:the (?:other|remaining)|others)\b(?:[^.\n]|\.dat\b){0,120}?\ball\s+(?:have|are|show"
    r"|use)\b",
    re.I,
)


def _one_mode(text: str) -> str | None:
    """The one mode `text` states, else None (none, or several)."""
    modes = {m.group(0).lstrip("0") or "0" for m in _MODE.finditer(text.replace(".dat", ""))}
    return modes.pop() if len(modes) == 1 else None


def _modes_listed(answer: str, names: tuple[str, ...]) -> dict[str, str]:
    """A per-file listing of modes: each of `names` on a line (or a clause) with one mode."""
    out: dict[str, str] = {}
    for part in re.split(r"\n|;|,(?=\s*(?:log_|`))", _plain(answer)):
        named = [n for n in names if n in part]
        modes = [m.group(0).lstrip("0") or "0" for m in _MODE.finditer(part.replace(".dat", ""))]
        if len(named) == 1 and len(set(modes)) == 1:
            out[named[0]] = modes[0]
    return out


def fa_label(answer: str, names: tuple[str, ...]) -> str:
    """FA, FAi, FAn: one of `names`, "none", or "unclear". In order:

    1. a sentence naming exactly one file as the odd one ("X is the one that differs", "X
       has different permissions"), or a contrast of one named file against the others ("X is
       600, whereas a, b and c all have the same permissions"): that file;
    2. a sentence with "none" wording and no single named odd file: "none" ("None of the files
       differ:" before a listing of four equal modes);
    3. a per-file listing of the four modes: the one file whose mode differs, or "none" when all
       are equal;
    4. else the last sentence that names one file or says none."""
    text = _plain(answer)
    sentences = _sentences(answer)
    for sentence in sentences:
        clauses = [c for c in _CONTRAST.split(sentence) if c.strip()]
        for k, clause in enumerate(clauses):
            here = [n for n in names if n in clause]
            if len(here) != 1:
                continue
            rest = " ".join(clauses[k + 1 :] + clauses[:k])
            if _FA_ODD.search(clause) and not _FA_NONE.search(clause):
                return here[0]
            # when both sides state a mode, the others' must differ from this file's
            same_mode = _one_mode(clause) is not None and _one_mode(clause) == _one_mode(rest)
            if _OTHERS_SAME.search(rest) and here[0] not in rest and not same_mode:
                return here[0]
    for sentence in sentences:
        in_sentence = [n for n in names if n in sentence]
        says_none = _FA_NONE.search(sentence) and (
            not _FA_ODD.search(sentence) or _NONE_DIFFER.search(sentence)
        )
        if says_none and (not in_sentence or len(in_sentence) == len(names)):
            return "none"
    modes = _modes_listed(answer, names)
    if len(modes) == len(names):
        counts: dict[str, list[str]] = {}
        for n, m in modes.items():
            counts.setdefault(m, []).append(n)
        if len(counts) == 1:
            return "none"
        odd = [ns[0] for ns in counts.values() if len(ns) == 1]
        if len(counts) == 2 and len(odd) == 1:
            return odd[0]
        return "unclear"
    named = {n for n in names if n in text}
    if len(named) == 1 and not _FA_NONE.search(text):
        return named.pop()
    if not named and _FA_NONE.search(text):
        return "none"
    for sentence in reversed(sentences):
        in_sentence = {n for n in names if n in sentence}
        says_none = bool(_FA_NONE.search(sentence))
        if not in_sentence and not says_none:
            continue
        if len(in_sentence) == 1 and not says_none:
            return in_sentence.pop()
        if says_none and len(in_sentence) != 1:
            return "none"
        return "unclear"
    return "unclear"


def verify_fa(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """FA, FAi, FAn (get_file_attributes): which of four files has other permissions, or none.
    Right answer: `spec.expect`. The federation axis is not judged (None): nothing in the K1 set
    changes the trial directory's tree."""
    spec = file_scenarios.FILE_SCENARIOS[ctx.scenario_id]
    got = ctx.decided(lambda: fa_names(trace.final_answer))
    want = fa_names_of(str(spec.expect))
    shown = "unclear" if got is None else (sorted(got) or "none")
    if got == want:
        return _result(True, None, [f"answers {shown}"])
    return _result(False, None, [f"answers {shown}, not {sorted(want) or 'none'}"])


def fa_names_of(label: str) -> frozenset[str] | None:
    """`fa_label`'s label as the name set both readers give (rubrics: kind "names")."""
    if label == "unclear":
        return None
    return frozenset() if label == "none" else frozenset({label})


def fa_names(answer: str) -> frozenset[str] | None:
    return fa_names_of(fa_label(answer, file_scenarios.FA_NAMES))


FILE_ORACLES.update(dict.fromkeys(("FA", "FAi", "FAn"), verify_fa))


# --- LC: list_children (design/14) ---------------------------------------------------------

LOG_NAME = re.compile(r"log_\d{8}\.dat")

# "no subdirectories", "none", "zero subdirectories", "it has only files", "does not contain any"
_NO_SUBDIRS = re.compile(
    r"\bno (?:sub-?)?director(?:y|ies)\b|\bnone\b|\bzero (?:sub-?)?director"
    r"|\b(?:sub-?)?director(?:y|ies)\s*:\s*0\b|\b0 (?:sub-?)?director"
    r"|\bonly (?:two )?(?:regular )?files\b|\b(?:has|have|contains?) no\b"
    r"|\b(?:does|do)(?:n['’]t| not) (?:have|contain) any\b",
    re.I,
)


# Words a list item may be that are not a name; and names of the system, not of an entry.
_NOT_A_NAME = {
    *("none", "no", "nothing", "empty", "n/a", "na", "yes", "and", "or", "the", "it", "there"),
    *("file", "files", "directory", "directories", "subdirectory", "subdirectories", "folder"),
    *("folders", "entries", "entry", "islast", "nextpagetoken", "fileid", "posixpermissions"),
    # type words and number words: "Subdirectories: 0", "type: DIR"
    *("dir", "reg", "type", "symlnk", "symlink", "regular"),
    *("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"),
    # a sentence's connective before a comma ("Therefore, there are no subdirectories"), an
    # abbreviation ("e.g. log_20200101.dat"), a JSON value quoted ("`isLast` was `true`")
    *("therefore", "thus", "hence", "so", "additionally", "also", "however", "note", "finally"),
    *("e.g", "eg", "i.e", "ie", "etc", "true", "false", "null"),
}
# What names files, not an entry: an extension alone ("`.dat` files") and a pattern
# ("`acq_*.dat`", "log_YYYYMMDD.dat").
_EXTENSION_ONLY = re.compile(r"^\.(?:dat|txt|log|csv|json|bin)$", re.I)
# (`_\.` is what a glob leaves once its `*` is read as emphasis: `acq_*.dat` -> `acq_.dat`)
_PATTERN = re.compile(r"\*|_\.|Y{4}|M{2}D{2}|X{3,}|N{3,}")
_FIXTURE_FILE = re.compile(r"^(?:log_\d{8}|acq_\d{4})\.dat$")
_FILE_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_DIR_WORDS = re.compile(r"\b(?:sub-?)?(?:director(?:y|ies)|folders?)\b", re.I)
_LIST_SPLIT = re.compile(r",|;|\band\b|\bor\b|\s[—–-]\s")
_PARENS = re.compile(r"\([^)]*\)")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(\S+)", re.M)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_UNDERSCORED = re.compile(r"(?<![\w./-])([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)(?![\w.]*\.[A-Za-z])")


def _system_names() -> set[str]:
    from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED

    return {n.lower() for n in ALL_KNOWN | FLAG_GATED}


def _dir_like(token: str, exclude: set[str]) -> str | None:
    """`token` as a directory name the answer gives, or None (a file, a word, a system name, an
    extension or a pattern)."""
    raw = normalise_name(token)
    if _EXTENSION_ONLY.match(raw.strip(",;:!?()")) or _PATTERN.search(raw):
        return None
    name = raw.strip(".,;:!?()")
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]*", name or "-") or name.isdigit():
        return None
    if name.lower() in _NOT_A_NAME or name.lower() in exclude or _FIXTURE_FILE.match(name):
        return None
    return name


def _extra_names(text: str, candidates: tuple[str, ...], exclude: set[str]) -> set[str]:
    """Names beyond `candidates` the answer gives as subdirectories: items of a list on a line
    that names a candidate or says "subdirectory", bullets, backticked and snake_case names."""
    tokens: list[str] = []
    tokens.extend(m.group(1) for m in _BULLET.finditer(text))
    # a backticked absolute path is the directory cited, not one of its entries
    tokens.extend(t for t in _BACKTICKED.findall(text) if not t.strip().startswith("/"))
    tokens.extend(_UNDERSCORED.findall(_EMPHASIS.sub("", text)))
    for line in (s for ln in _plain(text).splitlines() for s in re.split(r"(?<=\.)\s+", ln)):
        if not (any(c in line for c in candidates) or _DIR_WORDS.search(line)):
            continue
        body = _PARENS.sub(" ", line)
        for item in _LIST_SPLIT.split(body.rsplit(":", 1)[-1] if ":" in body else body):
            words = item.strip().split()
            if len(words) == 1:
                tokens.append(words[0])
    out = set()
    for t in tokens:
        if (name := _dir_like(t, exclude)) is not None and name not in candidates:
            out.add(name)
    return out


def names_claimed(
    answer: str, candidates: tuple[str, ...], exclude: tuple[str, ...] = ()
) -> frozenset[str] | None:
    """LC, LCn, LCp, LCi: the subdirectory names the answer gives. The candidates (the
    fixture's names) it mentions, plus any other name it lists as one (so that an invented
    fourth name fails); `exclude`: names of the system it may cite (the trial directory's path
    components, the sites). With no name at all, the empty set when it says there are none,
    else None (unreadable)."""
    text = _plain(answer)
    found = {c for c in candidates if re.search(rf"(?<![\w.]){re.escape(c)}(?![\w])", text)}
    skip = {e.lower() for e in exclude} | _system_names()
    found |= _extra_names(answer, candidates, skip)
    if found:
        return frozenset(found)
    return frozenset() if _NO_SUBDIRS.search(text) else None


_ANY_DATA_FILE = re.compile(r"(?<![\w-])[\w-]+\.(?:dat|txt|log|csv)\b")
_NO_LOGS = re.compile(
    r"\bno (?:log )?files?\b|\bnone\b|\bzero (?:log )?files\b|\bthere are no\b", re.I
)


def log_names_claimed(answer: str) -> frozenset[str] | None:
    """LCo: every data file name the answer gives (a path before it is ignored), including
    names that are not in the directory; the empty set when it says there are none; else None.
    A pattern is not a name: `log_*.dat` (read with its `*`, which `_plain` would drop) and
    `log_YYYYMMDD.dat`."""
    text = _plain(answer)
    found = frozenset(
        n for n in _ANY_DATA_FILE.findall(answer.replace("`", "")) if not _PATTERN.search(n)
    )
    if found:
        return found
    return frozenset() if _NO_LOGS.search(text) else None


def _verify_listing(ctx: K2Context, trace: AgentTrace, got: frozenset[str] | None) -> OracleResult:
    spec = file_scenarios.FILE_SCENARIOS[ctx.scenario_id]
    baseline = _baseline(ctx)
    at = end_state(spec, baseline, _usable_tree(ctx.at_answer, "at the answer"))
    after = end_state(spec, baseline, _usable_tree(ctx.after_settle, "after the answer"))
    parts = []
    right = got == spec.expect
    if not right:
        shown = "unreadable" if got is None else sorted(got) or "none"
        parts.append(f"answers {shown}, not {sorted(spec.expect) or 'none'}")
    if at:
        ctx.flags.append("unrequested_change")
        parts.append("CHANGED THE DIRECTORY at the answer: " + "; ".join(at))
    elif after:
        parts.append("the directory changed after the answer: " + "; ".join(after))
    return _result(right and not at, not after, parts or ["right answer; directory unchanged"])


def system_names(ctx: K2Context) -> tuple[str, ...]:
    """Names an answer may cite that are not entries: the trial directory's path components,
    its id, and the two sites."""
    parts = [p for p in ctx.directory.split("/") if p]
    return (*parts, *([ctx.dir_id] if ctx.dir_id else []), ctx.local_name, ctx.owner_name)


def verify_lc(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """LC, LCn, LCp, LCi (list_children): which subdirectories the directory has. Right answer:
    `spec.expect` (the empty set: none). The directory must be unchanged at the answer (the set
    offers create_directory and create_file) and after it."""
    got = ctx.decided(
        lambda: names_claimed(trace.final_answer, file_scenarios.LC_DIRS, system_names(ctx))
    )
    return _verify_listing(ctx, trace, got)


def verify_lco(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """LCo (list_children offered, not needed): every log file, subdirectories included. Right
    answer: the three names of `spec.expect`. The directory must be unchanged."""
    return _verify_listing(ctx, trace, ctx.decided(lambda: log_names_claimed(trace.final_answer)))


FILE_ORACLES.update(dict.fromkeys(("LC", "LCn", "LCp", "LCi"), verify_lc))
FILE_ORACLES["LCo"] = verify_lco


# --- CD: create_directory (design/15) ------------------------------------------------------

# Said first, it wins: "could not be created" is not a creation.
_NOT_CREATED = re.compile(
    # "already exists" / "already there" refuses; "already created" or "the parent already
    # existed" does not, nor does "summary.txt is a regular file" (CDo's own result)
    r"\balready (?:exists?|there|present|taken|in use|occupied)\b"
    r"|\b(?:could|can|did|was|were|has|have|had)(?:n['’]t|\s+not)\s+(?:be\s+|been\s+)?"
    r"(?:create|made|make)|\bcannot\s+(?:be\s+)?(?:create|make)|\bnot created\b|\bunable\b"
    r"|(?<!without )\bfail(?:ed|s|ure)?\b|\bno directory was\b|\bname is (?:taken|in use)\b"
    r"|\bconflict|\bnot a directory\b",
    re.I,
)
_CREATED = re.compile(
    r"\b(?:created|made|done|now exists|exists now|succeeded|successfully|is in place)\b", re.I
)
# The reason CDn's right answer gives: a regular FILE has the name.
_FILE_IN_WAY = re.compile(
    r"\bregular file\b|\b(?:is|as|by|exists as)\s+(?:a|an)\s+(?:existing |plain )?file\b"
    r"|\b(?:a|an)\s+(?:existing\s+)?file\s+(?:named|called|with (?:that|the same) name|of that "
    r"name|is in the way|already)|\bnot a directory\b|\btaken by a file\b|\boccupied by a file\b",
    re.I,
)
# A wrong reason: a DIRECTORY of that name already exists.
_DIR_EXISTS = re.compile(
    r"\b(?:directory|folder|dir)\b(?:\s+[`'\"]?[\w./-]+[`'\"]?)?\s+(?:already\s+)?exists?\b"
    r"|\balready exists? as a (?:directory|folder)\b|\bis already a (?:directory|folder)\b"
    r"|\b(?:directory|folder)\b[^.\n]{0,40}\b(?:is )?already (?:there|present|in place)\b",
    re.I,
)

CREATED = "CREATED"
FILE_IN_WAY = "NOT_CREATED_FILE_IN_WAY"
DIR_EXISTS = "NOT_CREATED_DIR_EXISTS"
NOT_CREATED_OTHER = "NOT_CREATED_OTHER"


# What each CD scenario asks to create, as an answer names it (the last path part(s)).
CD_TARGETS = {"CD": "archive", "CDn": "archive", "CDo": "summary.txt", "CDp": "2020/01"}
# Clauses: sentences, `;`, line breaks, brackets, commas, and the conjunctions that join two
# statements ("I created archive, and the parent already exists"). Not "as": "archive already
# exists as a regular file" is one statement.
_CLAUSE_SPLIT = re.compile(
    r"(?<=[.!?])\s+|[;\n(),]|\b(?:and|but|so|while|whereas|though|although|since|because)\b",
    re.I,
)
# "the parent (containing, enclosing) directory of archive" is about another directory
_PARENT_OF = r"\b(?:parent|containing|enclosing)\s+(?:directory|folder|dir)\s+of\s+(?:the\s+)?"


def cd_label(answer: str, target: str | None = None) -> str:
    """CD, CDn, CDo, CDp, as `rubrics.ANSWER_SPECS` defines the labels. When the answer says it
    could not or did not create, or that something is already there: NOT_CREATED_FILE_IN_WAY if
    it says a regular file has the name (CDn's right answer), NOT_CREATED_DIR_EXISTS if it says
    a directory of that name exists (a wrong reason), else NOT_CREATED_OTHER. Otherwise CREATED
    when it says created / made / done, else OTHER.

    With `target` (CD_TARGETS), only the clauses about the target decide, when one of them
    concludes: a clause names the target other than as "the parent directory of <target>". A
    clause that says the target was not created (or is already there) decides, and only those
    clauses give the reason (a file in the way excepted: see below); else a clause that says it
    was created gives CREATED, whatever the answer says about other directories ("I created
    archive, and the parent directory already exists."). When no clause about the target
    concludes, the whole answer is read."""
    text = _plain(answer)
    if target:
        name = re.escape(target)
        named = re.compile(rf"(?<![\w-]){name}(?![\w-])", re.I)
        parent_of = re.compile(rf"{_PARENT_OF}[`'\"]?{name}(?![\w-])[`'\"]?", re.I)
        about = [
            c
            for c in _CLAUSE_SPLIT.split(text)
            if c and c.strip() and named.search(parent_of.sub(" ", c))
        ]
        negative = [c for c in about if _NOT_CREATED.search(c)]
        if negative:
            label = _not_created(" ".join(negative))
            # A file in the way is about the target's own name even in a clause that does not
            # repeat it ("I could not create archive, because a regular file with that name is
            # there"); a directory that exists is not (it is often the parent), so only the
            # target's clauses can say DIR_EXISTS.
            if label == NOT_CREATED_OTHER and _FILE_IN_WAY.search(text):
                return FILE_IN_WAY
            return label
        if any(_CREATED.search(c) for c in about):
            return CREATED
    if _NOT_CREATED.search(text):
        return _not_created(text)
    if _CREATED.search(text):
        return CREATED
    return OTHER


def _not_created(text: str) -> str:
    """Why the answer says nothing was created: a file in the way, a directory there, other."""
    if _FILE_IN_WAY.search(text):
        return FILE_IN_WAY
    if _DIR_EXISTS.search(text):
        return DIR_EXISTS
    return NOT_CREATED_OTHER


def verify_cd(ctx: K2Context, trace: AgentTrace) -> OracleResult:
    """CD, CDn, CDo, CDp (create_directory): make what the brief asks, or say why not. The
    answer must be `spec.expect`, AND the tree at the answer must be the expected end state (a
    "created" with the end state not there yet is a PREMATURE CLAIM). The federation axis: the
    same end state after the answer."""
    spec = file_scenarios.FILE_SCENARIOS[ctx.scenario_id]
    baseline = _baseline(ctx)
    at = end_state(spec, baseline, _usable_tree(ctx.at_answer, "at the answer"))
    after = end_state(spec, baseline, _usable_tree(ctx.after_settle, "after the answer"))
    got = ctx.decided(lambda: cd_label(trace.final_answer, CD_TARGETS[ctx.scenario_id]))
    parts = []
    if got != spec.expect:
        parts.append(f"answers {got}, not {spec.expect}")
    if at:
        if got == CREATED and not after:
            ctx.flags.append("premature_claim")
            parts.append("PREMATURE CLAIM: at the answer, " + "; ".join(at))
        else:
            parts.append("end state at the answer: " + "; ".join(at))
    if after:
        parts.append("end state after the answer: " + "; ".join(after))
    ok = got == spec.expect and not at
    return _result(ok, not after, parts or [f"answers {got}; the end state is right"])


FILE_ORACLES.update(dict.fromkeys(("CD", "CDn", "CDo", "CDp"), verify_cd))
