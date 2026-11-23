"""MCP tool registration for transfer operations."""

from typing import Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from onedata_mcp.api.spaces import resolve_space_id_or_name
from onedata_mcp.api.transfers import (
    get_transfer,
    list_space_transfers,
    schedule_file_replication,
)


def register_module(mcp: FastMCP) -> None:
    """Register Onedata transfer tools with the MCP server."""

    @mcp.tool(name="list_space_transfers", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_list_space_transfers(
        space_id: str = Field(description="Space id OR human-readable name"),
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

        `space_id` accepts either the hex spaceId or the space name; an
        internal lookup resolves names.

        The response contains transfer ids only; for source / destination /
        bytes / state detail, follow up with get_transfer per id.

        Note: requires the `space_view_transfers` privilege on the calling token.
        """
        resolved = await resolve_space_id_or_name(space_id)
        return await list_space_transfers(resolved, state=state, limit=limit, page_token=page_token)

    @mcp.tool(name="get_transfer", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_transfer(
        transfer_id: str = Field(description="Transfer id"),
    ) -> dict[str, Any]:
        """Get detail for a single transfer by id.

        Includes type (replication / eviction / migration), the
        destination provider id (`replicatingProviderId`), file path, byte
        counts, and timing. The providers the data came from appear only
        as the keys of the per-provider byte histograms (`minHist`,
        `hrHist`, `dyHist`, `mthHist`).
        """
        return await get_transfer(transfer_id)

    @mcp.tool(
        name="schedule_file_replication",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def mcp_schedule_file_replication(
        file_id_or_path: str = Field(
            description="File id or path in format /<space_name>/<path_to_file>"
        ),
        target_provider_id: str = Field(
            description="Provider id to replicate the file TO (data is copied to this provider)"
        ),
    ) -> dict[str, Any]:
        """Schedule a replication transfer of a file to a target provider.

        Copies the file's data to `target_provider_id`. Returns
        `{"transferId": "<id>"}` as soon as the transfer is scheduled.
        Scheduled does not mean copied: no data has moved yet, and the data
        arrive later. A transfer's status can read completed before the
        copied blocks show at the target, so re-check the target provider's
        blocks of the file until they cover all of it, and judge the copy by
        those blocks, not by the transfer's status.

        Replication only — does not evict or migrate. The copy is one-off:
        nothing keeps the data at the target afterwards.

        The target must be a provider that supports the file's space, given by
        its provider id, not its name; otherwise the call fails.
        """
        return await schedule_file_replication(
            file_id_or_path, target_provider_id, check_providers=True
        )
