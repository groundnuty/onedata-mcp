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
from urllib.parse import quote

from onedata_mcp.api.files import _normalize_path_to_file_id
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.utils import OnedataApiError, request


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


# Where a caller finds a valid id, for the errors below. A model that passed a path, a file id
# or a stale id needs to know where the current ids are.
_WHERE_IDS_ARE = (
    "A file's current QoS requirement ids are the keys of `requirements` in "
    "get_file_qos_summary's answer for that file."
)
_SHOWN_ID_MAX = 80


def _shown(qos_id: str) -> str:
    """The id as echoed in an error: quoted, and cut short if it is long."""
    if len(qos_id) <= _SHOWN_ID_MAX:
        return repr(qos_id)
    return repr(qos_id[:_SHOWN_ID_MAX]) + "..."


def _check_qos_id(qos_id: str) -> None:
    """Refuse, before any request, an id that cannot name a requirement.

    The id is otherwise opaque: its alphabet is Onedata's, so nothing stricter is checked
    here. A `/` makes it a path (a model passing the file it means); `.` and `..` are dot
    segments, which the HTTP client resolves to another URL (`..` to the API root).
    """
    stripped = qos_id.strip()
    if not stripped:
        raise ValueError(f"qos_id is empty. {_WHERE_IDS_ARE}")
    if "/" in qos_id:
        raise ValueError(
            f"qos_id {_shown(qos_id)} is a path, not a QoS requirement id. {_WHERE_IDS_ARE}"
        )
    if stripped in {".", ".."}:
        raise ValueError(f"qos_id {_shown(qos_id)} is not a QoS requirement id. {_WHERE_IDS_ARE}")


def _remove_error_hint(error: OnedataApiError, qos_id: str) -> str | None:
    """What the caller can do about a failed removal, or None to pass the error on unchanged.

    Keyed on the HTTP status the swagger lists for this call (400, 403, 404), with the error
    id and errno as a fallback; the exact error bodies of 25.0 are not pinned here.
    """
    response = error.response if isinstance(error.response, dict) else {}
    status = response.get("status_code")
    shown = _shown(qos_id)
    if status == 403 or error.error_id == "forbidden" or error.errno in {"eacces", "eperm"}:
        return (
            f"Not allowed to remove QoS requirement {shown}: the token may not manage QoS in "
            "this space (the space privilege space_manage_qos), or is read-only. Calling "
            "again, with this id or another, will not help."
        )
    if status == 404 or error.error_id == "notFound" or error.errno == "enoent":
        return (
            f"No QoS requirement with id {shown} exists: it may have been removed already, "
            f"or the value is not a QoS requirement id (a file id is not one). {_WHERE_IDS_ARE}"
        )
    if status == 400:
        return f"Onedata rejected {shown} as a QoS requirement id. {_WHERE_IDS_ARE}"
    return None


async def remove_qos_requirement(qos_id: str) -> None:
    """Remove a QoS requirement. Returns None on success (HTTP 204).

    The id is percent-encoded into the path: unencoded, a `?` or `#` in it would cut the path
    short and remove the requirement named by the part before it. A failure the caller can
    act on (unknown id, wrong kind of id, no permission) is raised with what to do about it,
    followed by Onedata's own message.
    """
    _check_qos_id(qos_id)
    config = get_oneprovider_config()
    try:
        await request(config, "DELETE", f"/qos_requirements/{quote(qos_id, safe='')}")
    except OnedataApiError as error:
        hint = _remove_error_hint(error, qos_id)
        if hint is None:
            raise
        raise OnedataApiError(f"{hint} [{error}]", response=error.response) from error
