"""Opt-in gate for un-validated MCP-exposed features.

Two kinds of feature are registered ONLY when this flag is on: candidates adapted
from later commits of the upstream base that change the model-facing surface (new
tools, enriched tool docstrings, enriched server instructions), and tools moved
off the default list because they are redundant or cannot be validated (see
design/10-tool-maturity.md, "Leaving the default surface"). None has been through
the multi-LLM hardening sweep that the headline surface has (see onedata-mcp#1),
so exposing them by default could regress weaker models. Off by default. The tool
sets are classified in ``onedata_mcp/tool_maturity.py`` (``FLAG_GATED``).

Enable with ``ONEDATA_MCP_EXPERIMENTAL=1`` (any value outside the falsy set
below counts as on).
"""

from __future__ import annotations

import os

_FALSY = {"", "0", "false", "False", "no", "off"}


def experimental_enabled() -> bool:
    """True when ONEDATA_MCP_EXPERIMENTAL is set to a truthy value."""

    return os.getenv("ONEDATA_MCP_EXPERIMENTAL", "").strip() not in _FALSY
