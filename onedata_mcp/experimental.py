"""Opt-in gate for un-validated MCP-exposed features.

Candidates adapted from upstream (M0rgho/onedata-mcp) that change the
model-facing surface (new tools, enriched tool docstrings, enriched server
instructions) are registered ONLY when this flag is on. They have NOT been
through the multi-LLM hardening sweep that the rest of the headline surface
has (see onedata-mcp#1), so exposing them by default could regress weaker
models. Off by default: the surface stays byte-identical to the validated one.

Enable with ``ONEDATA_MCP_EXPERIMENTAL=1`` (any value outside the falsy set
below counts as on).
"""

from __future__ import annotations

import os

_FALSY = {"", "0", "false", "False", "no", "off"}


def experimental_enabled() -> bool:
    """True when ONEDATA_MCP_EXPERIMENTAL is set to a truthy value."""

    return os.getenv("ONEDATA_MCP_EXPERIMENTAL", "").strip() not in _FALSY
