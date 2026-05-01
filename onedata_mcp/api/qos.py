"""
QoS API client for Oneprovider.

Endpoints (Onedata 25.0, oneprovider-swagger):
- GET  /data/{file_id}/qos/summary           -> get_file_qos_summary
- POST /qos_requirements                     -> add_qos_requirement
- GET  /qos_requirements/{qos_id}            -> get_qos_requirement
- DELETE /qos_requirements/{qos_id}          -> remove_qos_requirement

Note: add_qos_requirement is the only QoS write op on the public REST surface.
The fileId is in the body, not the URL (per swagger paths/qos_requirements.yaml).
"""

from __future__ import annotations

from typing import Any

from onedata_mcp.api.files import _normalize_path_to_file_id
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.utils import request


async def get_file_qos_summary(file_id_or_path: str) -> dict[str, Any]:
    """Return the effective QoS summary for a file or directory.

    QoS summary merges QoS requirements defined directly with those inherited
    from ancestors. Status is one of: 'pending', 'fulfilled', 'impossible'.

    Response shape (from QosSummary swagger definition):
        {
          "requirements": {"<qos_id>": "<status>", ...},
          "status": "pending|fulfilled|impossible"
        }
    """
    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    response = await request(config, "GET", f"/data/{file_id}/qos/summary")
    return response["body"]


async def add_qos_requirement(
    file_id_or_path: str,
    expression: str,
    replicas_num: int = 1,
) -> dict[str, Any]:
    """Add a new QoS requirement on a file or directory.

    Async semantics: returns immediately with the new requirement ID; the
    requested replicas materialise eventually. Poll get_file_qos_summary or
    list_space_transfers to observe progress.

    Args:
        file_id_or_path: target file id or path (/space/path/...)
        expression: QoS expression. Examples: "country=PL",
            "country=FR & type=ssd", "geo=EU \\ providerId=p123".
            Operands: admin-assigned key=value tags (geo, type, ...) +
            implicit storageId / providerId + built-in 'anyStorage'.
            Operators: '&' (AND), '|' (OR), '\\' (exclusion).
            Reference: https://onedata.org/#/home/documentation/doc/using_onedata/qos.html
        replicas_num: target replica count. Must be >= 1.

    Response: {"qosRequirementId": "<id>"} + Location header.

    On invalid expression: server returns 400 with structured error.
    The error body's `errno`/`error_id`/`description` should be surfaced
    to the agent so it can self-correct (paper §5.4 H_qos_syntax metric).
    """
    if replicas_num < 1:
        raise ValueError("replicas_num must be >= 1")

    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    response = await request(
        config,
        "POST",
        "/qos_requirements",
        json_body={
            "expression": expression,
            "replicasNum": replicas_num,
            "fileId": file_id,
        },
    )
    return response["body"]


async def get_qos_requirement(qos_id: str) -> dict[str, Any]:
    """Return detailed information about a single QoS requirement.

    Response shape (QosRequirement swagger definition):
        {
          "qosRequirementId": "...",
          "fileId": "...",
          "qosExpression": "...",
          "replicasNum": N,
          "status": "fulfilled|pending|impossible"
        }
    """
    config = get_oneprovider_config()
    response = await request(config, "GET", f"/qos_requirements/{qos_id}")
    return response["body"]


async def remove_qos_requirement(qos_id: str) -> None:
    """Remove a QoS requirement. Returns None on success (HTTP 204)."""
    config = get_oneprovider_config()
    await request(config, "DELETE", f"/qos_requirements/{qos_id}")
