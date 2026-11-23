"""Per-tool maturity + provenance classification, and launch-time selection.

**Model-invisible.** None of this appears in the tool descriptions the LLM
reads. It records each tool's maturity and drives which tools the server
exposes at launch (``ONEDATA_MCP_MATURITY`` / ``ONEDATA_MCP_TOOLS``).

Two axes:

``maturity``
  ``stable``       — passed the promotion protocol at its current surface
                     (design/10-tool-maturity.md): validated with several LLMs
                     against a live Onedata federation.
  ``experimental`` — registered, but not (or not yet again) through that
                     protocol. Every new tool starts here.

``origin`` (a distrust signal for anything unvalidated)
  ``ours``     — authored here after the upstream base commit (85b8515).
  ``upstream`` — inherited from the upstream base surface, or adapted from its
                 later commits (two of the flag-gated tools, see ``FLAG_GATED``).

Validation trumps origin: an upstream-origin tool that passed validation is
``stable``. The least-trusted set is ``experimental`` ∩ ``upstream``, plus the
flag-gated tools, which are off the default list altogether.

The initial ``stable`` set is the 16 tools the earlier multi-LLM benchmark
evaluated (``benchmark.tool_allowlist.HEADLINE``, frozen as that benchmark's
record). ``STABLE`` must contain it and grows only by promotion; a unit test
asserts the containment (the server package must not import the benchmark).
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# --- classification (the 23 tools registered by default) ---

# maturity == "stable": passed the promotion protocol (design/10-tool-maturity.md).
STABLE: frozenset[str] = frozenset(
    {
        "add_file_qos_requirement",
        "create_directory",  # promoted 2026-11-23, design/15
        "create_file",
        "delete_file",
        "download_file",
        "get_file_distribution",
        "get_file_metadata",
        "get_file_qos_summary",
        "get_qos_requirement",
        "get_transfer",
        "list_files_recursively",
        "list_space_providers",
        "list_space_transfers",
        "list_user_spaces",
        "move_file",
        "query_by_metadata",
        "remove_qos_requirement",  # promoted 2026-11-21, design/11
        "set_file_metadata",
    }
)

# maturity == "experimental": registered, not (yet) through the promotion protocol.
EXPERIMENTAL: frozenset[str] = frozenset(
    {
        # ours, not yet validated
        "schedule_file_replication",
        # ours, the readiness tools (design/07-readiness-tools.md)
        "check_readiness",
        "ensure_readiness",
        # inherited from the upstream base surface, not yet validated (the
        # genuine distrust set of the default list)
        "get_file_attributes",
        "list_children",
    }
)

# origin == "upstream": present at the upstream base commit (85b8515).
# Everything else is "ours".
UPSTREAM: frozenset[str] = frozenset(
    {
        "create_file",
        "delete_file",
        "download_file",
        "get_file_attributes",
        "get_file_id",
        "get_file_metadata",
        "get_harvester_index_schema",
        "grep_file_content",
        "list_children",
        "list_files_recursively",
        "list_marketplace_spaces",
        "list_user_harvesters",
        "list_user_spaces",
        "query_harvester_index",
        "set_file_metadata",
    }
)

# Registered ONLY with the preview flag ONEDATA_MCP_EXPERIMENTAL
# (onedata_mcp/experimental.py): with it off they do not exist on the server, so
# they are kept out of ALL_KNOWN, the surface registered by default. All are
# ``experimental`` (``maturity_of`` already answers that for anything outside
# STABLE) and ``upstream`` in origin (``origin_of`` answers that for every member).
# The first two were adapted from later upstream commits (63c21b5, 46f030a), so they
# are not in UPSTREAM; the other six are inherited from the base commit and stay in
# it. The six were moved off the default list (design/10-tool-maturity.md, "Leaving
# the default surface"); each comment says why. They can return to the default list
# through the promotion protocol.
FLAG_GATED: frozenset[str] = frozenset(
    {
        "list_space_datasets",
        "set_file_xattrs",
        "get_file_id",  # redundant: every path-taking tool resolves paths itself
        "get_harvester_index_schema",  # no harvester in the test federation to validate it
        "grep_file_content",  # untested; matches a literal, case-sensitive substring
        "list_marketplace_spaces",  # finds spaces no other tool can act on
        "list_user_harvesters",  # no harvester in the test federation to validate it
        "query_harvester_index",  # no harvester in the test federation to validate it
    }
)

# Every tool registered by default (flag off). A unit test asserts this equals the
# registered surface, so adding a tool without classifying it fails CI; another
# asserts ALL_KNOWN | FLAG_GATED equals the surface with the flag on.
ALL_KNOWN: frozenset[str] = STABLE | EXPERIMENTAL

VALID_TIERS: frozenset[str] = frozenset({"stable", "experimental"})

MATURITY_ENV = "ONEDATA_MCP_MATURITY"
TOOLS_ENV = "ONEDATA_MCP_TOOLS"

# The tools the client was offered, set by a host that offers fewer than the server registers:
# the validation panel's in-process host filters the listing per scenario
# (`benchmark/k2val/recording.py`). Unset, the server's own tool list is what the client is
# offered (main.py removes the rest at launch). A tool's text names another tool only when it
# is offered (design/10, gate 2): see `is_offered`.
OFFERED_TOOLS: ContextVar[frozenset[str] | None] = ContextVar(
    "onedata_mcp_offered_tools", default=None
)


async def is_offered(server: object, name: str) -> bool:
    """Whether `name` is offered to the client calling now: in OFFERED_TOOLS when a host set
    it, else in the server's own tool list."""
    offered = OFFERED_TOOLS.get()
    if offered is not None:
        return name in offered
    return name in {tool.name for tool in await server.list_tools()}  # type: ignore[attr-defined]


