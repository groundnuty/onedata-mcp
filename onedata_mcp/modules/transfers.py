"""MCP tool registration for transfer operations."""

from typing import Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from onedata_mcp.api.transfers import get_transfer, list_space_transfers


def register_module(mcp: FastMCP) -> None:
    """Register Onedata transfer tools with the MCP server."""

    @mcp.tool(name="list_space_transfers", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_list_space_transfers(
        space_id: str = Field(description="Space id to list transfers for"),
        state: Literal["waiting", "ongoing", "ended"] = Field(
            default="ongoing",
            description="Transfer state filter. Defaults to 'ongoing'.",
        ),
        limit: int = Field(
            default=100,
            ge=1,
            le=1000,
            description="Maximum number of transfer ids to return (1..1000)",
        ),
        page_token: str | None = Field(
            default=None,
            description="Pagination token from a previous response's nextPageToken",
        ),
    ) -> dict[str, Any]:
        """List transfer ids in a space.

        The response contains transfer ids only; for source / destination /
        bytes / state detail, follow up with get_transfer per id.

        Note: requires the `space_view_transfers` privilege on the calling token.
        """
        return await list_space_transfers(space_id, state=state, limit=limit, page_token=page_token)

    @mcp.tool(name="get_transfer", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_transfer(
        transfer_id: str = Field(description="Transfer id"),
    ) -> dict[str, Any]:
        """Get detail for a single transfer by id.

        Includes type (replication / eviction / migration), source and
        destination provider ids, file path, byte counts, and timing.
        """
        return await get_transfer(transfer_id)
