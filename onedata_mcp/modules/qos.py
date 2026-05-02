"""MCP tool registration for QoS operations."""

import asyncio
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

        ## Per-rule detail enrichment

        The Oneprovider REST returns `requirements` as a flat
        `{qosId: status}` mapping. To answer common questions like
        "which files require only 1 replica?" the agent would need
        to follow up with a `get_qos_requirement` call per rule. This
        wrapper fetches each rule's detail in parallel and embeds it
        inline, so the response shape becomes:

            {
              "status": "fulfilled",
              "requirements": {
                "<qosId>": {
                  "status": "fulfilled",
                  "expression": "providerId=...",
                  "replicas_num": 2,
                  ...
                },
                ...
              }
            }

        See research/empirical-mcp-server-findings.md M-7. The original
        flat shape is preserved as `requirements_flat` for callers that
        depend on it.
        """
        summary = await get_file_qos_summary(file_id_or_path)
        flat = summary.get("requirements")
        if not isinstance(flat, dict) or not flat:
            return summary

        # Fetch per-rule detail in parallel; tolerate per-rule failure
        # so a single missing rule doesn't break the whole response.
        async def _fetch(qid: str) -> tuple[str, dict[str, Any] | None]:
            try:
                return qid, await get_qos_requirement(qid)
            except Exception:  # noqa: BLE001 — best-effort enrichment
                return qid, None

        details = await asyncio.gather(*[_fetch(qid) for qid in flat])

        enriched: dict[str, Any] = {}
        for qid, status in flat.items():
            base: dict[str, Any] = {"status": status}
            for d_qid, detail in details:
                if d_qid == qid and isinstance(detail, dict):
                    # Merge detail keys we care about for benchmark
                    # use cases (expression, replicas_num, fulfilled).
                    for k in ("expression", "replicas_num", "fulfilled", "qosRequirementId"):
                        if k in detail:
                            base[k] = detail[k]
            enriched[qid] = base
        out = dict(summary)
        out["requirements"] = enriched
        out["requirements_flat"] = flat
        return out

    @mcp.tool(name="add_file_qos_requirement", annotations=ToolAnnotations(destructiveHint=True))
    async def mcp_add_file_qos_requirement(
        file_id_or_path: str = Field(
            description="File id or path of the file or directory to attach the requirement to"
        ),
        expression: str = Field(
            description=(
                "Onedata QoS expression. Operands depend on what attributes "
                "the federation operator has assigned to storages. Two "
                "ALWAYS-SAFE implicit operands: 'providerId=<id>' and "
                "'storageId=<id>'. Admin-set string attributes (e.g. "
                "country=PL, type=ssd, geo=EU) ONLY work if a federation "
                "admin has tagged the storages — discover via list_user_spaces "
                "to see which providers are actually present, and check the "
                "specific federation's docs for any admin-attributed tags. "
                "Operators: '&' (AND), '|' (OR), '\\\\' (exclusion). "
                "USE SINGLE EQUALS '=' (NOT '=='). "
                "Avoid 'cloud=...', 'region=...', 'zone=...' unless verified "
                "— these are common-but-fictional operand names. "
                "See research/empirical-mcp-server-findings.md M-8."
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
