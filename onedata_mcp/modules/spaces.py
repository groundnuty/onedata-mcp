from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from onedata_mcp.api.spaces import (
    get_space_providers,
    list_marketplace_spaces,
    list_user_spaces,
)


def register_module(mcp: FastMCP) -> None:
    """Register onedata spaces module tools and prompts with the MCP server."""

    @mcp.tool(name="list_user_spaces", description="List spaces available to the user")
    async def mcp_list_user_spaces() -> list[dict]:
        """
        Get all onedata spaces

        A space is a top-level shared data workspace that groups files,
        users, and storage providers in Onedata.
        """
        return await list_user_spaces()

    @mcp.tool(name="list_marketplace_spaces", description="List marketplace spaces with details")
    async def mcp_list_marketplace_spaces(
        tags: list[str] | None = Field(
            default=None,
            description="Optional tags filter; returns spaces that match at least one provided tag",
        ),
        limit: int = Field(default=20, ge=1, le=50),
        token: str | None = Field(
            default=None,
            description="Pagination token from previous response",
        ),
        offset: int = Field(
            default=0,
            description="Offset relative to token start point; can be negative",
        ),
    ) -> dict[str, Any]:
        """
        List spaces advertised in the Marketplace and return detailed entries.

        Supports tags filtering and pagination with limit/token/offset.
        """
        return await list_marketplace_spaces(
            tags=tags,
            limit=limit,
            token=token,
            offset=offset,
        )

    @mcp.tool(name="list_space_providers", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_list_space_providers(
        space_id: str = Field(description="Space id"),
    ) -> dict[str, Any]:
        """List providers supporting a space, queried from Oneprovider.

        Returns the canonical (providerId, providerName) pairs as the
        oneprovider sees them. For richer per-provider attributes
        (geographic location, storage classes, online status), follow up
        with an onezone /providers/{providerId} call per id — the agent
        chains the two calls when needed.
        """
        return await get_space_providers(space_id)
