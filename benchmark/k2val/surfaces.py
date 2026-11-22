"""The offered tool sets' surfaces, hashed as the promotion records hash them.

A record (design/11–15, "Gate 4") cites, per offered set, the sha256 of `make -s tools-manifest`
with `ONEDATA_MCP_TOOLS` set to the set: `scripts/tools_manifest.py`'s sorted JSON of each tool's
name, description, inputSchema and annotations, printed with a trailing newline. `surface_sha`
renders a listing the same way, so the hash of what a trial would offer can be compared with the
recorded one before the model is called (a test keeps the two renderings equal).

Changing a tool's surface changes these hashes: recompute them with the command above, update
the records and `RECORDED_SURFACES` in the same change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from benchmark.k2val.config import K1_TOOLS, K2_TOOLS
from benchmark.k2val.file_scenarios import FILE_TOOLS
from benchmark.k2val.stable_scenarios import ST7_TOOLS

# offered set -> (its name in the records, the recorded hash)
RECORDED_SURFACES: Mapping[frozenset[str], tuple[str, str]] = {
    frozenset(K1_TOOLS): (
        "K1",
        "9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3",
    ),
    frozenset(K2_TOOLS): (
        "K2",
        "6f502cb473137d4e972434c039830d7388b3e87f181dcc0386e0d0747fbc141b",
    ),
    frozenset(FILE_TOOLS): (
        "FILE_TOOLS",
        "c030429d0e04de218f50a1bda935d17e84026c8c24ea1a61fd6d91b0bac31922",
    ),
    # the stable tools' smoke scenario ST7 (design/16): no tool but the listing names a provider
    frozenset(ST7_TOOLS): (
        "ST7_TOOLS",
        "76995017fe996f35c81b2437727354ebed1951c6d39d767e57ee3e1372646b1c",
    ),
}


def _as_mcp_tool(tool: Any) -> Any:
    """A FastMCP server tool as a client receives it (the listing `tools_manifest` reads)."""
    to_mcp = getattr(tool, "to_mcp_tool", None)
    return to_mcp() if callable(to_mcp) else tool


def manifest_text(listing: Iterable[Any]) -> str:
    """`scripts/tools_manifest.py`'s output for these tools, byte for byte."""
    tools = sorted((_as_mcp_tool(t) for t in listing), key=lambda t: t.name)
    rows = [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.inputSchema,
            "annotations": (
                t.annotations.model_dump(mode="json", exclude_none=True)
                if t.annotations is not None
                else None
            ),
        }
        for t in tools
    ]
    return json.dumps(rows, indent=2, sort_keys=True) + "\n"


def surface_sha(listing: Iterable[Any]) -> str:
    return hashlib.sha256(manifest_text(listing).encode("utf-8")).hexdigest()


def surface_problem(
    listing: Iterable[Any],
    offered: frozenset[str],
    recorded: Mapping[frozenset[str], tuple[str, str]],
) -> tuple[str | None, str | None]:
    """(the listing's hash, why it is not the recorded surface of the offered set, or None).

    A set with no recorded hash is a problem too: a promotion run offers only recorded sets."""
    try:
        sha = surface_sha(listing)
    except Exception as e:  # noqa: BLE001 - a listing that cannot be rendered is not the record
        return None, f"the tools listing cannot be rendered: {type(e).__name__}: {e}"
    entry = recorded.get(offered)
    if entry is None:
        return sha, f"no recorded surface hash for the offered set {sorted(offered)}"
    name, expected = entry
    if sha != expected:
        return sha, f"the {name} set's surface hashes to {sha}, the record says {expected}"
    return sha, None
