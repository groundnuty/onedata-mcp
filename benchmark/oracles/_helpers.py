"""Small parsing + tool-call inspection helpers used across oracles."""

from __future__ import annotations

import re
from collections.abc import Callable

from benchmark._runtime_types import AgentTrace, ToolCall

# Path-like substring extractor: anything starting with /<space>/...
PATH_RE = re.compile(r"/[A-Za-z][A-Za-z0-9_\-]*/[^\s,;'\"`)\]]+")


def extract_paths(text: str, anchor: str | None = None) -> set[str]:
    """Pull FILE paths from a free-form agent answer.

    Robust against bullet points, backticks, list separators. Optionally
    filter to paths starting with `anchor` (e.g. '/ppam_2026_mcp_tests/').

    Excludes trailing-slash matches (those are directory headers in agent
    prose like "Files in `/space/d2/datasets/`:", NOT file paths the
    agent intended to enumerate). All current callers expect file paths.
    Surfaced 2026-05-02 by D2 live smoke: Claude correctly listed 3 file
    paths but the directory header in its intro line got picked up as a
    spurious 4th path. See research/empirical-onedata-25.0-findings.md.

    Per-line exclusion-marker detection: skips paths on lines that
    explicitly disclaim them. Surfaced 2026-05-02 by P6 (Qwen + GLM):
    agents enumerated all candidate paths in a bullet structure with
    inline annotations like "(so excluded)" or "should not be included",
    and the naive extractor over-counted. Both agents correctly reasoned
    about which paths fit the criterion; their final answers are valid
    English. Oracle now respects the exclusion semantics.

    Three-pass design:
      Pass 1 — per-line: skip paths on lines that contain an exclusion
        marker (catches GLM-style inline `(so excluded)`).
      Pass 2 — section-context: track strong-header sections. A header
        like `**File NOT meeting the criteria:**` opens an exclusion
        section; subsequent paths until the next header or blank line
        are dropped (catches GLM's header pattern observed 2026-05-02
        in run T202740).
      Pass 3 — basename cross-reference: drop any captured path whose
        basename is mentioned on a self-correction line (catches
        Qwen-style `Wait, redundant.bin...` follow-ups where the bullet
        line was captured cleanly but a later line retracts).
    """
    candidate_paths: set[str] = set()
    self_correction_lines: list[str] = []
    in_exclusion_section = False

    for line in text.splitlines():
        stripped = line.strip()
        line_lower = line.lower()

        # Section-state transitions. Strong headers (markdown bold-with-
        # colon `**...:**` or `#`/`##` style) reset the section context;
        # blank lines also end whatever section we were in.
        if not stripped:
            in_exclusion_section = False
            continue
        if _is_strong_header(stripped):
            in_exclusion_section = any(
                phrase in line_lower for phrase in _EXCLUSION_SECTION_HEADERS
            )
            # Header lines themselves don't typically contain paths,
            # but treat them as boundaries either way.
            continue

        is_inline_exclusion = any(marker in line_lower for marker in _EXCLUSION_MARKERS)
        if is_inline_exclusion or in_exclusion_section:
            self_correction_lines.append(line_lower)
            continue

        for path in PATH_RE.findall(line):
            if path.endswith("/"):
                continue
            if anchor and not path.startswith(anchor):
                continue
            candidate_paths.add(path)

    # Pass 3: drop paths whose basename appears on a self-correction line.
    if self_correction_lines:
        retracted: set[str] = set()
        for path in candidate_paths:
            basename = path.rsplit("/", 1)[-1].lower()
            if any(basename in sc for sc in self_correction_lines):
                retracted.add(path)
        candidate_paths -= retracted

    return candidate_paths


def _is_strong_header(line: str) -> bool:
    """True for lines that look like a Markdown header marking a new
    section. Two common shapes in agent prose:
    - `**Header text:**` or `**Header text**` (bold, often colon-suffixed)
    - `# Header` / `## Header` (ATX-style)

    Used by extract_paths to detect section boundaries: a header opens
    a new section whose context (inclusion vs exclusion) is determined
    by phrases inside it.
    """
    if line.startswith("#"):
        return True
    if line.startswith("**") and ("**" in line[2:] or line.endswith(":")):
        # Catches `**text:**`, `**text**`, `**File NOT meeting:**`, etc.
        return True
    return False


