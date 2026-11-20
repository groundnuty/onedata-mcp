from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from onedata_mcp.api.harvesters import (
    get_harvester_index_schema,
    list_user_harvesters,
    query_harvester_index,
)


def register_module(mcp: FastMCP, *, experimental: bool = False) -> None:
    """Register onedata harvesters module tools with the MCP server.

    All three harvester tools are preview tools: they are registered only when
    ``experimental`` is set (ONEDATA_MCP_EXPERIMENTAL — see
    onedata_mcp/experimental.py and design/10-tool-maturity.md), because they
    cannot be validated without a harvester in the test federation. With the
    flag off this module registers nothing.

    The api layer accepts a ``query`` given as a dict OR a JSON-object string
    (``coerce_harvesters_index_query``; a dict passes through unchanged), and
    ``query_harvester_index`` declares the parameter as an object or a string and
    carries schema-first guidance in its description.
    """

    if not experimental:
        return

    @mcp.tool(name="list_user_harvesters", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_list_user_harvesters() -> list[dict[str, Any]]:
        """
        List harvesters available to the current user.

        Each harvester embeds detailed index metadata with schema omitted.
        """
        return await list_user_harvesters()

    @mcp.tool(name="get_harvester_index_schema", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_harvester_index_schema(
        harvester_id: str = Field(description="Harvester id"),
        index_id: str = Field(description="Harvester index id"),
    ) -> dict[str, Any]:
        """
        Get harvester index details, including schema.
        """
        return await get_harvester_index_schema(harvester_id, index_id)

    @mcp.tool(
        name="query_harvester_index",
        description=(
            "Execute a query against a specific harvester index.\n\n"
            "Call get_harvester_index_schema first and read mappings.properties "
            "for the field names and nested shapes; build term / bool filters from "
            "those declared paths."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def mcp_query_harvester_index_tolerant(
        harvester_id: str = Field(description="Harvester id"),
        index_id: str = Field(description="Harvester index id"),
        query: dict[str, Any] | str = Field(
            description=(
                "Plugin-specific query payload for the index "
                '(for example: {"method": "get", "path": "resource_id"}). '
                "May also be a JSON-object string."
            )
        ),
    ) -> dict[str, Any]:
        return await query_harvester_index(harvester_id, index_id, query)