def maturity_of(tool: str) -> str:
    """Maturity tier of a tool. Unknown tools default to ``experimental`` (safe)."""

    return "stable" if tool in STABLE else "experimental"


def origin_of(tool: str) -> str:
    """Provenance of a tool. Unknown tools default to ``ours``."""

    return "upstream" if tool in UPSTREAM or tool in FLAG_GATED else "ours"


def registered_surface(experimental: bool) -> frozenset[str]:
    """The tools the server registers: ALL_KNOWN, plus FLAG_GATED when the flag is on."""

    return ALL_KNOWN | FLAG_GATED if experimental else ALL_KNOWN


def _parse_csv_env(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def selected_tools(registered: frozenset[str] = ALL_KNOWN) -> set[str] | None:
    """Names to KEEP given the launch env, or ``None`` to keep all.

    Precedence: ``ONEDATA_MCP_TOOLS`` (explicit allowlist) wins over
    ``ONEDATA_MCP_MATURITY`` (tier filter); with neither set, all tools stay.
    Explicit names not present in ``registered`` are warned about and ignored.
    Unknown maturity tiers are warned about and ignored (an all-unknown tier
    string keeps everything, failing safe/open rather than hiding the surface).
    """

    explicit = _parse_csv_env(TOOLS_ENV)
    if explicit:
        unknown = explicit - registered
        if unknown:
            logger.warning(
                "%s names not registered (ignored): %s",
                TOOLS_ENV,
                ", ".join(sorted(unknown)),
            )
        return explicit & registered

    tiers = _parse_csv_env(MATURITY_ENV)
    if tiers:
        bad = tiers - VALID_TIERS
        if bad:
            logger.warning(
                "%s has unknown tier(s) (ignored): %s (valid: %s)",
                MATURITY_ENV,
                ", ".join(sorted(bad)),
                ", ".join(sorted(VALID_TIERS)),
            )
        good = tiers & VALID_TIERS
        if not good:
            return None  # fail open — don't silently hide everything
        return {t for t in registered if maturity_of(t) in good}

    return None


def tools_to_remove(registered: frozenset[str] = ALL_KNOWN) -> set[str]:
    """Names to prune from the registered surface for this launch (may be empty).

    Sync + name-based so it mirrors the ``token_policy`` prune in ``main.py``
    (no async tool enumeration needed at server-construction time).
    """

    keep = selected_tools(registered)
    if keep is None:
        return set()
    removed = set(registered) - keep
    if removed:
        logger.info(
            "Tool-selection: exposing %d/%d tools (pruned: %s)",
            len(keep),
            len(registered),
            ", ".join(sorted(removed)),
        )
    return removed
