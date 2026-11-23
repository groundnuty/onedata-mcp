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

import re
from typing import Any
from urllib.parse import quote

from onedata_mcp.api.files import _file_id_parts, _normalize_path_to_file_id
from onedata_mcp.api.transfers import _providers_of_space, _providers_sentence
from onedata_mcp.config import OnedataConfig, get_oneprovider_config
from onedata_mcp.utils import (
    OnedataApiError,
    bound_message,
    http_failure_message,
    redact_endpoints,
    request,
)


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


# A `providerId=<value>` operand of a QoS expression: the key as a whole word (not the tail of
# another attribute's name), a single `=` (`==` is Onedata's to refuse), and a value that runs to
# the next operator, bracket, `=` or space.
_PROVIDER_OPERAND = re.compile(r"(?<![A-Za-z0-9_.-])providerId\s*=(?!=)\s*([^\s&|\\()=]+)")


async def _refuse_unusable_provider_operands(
    file_id: str, expression: str, config: OnedataConfig
) -> None:
    """Refuse, before any rule is created, a `providerId=<value>` operand whose value is not the
    id of a provider supporting the file's space (SF1, design/16).

    Onedata accepts `providerId=local`, a provider's NAME, and creates a rule that no storage
    matches: it stays "impossible" for good, and the model blames the site. A value that is the
    NAME of a provider of the space is answered with that provider's id; one that is neither a
    name nor an id of such a provider, with the providers listed. Ids pass unchanged, and no
    other operand is looked at.

    Best-effort: with no `providerId=` operand there is no request at all, and when the space
    cannot be told from the file id (the id does not decode) or its providers cannot be listed,
    nothing is refused and the rule is created as before.
    """
    operands = list(dict.fromkeys(_PROVIDER_OPERAND.findall(expression)))
    if not operands:
        return
    parts = _file_id_parts(file_id)
    if parts is None:
        return
    providers = await _providers_of_space(parts[1])
    if not providers:
        return
    ids = {pid for _, pid in providers}
    problems: list[str] = []
    for value in operands:
        if value in ids:
            continue
        named = next((pid for name, pid in providers if name is not None and name == value), None)
        if named is not None:
            problems.append(
                f"{_shown(value)} is a provider's name; providerId= takes its id: "
                f"providerId=<id>, here providerId={named}."
            )
        else:
            problems.append(
                f"{_shown(value)} is neither the id nor the name of a provider that supports "
                "the file's space."
            )
    if problems:
        message = (
            "No QoS requirement was added. "
            + " ".join(problems)
            + " "
            + _providers_sentence(providers)
        )
        raise ValueError(bound_message(redact_endpoints(message, config)))