# Lines containing any of these markers (case-insensitive) are treated
# as agent self-exclusions: paths on such lines are NOT counted as
# part of the answer set. Conservative list — phrases that strongly
# indicate "I'm listing this path but explicitly rejecting it from my
# answer", not phrases that could appear in a positive description.
_EXCLUSION_MARKERS = (
    "exclud",        # "excluded", "exclude", "excluding"
    "not include",   # "not included", "not include this"
    "shouldn't",
    "should not",
    "do not match",
    "doesn't match",
    "wait,",         # mid-thought self-correction (Qwen pattern)
    "however,",
    "actually,",
    "(distractor",
    "is a distractor",
)

# Phrases that, when they appear in a Markdown-style strong header line,
# signal that the section the header opens contains paths the agent is
# explicitly EXCLUDING from its answer. All paths between this header
# and the next header / blank line are dropped. Caught GLM's
# `**File NOT meeting the criteria:**` pattern observed 2026-05-02.
_EXCLUSION_SECTION_HEADERS = (
    "not meeting",
    "doesn't meet",
    "does not meet",
    "do not meet",
    "not match",
    "doesn't match",
    "do not qualify",
    "doesn't qualify",
    "not included",
    "to exclude",
    "excluded",
)


def extract_int(text: str, key: str) -> int | None:
    """Pull a number associated with `key` from `text`. Returns None if not
    found or not parseable as int.

    Tolerates several output shapes agents typically use:
      'tagged=5'                          — kv equals
      'count: 5'                          — kv colon
      '| CloudSKTest | 3 |'               — markdown table row
      '| StefansSpace (duplicate) | 2 |'  — markdown row with disambig annotation
      'CloudSKTest 3 providers'           — name-then-number
      '`Cloud-SK`: 3'                     — backticked key + colon

    Strategy: locate `key` (literal, case-sensitive), tolerate an optional
    parenthetical annotation immediately after (e.g. ' (duplicate)',
    ' (first)' — Granite uses these to disambiguate Onedata's
    duplicate-allowed space names), then scan forward on the same line
    for the first non-negative integer that's not part of a longer
    alphanumeric token (e.g. ignore '46-g14b5bda7' digits).

    The parenthetical-annotation tolerance was added 2026-05-03 (run
    T232042 Granite v2 D1) when the agent thoughtfully labelled two
    federation spaces named 'StefansSpace' as 'StefansSpace (first)'
    and 'StefansSpace (duplicate)'. The previous pattern's separator
    class did not include `(`, so the annotation broke the lookup.
    """
    # Optional parenthetical annotation after key: tolerates one level
    # of `(...)`. Doesn't recurse into nested parens — those would be
    # bizarre in this context.
    pattern = rf"{re.escape(key)}\s*(?:\([^)]*\)\s*)?[=:|\-`*\s]*(\d+)(?!\w)"
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def extract_kv_lines(text: str) -> dict[str, str]:
    """Pull 'key: value' lines into a dict (best-effort)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().strip("`\"'")
            v = v.strip().strip("`\"',").strip()
            if k and v:
                out[k] = v
    return out


def contains_token(text: str, token: str) -> bool:
    """Case-insensitive substring match — used for 'agent claimed X' checks."""
    return token.lower() in text.lower()


# ---------------------------------------------------------------------------
# Tool-call inspection (mcp_pass axis)
# ---------------------------------------------------------------------------


def find_calls(
    trace: AgentTrace,
    tool_name: str,
    args_predicate: Callable[[dict], bool] | None = None,
) -> list[ToolCall]:
    """Return tool calls matching `tool_name` (and optionally `args_predicate`).

    Used by static + dynamic oracles to verify the agent's MCP-call sequence.
    Both successful and failed calls are returned — Onedata-side failures do
    NOT disqualify the agent (per design/06).
    """
    matches: list[ToolCall] = []
    for call in trace.tool_calls:
        if call.tool_name != tool_name:
            continue
        if args_predicate is None or args_predicate(call.arguments):
            matches.append(call)
    return matches


def has_successful_call(
    trace: AgentTrace,
    tool_name: str,
    args_predicate: Callable[[dict], bool] | None = None,
) -> bool:
    """True if at least one matching tool call returned a non-error response.

    Strictly speaking, per design/06 we count any RECEIVED call as MCP
    success — but for the static-oracle predicate "did the agent
    successfully complete this action" we want at least one call to have
    not errored, since otherwise the agent's reasoning never had a chance
    to read a successful response.

    Use this for "agent successfully wrote X" predicates.
    Use `find_calls` (returns all, including failed) for "agent attempted X".
    """
    return any(call.succeeded for call in find_calls(trace, tool_name, args_predicate))
