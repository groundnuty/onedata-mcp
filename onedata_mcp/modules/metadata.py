"""MCP tool registration for metadata-query operations.

Composes existing primitives — see design/02-query-by-metadata-no-harvester.md.
"""

from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from onedata_mcp.api.metadata import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_RESULTS,
    query_by_metadata,
)


def register_module(mcp: FastMCP) -> None:
    """Register Onedata metadata-query tools with the MCP server."""

    @mcp.tool(name="query_by_metadata", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_query_by_metadata(
        space: str = Field(description="Onedata space name to search within"),
        predicate: str = Field(
            description=(
                "Match predicate over JSON metadata at top level. "
                "Format: 'key=value' or 'key=*' (key present, any value). "
                "Multiple clauses joined by '&' are AND-ed: "
                "'pipeline_stage=raw & reviewed=*'. "
                "USE SINGLE '=' (NOT '==')."
            )
        ),
        path: str = Field(
            default="/",
            description="Subtree under the space to search. Default '/' (whole space).",
        ),
        max_depth: int = Field(
            default=DEFAULT_MAX_DEPTH,
            ge=1,
            le=20,
            description="Path-depth cap below `path`. Default 5.",
        ),
        max_results: int = Field(
            default=DEFAULT_MAX_RESULTS,
            ge=1,
            le=500,
            description="Cap on returned matches. Default 50.",
        ),
    ) -> dict[str, Any]:
        """Find files whose JSON metadata matches the predicate.

        Recursive scan from (space, path). Bounded by max_depth, max_results,
        and an internal MAX_FILES_VISITED cap. The result envelope includes
        `truncated: true` when any cap fires, so partial-result situations
        are explicit.

        Returns: {"matches": [{path, fileId, matched_keys}], "truncated": bool,
                  "files_visited": N}.
        """
        return await query_by_metadata(
            space,
            predicate,
            path=path,
            max_depth=max_depth,
            max_results=max_results,
        )
