"""Per-tool maturity + provenance classification, and launch-time selection.

**Model-invisible.** None of this appears in the tool descriptions the LLM
reads. It documents which tools are panel-validated and drives which tools the
server exposes at launch (``ONEDATA_MCP_MATURITY`` / ``ONEDATA_MCP_TOOLS``).

Two axes:

``maturity``
  ``stable``       — validated against the PPAM 7-LLM K=8 panel (== the
                     benchmark HEADLINE-16). Proven against weaker models.
  ``experimental`` — registered but NOT panel-validated: either added to this
                     project after the paper freeze, or inherited from the
                     upstream base surface and never swept against our panel.

``origin`` (a distrust signal for anything unvalidated)
  ``ours``     — authored here after the upstream base commit (85b8515).
  ``upstream`` — inherited from the upstream base surface, or adapted from its
                 later commits (the flag-gated tools, see ``FLAG_GATED``).

Note **panel-validation trumps origin**: an upstream-origin tool that made it
into the HEADLINE-16 and survived K=8 was hardened by us (M-1..M-13 findings)
and is ``stable``. The genuine distrust set is ``experimental`` ∩ ``upstream``.

The ``stable`` set is kept identical to ``benchmark.tool_allowlist.HEADLINE``;
a unit test asserts the two stay in sync without a runtime import (the server
package must not depend on the benchmark package).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# --- classification (the 29 tools registered by default; 27 verified against fork 85b8515) ---

# maturity == "stable": panel-validated (the HEADLINE-16).
STABLE: frozenset[str] = frozenset(
    {
        "add_file_qos_requirement",
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
        "set_file_metadata",
    }
)

# maturity == "experimental": registered but not panel-validated.
EXPERIMENTAL: frozenset[str] = frozenset(
    {
        # ours, added after the paper freeze — not yet swept
        "create_directory",
        "remove_qos_requirement",
        "schedule_file_replication",
        # ours, the readiness tools (design/07-readiness-tools.md)
        "check_readiness",
        "ensure_readiness",
        # inherited from the upstream base surface, never panel-swept (the
        # genuine distrust set)
        "get_file_attributes",
        "get_file_id",
        "get_harvester_index_schema",
        "grep_file_content",
        "list_children",
        "list_marketplace_spaces",
        "list_user_harvesters",
        "query_harvester_index",
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

# Registered ONLY when ONEDATA_MCP_EXPERIMENTAL is on (onedata_mcp/experimental.py);
# with the flag off they do not exist on the server, so they are kept out of
# ALL_KNOWN, the surface registered by default. Both are ``experimental`` (never
# panel-validated, and ``maturity_of`` already answers that for anything outside
# STABLE) and ``upstream`` in origin: adapted from later upstream commits
# (``list_space_datasets`` from 63c21b5, ``set_file_xattrs`` from 46f030a) rather
# than present at the base commit, so they are not in UPSTREAM. They are therefore
# outside the EXPERIMENTAL ∩ UPSTREAM set of the registered-by-default distrust tools,
# but carry the same two labels through ``maturity_of`` / ``origin_of``.
FLAG_GATED: frozenset[str] = frozenset(
    {
        "list_space_datasets",
        "set_file_xattrs",
    }
)

# Every tool registered by default (flag off). A unit test asserts this equals the
# registered surface, so adding a tool without classifying it fails CI; another
# asserts ALL_KNOWN | FLAG_GATED equals the surface with the flag on.
ALL_KNOWN: frozenset[str] = STABLE | EXPERIMENTAL

VALID_TIERS: frozenset[str] = frozenset({"stable", "experimental"})

MATURITY_ENV = "ONEDATA_MCP_MATURITY"
TOOLS_ENV = "ONEDATA_MCP_TOOLS"


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
