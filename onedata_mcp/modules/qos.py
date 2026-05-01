"""MCP tool registration for QoS operations."""

from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from onedata_mcp.api.qos import (
    add_qos_requirement,
    get_file_qos_summary,
    get_qos_requirement,
    remove_qos_requirement,
)


def register_module(mcp: FastMCP) -> None:
    """Register Onedata QoS tools with the MCP server."""

    @mcp.tool(name="get_file_qos_summary", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_file_qos_summary(
        file_id_or_path: str = Field(
            description="File id or path to the file in format /<space_name>/<path_to_file>"
        ),
    ) -> dict[str, Any]:
        """Return the effective QoS summary for a file or directory.

        Includes inherited requirements (from ancestor directories) and
        per-requirement status. Status: 'pending', 'fulfilled', 'impossible'.
        """
        return await get_file_qos_summary(file_id_or_path)

    @mcp.tool(name="add_file_qos_requirement", annotations=ToolAnnotations(destructiveHint=True))
    async def mcp_add_file_qos_requirement(
        file_id_or_path: str = Field(
            description="File id or path of the file or directory to attach the requirement to"
        ),
        expression: str = Field(
            description=(
                "Onedata QoS expression. Examples: 'country=PL', 'type=ssd', "
                "'country=FR & type=ssd'. Operands: admin-assigned key=value tags "
                "plus implicit storageId / providerId / anyStorage. "
                "Operators: '&' (AND), '|' (OR), '\\\\' (exclusion). "
                "USE SINGLE EQUALS '=' (NOT '==') — common Python-trained-LLM error."
            )
        ),
        replicas_num: int = Field(
            default=1,
            ge=1,
            description="Target replica count (>= 1). The system will replicate "
            "the file until this many copies satisfy the expression.",
        ),
    ) -> dict[str, Any]:
        """Add a new QoS requirement; returns the requirement ID immediately.

        Replication is asynchronous: poll get_file_qos_summary or
        list_space_transfers to observe materialisation.
        """
        return await add_qos_requirement(file_id_or_path, expression, replicas_num=replicas_num)

    @mcp.tool(name="get_qos_requirement", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_qos_requirement(
        qos_id: str = Field(description="QoS requirement id"),
    ) -> dict[str, Any]:
        """Get detail for a single QoS requirement by its id."""
        return await get_qos_requirement(qos_id)

    @mcp.tool(name="remove_qos_requirement", annotations=ToolAnnotations(destructiveHint=True))
    async def mcp_remove_qos_requirement(
        qos_id: str = Field(description="QoS requirement id"),
    ) -> None:
        """Remove a QoS requirement by id. Does not roll back already-replicated data."""
        await remove_qos_requirement(qos_id)