async def add_qos_requirement(
    file_id_or_path: str,
    expression: str,
    replicas_num: int = 1,
    *,
    check_providers: bool = False,
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
        check_providers: refuse a `providerId=<value>` operand whose value is not the id of a
            provider supporting the file's space (`_refuse_unusable_provider_operands`). Off by
            default: the MCP tool turns it on, the readiness tools and the fixtures pass ids
            they got from Onedata and need no extra request.

    Response: {"qosRequirementId": "<id>"} + Location header.

    On invalid expression: server returns 400 with structured error.
    The error body's `errno`/`error_id`/`description` should be surfaced
    to the agent so it can self-correct (paper §5.4 H_qos_syntax metric).
    """
    if replicas_num < 1:
        raise ValueError("replicas_num must be >= 1")

    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    if check_providers:
        await _refuse_unusable_provider_operands(file_id, expression, config)
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

    Response shape (Onedata 25.0, as measured live; the swagger example names the expression
    `qosExpression`, which 25.0 does not send):
        {
          "qosRequirementId": "...",
          "fileId": "...",
          "expression": "...",
          "replicasNum": N,
          "status": "fulfilled|pending|impossible"
        }

    The id is stripped of surrounding whitespace, and refused (see `_clean_qos_id`) when it
    cannot name a requirement.
    """
    qos_id = _clean_qos_id(qos_id)
    config = get_oneprovider_config()
    # Percent-encoded, as in remove_qos_requirement: a `?` or `#` would otherwise cut the path.
    response = await request(config, "GET", f"/qos_requirements/{quote(qos_id, safe='')}")
    return response["body"]


# Where a caller finds a valid id, for the errors below. A model that passed a path, a file id
# or a stale id needs to know where the current ids are. The text names no tool: the tool that
# shows a file's QoS summary may not be offered alongside this one.
_WHERE_IDS_ARE = (
    "A file's current QoS requirement ids are the keys of `requirements` in its QoS summary."
)
_SHOWN_ID_MAX = 80


def _shown(qos_id: str) -> str:
    """The id as echoed in an error: quoted, and cut short if it is long."""
    if len(qos_id) <= _SHOWN_ID_MAX:
        return repr(qos_id)
    return repr(qos_id[:_SHOWN_ID_MAX]) + "..."


def _clean_qos_id(qos_id: str) -> str:
    """The id to use: stripped of surrounding whitespace. Refuse, before any request, one that
    cannot name a requirement.

    The id is otherwise opaque: its alphabet is Onedata's, so nothing stricter than the
    following is checked. Each refusal says what the value looks like and where the ids are:

    - empty;
    - a `/`: a path (a model passing the file it means);
    - `.` or `..`: dot segments, which the HTTP client resolves to another URL (`..` to the
      API root);
    - whitespace inside, or a control character;
    - `=`: a QoS expression, which is not an id;
    - a backslash: a QoS operator or a Windows path, not part of an id.
    """
    stripped = qos_id.strip()
    shown = _shown(qos_id)
    if not stripped:
        raise ValueError(f"qos_id is empty. {_WHERE_IDS_ARE}")
    if "/" in stripped:
        raise ValueError(f"qos_id {shown} is a path, not a QoS requirement id. {_WHERE_IDS_ARE}")
    if stripped in {".", ".."}:
        raise ValueError(f"qos_id {shown} is not a QoS requirement id. {_WHERE_IDS_ARE}")
    if any(c.isspace() for c in stripped):
        raise ValueError(
            f"qos_id {shown} has whitespace inside, which no QoS requirement id has. "
            f"{_WHERE_IDS_ARE}"
        )
    if not stripped.isprintable():
        raise ValueError(
            f"qos_id {shown} has a control character, which no QoS requirement id has. "
            f"{_WHERE_IDS_ARE}"
        )
    if "=" in stripped:
        raise ValueError(
            f"qos_id {shown} looks like a QoS expression, not a QoS requirement id. "
            f"{_WHERE_IDS_ARE}"
        )
    if "\\" in stripped:
        raise ValueError(
            f"qos_id {shown} has a backslash, which no QoS requirement id has. {_WHERE_IDS_ARE}"
        )
    return stripped


def _status_of(error: OnedataApiError) -> int | None:
    response = error.response if isinstance(error.response, dict) else {}
    status = response.get("status_code")
    return status if isinstance(status, int) else None


def _remove_error_hint(error: OnedataApiError, qos_id: str) -> str | None:
    """What the caller can do about a failed removal, or None to pass the error on unchanged.

    A specific diagnosis needs a structured Onedata error (a JSON body whose `error.id` or
    `error.details.errno` says what happened) on a 4xx status. What a 25.0 provider answers for
    each case is not pinned here (the swagger lists 400, 403, 404 and 500, each with the generic
    Error body); the live test prints the real answers. Anything less specific (a bare or HTML
    404, an unrecognised 400, a 5xx that mentions a missing file) gets a generic hint that keeps
    the uncertainty and does not say the requirement is gone.
    """
    status = _status_of(error)
    shown = _shown(qos_id)
    not_found = error.error_id == "notFound" or error.errno == "enoent"
    forbidden = error.error_id == "forbidden" or error.errno in {"eacces", "eperm"}
    client_error = status in {400, 404}
    if status is not None and 300 <= status < 400:
        # `request` treats anything outside 2xx as an error, a redirect included; it follows
        # none, so the removal did not happen as far as anything here can tell.
        return (
            f"Onedata answered {status}, not 204: this tool does not follow a redirect, so the "
            f"removal is not confirmed and QoS requirement {shown} may still exist. Check the "
            f"provider. {_WHERE_IDS_ARE}"
        )
    if status == 403 or (client_error and forbidden):
        return (
            f"Not allowed to remove QoS requirement {shown}: the token may lack the privilege to "
            "manage QoS in the space this requirement belongs to (space_manage_qos), or be "
            "read-only. Calling again with this id will not help. A requirement in a space where "
            "the token has that privilege is not affected."
        )
    if client_error and not_found:
        return (
            f"No QoS requirement with id {shown} exists: it may have been removed already, "
            f"or the value is not a QoS requirement id (a file id is not one). {_WHERE_IDS_ARE}"
        )
    if status == 404:
        return (
            "Onedata answered 404 without saying the requirement is missing; check the id and "
            f"the provider. {_WHERE_IDS_ARE}"
        )
    if status == 400:
        return (
            "Onedata answered 400 without saying the requirement is missing; check that "
            f"{shown} is a QoS requirement id (a file id is not one) and the provider. "
            f"{_WHERE_IDS_ARE}"
        )
    if status is not None and status >= 500 and not_found:
        return (
            f"Onedata answered {status} and mentioned a missing item; that does not show that "
            "the requirement is gone, so the removal is not confirmed. Check the id and the "
            f"provider. {_WHERE_IDS_ARE}"
        )
    return None


async def remove_qos_requirement(qos_id: str) -> None:
    """Remove a QoS requirement. Returns None on success (HTTP 204).

    The id is stripped of surrounding whitespace and percent-encoded into the path: unencoded,
    a `?` or `#` in it would cut the path short and remove the requirement named by the part
    before it. Only a 204, the one documented success, is a success: a redirect (which is not
    followed) and a 202 (accepted, not confirmed) are errors that say the removal is not
    confirmed and the requirement may still exist. A failure the caller can act on (unknown id,
    wrong kind of id, no permission) is raised with what to do about it, followed by Onedata's
    own message. The whole message holds no configured host or URL and is bounded.
    """
    qos_id = _clean_qos_id(qos_id)
    config = get_oneprovider_config()
    path = f"/qos_requirements/{quote(qos_id, safe='')}"
    try:
        response = await request(config, "DELETE", path)
    except OnedataApiError as error:
        hint = _remove_error_hint(error, qos_id)
        if hint is None:
            raise
        message = bound_message(redact_endpoints(f"{hint} [{error}]", config))
        raise OnedataApiError(message, response=error.response) from error
    status = response["status_code"]
    if status != 204:
        # 204 is the one documented success. Any other 2xx, a 202 (accepted) above all, does not
        # say that the requirement is gone: the removal is not confirmed.
        accepted = (
            "the removal was accepted but is not confirmed"
            if status == 202
            else "the removal is not confirmed"
        )
        failure = http_failure_message(
            config, "DELETE", path, status, None, label="Onedata API answer"
        )
        message = (
            f"Onedata answered {status}{' (accepted)' if status == 202 else ''}, not 204: "
            f"{accepted}, so QoS requirement {_shown(qos_id)} may still exist. Check the "
            f"provider. {_WHERE_IDS_ARE} [{failure}]"
        )
        raise OnedataApiError(
            bound_message(redact_endpoints(message, config)),
            response={"status_code": status, "body": None},
        )
