from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from onedata_mcp.api.spaces import (
    get_space_providers,
    list_marketplace_spaces,
    list_space_datasets,
    list_user_spaces,
    resolve_space_id_or_name,
)


def register_module(mcp: FastMCP, *, experimental: bool = False) -> None:
    """Register onedata spaces module tools and prompts with the MCP server.

    When ``experimental`` is set, additionally registers un-validated tools
    (currently ``list_space_datasets``) gated behind ONEDATA_MCP_EXPERIMENTAL —
    see onedata_mcp/experimental.py and onedata-mcp#1.
    """

    if experimental:

        @mcp.tool(
            name="list_space_datasets",
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        async def mcp_list_space_datasets(
            space_id_or_name: str = Field(
                description="Space id or space name (as returned by list_user_spaces)"
            ),
            state: str = Field(
                default="attached",
                description=(
                    "Dataset tree: 'attached' follows the current file layout; "
                    "'detached' is the hierarchy frozen at detachment time"
                ),
            ),
            limit: int = Field(default=100, ge=1, le=1000),
            offset: int = Field(default=0, ge=0),
        ) -> dict[str, Any]:
            """List top-level datasets established in a space."""
            return await list_space_datasets(
                space_id_or_name,
                state="detached" if state == "detached" else "attached",
                limit=limit,
                offset=offset,
            )

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
        space_id: str = Field(description="Space id OR human-readable name"),
    ) -> dict[str, Any]:
        """List providers supporting a space, queried from Oneprovider.

        `space_id` accepts either the hex spaceId or the space name; an
        internal lookup resolves names. See research/empirical-mcp-server-
        findings.md M-3.

        Returns the canonical (providerId, providerName) pairs as the
        oneprovider sees them. For richer per-provider attributes
        (geographic location, storage classes, online status), follow up
        with an onezone /providers/{providerId} call per id — the agent
        chains the two calls when needed.
        """
        resolved = await resolve_space_id_or_name(space_id)
        return await get_space_providers(resolved)
