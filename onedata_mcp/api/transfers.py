"""
Transfers API client for Oneprovider.

Endpoints (Onedata 25.0, oneprovider-swagger):
- GET /spaces/{sid}/transfers      -> get_all_transfers (returns IDs only)
- GET /transfers/{tid}             -> get_transfer (per-transfer detail)
- POST /transfers                  -> create_transfer (schedule_file_replication)

Note: list_space_transfers requires the `space_view_transfers` privilege on
the calling token. State filter values are: 'waiting', 'ongoing', 'ended'
(NOT 'completed' / 'failed' / 'all' — paper spec §3.8 was wrong).

The list endpoint returns IDs only; agents needing source/dest/state/bytes
must follow up with get_transfer per id. We expose both as separate tools
to keep the cost of the list call bounded.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from onedata_mcp.api.files import (
    _file_id_parts,
    _id_space_note,
    _incomplete_file_id_message,
    _is_unconfirmed_not_found,
    _unconfirmed_hint,
)
from onedata_mcp.api.spaces import resolve_space_id_or_name
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.utils import OnedataApiError, bound_message, id_segment, redact_endpoints, request

TransferState = Literal["waiting", "ongoing", "ended"]


async def list_space_transfers(
    space_id: str,
    *,
    state: TransferState = "ongoing",
    limit: int = 100,
    page_token: str | None = None,
) -> dict[str, Any]:
    """List transfer IDs in a space.

    Args:
        space_id: Onedata space ID.
        state: One of 'waiting', 'ongoing', 'ended'. Default 'ongoing'.
        limit: 1..1000. Default 100.
        page_token: Continuation token from a previous response's
            `nextPageToken`.

    Response shape:
        {
          "transfers": ["<tid>", ...],
          "nextPageToken": "<token>" | null
        }
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")

    config = get_oneprovider_config()
    space_segment = id_segment(space_id, "space id")
    params: dict[str, Any] = {"state": state, "limit": limit}
    if page_token:
        params["page_token"] = page_token
    response = await request(
        config,
        "GET",
        f"/spaces/{space_segment}/transfers",
        params=params,
    )
    return response["body"]


async def get_transfer(transfer_id: str) -> dict[str, Any]:
    """Get detail for a single transfer by ID.

    Response shape (per Transfer swagger definition):
        {
          "type": "replication|eviction|migration",
          "userId": "...",
          "rerunId": "..." | null,
          "spaceId": "...",
          "fileId": "...",
          "filePath": "...",
          "callback": "..." | null,
          "queryParams": "..." | null,
          "transferState": "scheduled|enqueued|active|completed|aborting|cancelled|skipped|failed",
          "scheduleTime": <epoch>,
          "startTime": <epoch>,
          "finishTime": <epoch>,
          "replicationStatus": "...",
          "evictionStatus": "...",
          "replicatingProviderId": "...",
          "evictingProviderId": "...",
          "filesToProcess": N,
          "filesProcessed": N,
          "filesReplicated": N,
          "fileReplicasEvicted": N,
          "filesFailed": N,
          "filesSkipped": N,
          "bytesReplicated": N
        }
    """
    config = get_oneprovider_config()
    response = await request(config, "GET", f"/transfers/{id_segment(transfer_id, 'transfer id')}")
    return response["body"]


TransferType = Literal["replication", "eviction", "migration"]


async def create_transfer(
    file_id: str,
    transfer_type: TransferType,
    *,
    replicating_provider_id: str | None = None,
    evicting_provider_id: str | None = None,
) -> dict[str, Any]:
    """Schedule a transfer directly. Returns immediately with the transferId.

    Endpoint: POST /api/v3/oneprovider/transfers (operationId: create_transfer)

    Used by the fixture runner's pre-stage phase to set up scenario P4
    (most-recent migration). The QoS-rule indirection turned out to be
    flaky — rules stayed 'pending' on the live federation per
    research/empirical-findings #16, so the migration was never reliably
    triggered. This endpoint schedules transfers directly.

    Args:
        file_id: The file or directory to transfer (resolved via lookup_file_id).
        transfer_type: 'replication', 'eviction', or 'migration'.
        replicating_provider_id: Required for 'replication' and 'migration'.
            The provider the data is being copied TO.
        evicting_provider_id: Required for 'eviction' and 'migration'.
            The provider whose replica is being removed.

    Returns: {"transferId": "<id>"}.
    """
    if transfer_type in ("replication", "migration") and not replicating_provider_id:
        raise ValueError(f"replicating_provider_id required for {transfer_type!r}")
    if transfer_type in ("eviction", "migration") and not evicting_provider_id:
        raise ValueError(f"evicting_provider_id required for {transfer_type!r}")

    body: dict[str, Any] = {
        "type": transfer_type,
        "dataSourceType": "file",
        "fileId": file_id,
    }
    if replicating_provider_id is not None:
        body["replicatingProviderId"] = replicating_provider_id
    if evicting_provider_id is not None:
        body["evictingProviderId"] = evicting_provider_id

    config = get_oneprovider_config()
    response = await request(config, "POST", "/transfers", json_body=body)
    return response["body"]


# ---------------------------------------------------------------------------
# schedule_file_replication: the argument checks and the error hints a model can act on
# (design/12-promotion-schedule-file-replication.md, gate 1).
#
# The swagger (paths/transfers.yaml, POST) lists 201, 400, 403, 404 and 500, each failure with
# the generic Error body; the 25.0 error ids are not pinned there. A bare 400 does not say which
# argument was wrong, so a hint blames the provider only when Onedata's answer does (the error
# id `spaceNotSupportedBy`, or `details.key` naming the provider field), and blames the file only
# when the key names the file field. A 404 that names neither field, for a file given as an id,
# points at both arguments (the file may be the unknown one, or the target); for a path, which
# has already resolved to a file by then, it points at the target. A target that is the NAME of
# one of the space's providers (matched on the name as Onedata gives it) is answered with that
# provider's id, first. Every hint is for a 4xx status only: a 5xx with a body that mentions a
# missing item or a permission says the provider failed, not what the failure was about, and is
# passed on unchanged. Every hint is redacted and bounded as a whole. Onedata does not refuse a
# second replication of a file that is already being copied: no status in the swagger says so,
# and nothing here pretends otherwise.
#
# Before the request, a target is checked against the providers of the file's space
# (`_refuse_unusable_target`, M-46 and design/12): a provider's NAME is refused with its id, and a
# value that is neither a name nor an id of one of them with the providers listed, both without
# a word of Onedata's. That is so for the MCP tool only (`check_providers`): when the listing
# cannot be read the request goes as it would have without the check, and the mapping above
# stays the fallback. The hint that tells a name from an id stands alone, with none of Onedata's
# text after it: Onedata reads a name as an id and says the space is "not supported by provider
# <name>", which contradicts the hint, and a model believed it (`_StandsAlone`).
#
# A file given as an id is the one a model copied out of an earlier answer, and a bad copy (a
# dropped, changed, doubled or spliced character) is answered with a 403 or `spaceNotSupportedBy`
# that says nothing about a copy, or acts on another file. So an id Onedata's encoder could not
# have written is refused before any request (`_file_id_parts`); for an id that passes, both
# answers give the refusal first and then what to do if it is another file's id. A path keeps
# its hints.
# ---------------------------------------------------------------------------

_SHOWN_MAX = 80
# In the result of a schedule (F2, design/12): gate 4 had models answer "done" from the
# transfer's status, and some stop right after scheduling or hand the waiting back to the user
# (M-33, M-49). So it says what to do next and when the copy is complete, in the words of the
# description's own instruction. It does not say the copy has not happened: the target may
# already hold every block, or the copy may finish before the reply is read. It names no tool:
# the tool that reads the blocks may not be offered.
SCHEDULED_NOTE = (
    "Replication scheduled; the copy may not be complete yet. To know when it is, re-check the "
    "target provider's blocks of the file until they cover all of it; a transfer's 'completed' "
    "status alone does not show that."
)
_PATH_FORM = "a path starting with '/': /<space_name>/<path_to_file>"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PROVIDER_FIELDS = {"replicatingProviderId", "replicating_provider_id", "providerId"}
_FILE_FIELDS = {"fileId", "file_id"}


def _shown(value: str) -> str:
    """The value as echoed in an error: quoted, and cut short if it is long."""
    if len(value) <= _SHOWN_MAX:
        return repr(value)
    return repr(value[:_SHOWN_MAX]) + "..."


def _check_replication_args(file_arg: str, target: str) -> None:
    """Refuse, before any request, a value that cannot name a file or a provider. Both values
    are already stripped of surrounding whitespace.

    A value with a `/` that does not start with one is a relative path (Onedata would read it
    as a malformed file id). A path must name a space and an entry in it, `/<space>/<name>` at
    least: `/log.dat` is missing its space. `.` and `..` name nothing. A file that is not a
    path must be a whole file id (`_file_id_parts`): a truncated, extended or non-hex value
    cannot be one. A provider id is opaque, so nothing stricter is checked here: whether it is
    one of the file's space's providers is `_refuse_unusable_target`'s to say, once the file's
    space is known.
    """
    if not file_arg:
        raise ValueError(f"file_id_or_path is empty: give a file id, or {_PATH_FORM}.")
    if "/" in file_arg and not file_arg.startswith("/"):
        raise ValueError(
            f"Paths must start with '/': use /<space_name>/<path_to_file> (got {_shown(file_arg)})."
        )
    if file_arg.startswith("/") and len([part for part in file_arg.split("/") if part]) < 2:
        raise ValueError(
            "A path must name the space first: use /<space_name>/<path_to_file> "
            f"(got {_shown(file_arg)})."
        )
    if file_arg in {".", ".."}:
        raise ValueError(
            f"file_id_or_path {_shown(file_arg)} is not a file id or a path: give a file id, or "
            f"{_PATH_FORM}."
        )
    if not file_arg.startswith("/") and _file_id_parts(file_arg) is None:
        raise ValueError(_incomplete_file_id_message(file_arg))
    if not target:
        raise ValueError(
            "target_provider_id is empty: give the id of a provider that supports the file's space."
        )
    if "/" in target or target in {".", ".."}:
        raise ValueError(
            f"target_provider_id {_shown(target)} is not a provider id: give the id of a provider "
            "that supports the file's space."
        )


def _space_id_of_file_id(file_id: str) -> str | None:
    """The space id inside an Onedata file id, or None.

    A 25.0 file id is the hex form of an object id whose payload is `guid#<uuid>#<space id>`
    (the swagger's own example decodes so). Used only to list the space's providers in an
    error; any other shape gives None.
    """
    try:
        raw = bytes.fromhex(file_id)
    except ValueError:
        return None
    start = raw.find(b"guid#")
    if start < 0:
        return None
    parts = raw[start:].split(b"#")
    if len(parts) != 3:
        return None
    try:
        space_id = parts[2].decode("ascii")
    except UnicodeDecodeError:
        return None
    return space_id if _ID_RE.fullmatch(space_id) else None


async def _space_id_for(file_arg: str, file_id: str, error: OnedataApiError) -> str | None:
    """The file's space id, for the hint: from Onedata's error details, else from the file id,
    else (for a path) by resolving its leading space segment. None if none of them gives one."""
    error_obj = error.body.get("error")
    details = error_obj.get("details") if isinstance(error_obj, dict) else None
    space_id = details.get("spaceId") if isinstance(details, dict) else None
    if isinstance(space_id, str) and _ID_RE.fullmatch(space_id):
        return space_id
    if space_id := _space_id_of_file_id(file_id):
        return space_id
    if file_arg.startswith("/"):
        try:
            space_id = await resolve_space_id_or_name(file_arg.split("/")[1])
        except Exception:  # noqa: BLE001 - the hint is best-effort; the original error is raised
            return None
        return space_id if isinstance(space_id, str) and _ID_RE.fullmatch(space_id) else None
    return None


async def _space_providers(
    file_arg: str, file_id: str, error: OnedataApiError
) -> list[tuple[str | None, str]] | None:
    """(name, id) of the providers supporting the file's space, or None if not found.

    Only on the error path: the space comes from `_space_id_for`, and one `GET /spaces/{sid}`
    (the id percent-encoded) lists its providers. Any failure here gives None, so the hint
    falls back to plain words. The name is kept as Onedata gives it (None when there is none),
    so that a target can be matched against it; it is redacted only where it is shown, with the
    rest of the message.
    """
    space_id = await _space_id_for(file_arg, file_id, error)
    if not space_id:
        return None
    return await _providers_of_space(space_id)


async def _providers_of_space(space_id: str) -> list[tuple[str | None, str]] | None:
    """(name, id) of the providers supporting a space: one `GET /spaces/{sid}`, the id
    percent-encoded. None when the listing cannot be read or names no provider, so a caller that
    only checks or explains a value goes on as it would without the listing."""
    config = get_oneprovider_config()
    try:
        response = await request(config, "GET", f"/spaces/{id_segment(space_id, 'space id')}")
    except Exception:  # noqa: BLE001 - best-effort: the caller goes on without the listing
        return None
    body = response["body"]
    providers = body.get("providers") if isinstance(body, dict) else None
    out: list[tuple[str | None, str]] = []
    for entry in providers if isinstance(providers, list) else ():
        if not isinstance(entry, dict):
            continue
        pid, name = entry.get("providerId"), entry.get("providerName")
        if isinstance(pid, str) and pid:
            out.append((name if isinstance(name, str) and name else None, pid))
    return out or None


def _providers_sentence(providers: list[tuple[str | None, str]] | None) -> str:
    """Which ids are valid targets. The names are shown as given; the message they go into is
    redacted as a whole (`_hinted`)."""
    if not providers:
        return "Give the id (not the name) of a provider that supports the file's space."
    listed = ", ".join(f"{name or '(no name)'} (id {pid!r})" for name, pid in providers)
    return f"The providers that support the file's space are: {listed}."


def _id_of_name(providers: list[tuple[str | None, str]] | None, target: str) -> str | None:
    """The id of the space's provider whose NAME the target is, if it is one. The raw name is
    compared: a name that redaction would change, or a provider with no name, matches only
    itself."""
    return next((pid for name, pid in providers or () if name is not None and name == target), None)


class _StandsAlone(str):
    """A hint that is the whole message: Onedata's own text is not put after it.

    For a hint that Onedata's message can contradict. A provider's name given as the id is
    answered "Pass its id ..., not its name: ... is the name of a provider that supports the
    space"; Onedata, which reads the name as an id, says the space is "not supported by provider
    <name>". A model took the second for the fact and copied to the other provider (M-46,
    design/12). No other hint here has that problem: each either agrees with what Onedata says
    or says it does not know which argument is meant, so those keep the message after the
    hint."""


def _hinted(hint: str, error: OnedataApiError) -> OnedataApiError:
    """The hint first, then Onedata's own message (not after a `_StandsAlone` hint); the whole
    redacted (a configured host, a URL) and bounded, so that no echoed argument, provider name
    or id carries either past it."""
    config = get_oneprovider_config()
    text = str(hint) if isinstance(hint, _StandsAlone) else f"{hint} [{error}]"
    message = bound_message(redact_endpoints(text, config))
    return OnedataApiError(message, response=error.response)


def _name_given_as_id_hint(
    pid: str,
    shown_target: str,
    shown_file: str,
    providers: list[tuple[str | None, str]] | None,
) -> str:
    """The answer to a target that is a provider's NAME: what to pass, then which providers
    there are. Used before the request and after Onedata's refusal, so that the two cannot
    drift apart."""
    return (
        f"Pass its id {pid!r}, not its name: {shown_target} is the name of a provider that "
        f"supports the space of {shown_file}. {_providers_sentence(providers)}"
    )


async def _refuse_unusable_target(file_arg: str, file_id: str, target: str) -> None:
    """Refuse, before the request, a target that is not the id of a provider supporting the
    file's space (M-46, design/12).

    Onedata answers a provider's NAME with `spaceNotSupportedBy`, whose message says the space
    is "not supported by provider <name>": a model read that as true and copied to the other
    provider. So a target that is the NAME of a provider of the space is answered with that
    provider's id, and one that is neither a name nor an id of such a provider with the
    providers listed (name and id). Nothing of Onedata's is in either message, and no transfer
    is requested. A target that is the id of such a provider passes unchanged.

    Best-effort, like the check on a QoS rule's `providerId=` operands: when the space cannot
    be told from the file id, or its providers cannot be listed, nothing is refused, and the
    request goes as it would have without the check (the error mapping below stays the
    fallback). The one added request is the space's `GET /spaces/{sid}`.
    """
    parts = _file_id_parts(file_id)
    if parts is None:
        return
    providers = await _providers_of_space(parts[1])
    if not providers or any(pid == target for _, pid in providers):
        return
    shown_file, shown_target = _shown(file_arg), _shown(target)
    if (pid := _id_of_name(providers, target)) is not None:
        message = _name_given_as_id_hint(pid, shown_target, shown_file, providers)
    else:
        message = (
            f"Cannot replicate to {shown_target}: no provider with that id supports the space of "
            f"{shown_file}, and none of them has that name. {_providers_sentence(providers)}"
        )
    message += " No replication was scheduled."
    raise ValueError(bound_message(redact_endpoints(message, get_oneprovider_config())))


def _error_parts(error: OnedataApiError) -> tuple[int | None, str | None, str | None, Any]:
    response = error.response if isinstance(error.response, dict) else {}
    error_obj = error.body.get("error")
    details = error_obj.get("details") if isinstance(error_obj, dict) else None
    key = details.get("key") if isinstance(details, dict) else None
    return response.get("status_code"), error.error_id, error.errno, key


def _is_client_error(status: Any) -> bool:
    return isinstance(status, int) and 400 <= status < 500


async def _lookup_path(path: str) -> str:
    """The file id of a path (`POST /lookup-file-id/<path>`), status-aware like the promoted file
    tools' own lookup: only a structured not-found on a 4xx is `Path "..." not found`. Anything
    else, a 5xx whatever its body says included, passes on as the provider's own error: never
    the shared stable resolver's "Space ... does not exist" (it reads `spaceNotSupportedBy`
    on any status as a missing space; that resolver keeps its mapping for the stable tools)."""
    try:
        response = await request(
            get_oneprovider_config(), "POST", f"/lookup-file-id/{quote(path, safe='')}"
        )
    except OnedataApiError as error:
        status, error_id, errno, _ = _error_parts(error)
        if _is_client_error(status) and (errno == "enoent" or error_id == "notFound"):
            raise FileNotFoundError(f'Path "{path}" not found') from error
        raise
    return response["body"]["fileId"]


def _lookup_error_hint(shown_file: str, error: OnedataApiError) -> str | None:
    """A failed path lookup: the permission hint; on a 4xx `spaceNotSupportedBy`, that this
    provider does not support the file's space (the space may still exist); on a 5xx that
    mentions a missing item, that the provider failed and the file may also not exist; else
    None (the error passes on unchanged)."""
    if hint := _permission_hint(shown_file, error):
        return hint
    status, error_id, _, _ = _error_parts(error)
    if _is_client_error(status) and error_id == "spaceNotSupportedBy":
        return (
            f"This provider does not support the space of {shown_file}, so it cannot resolve "
            "the path here; the space may still exist. Check the path's space name."
        )
    if _is_unconfirmed_not_found(error):
        return _unconfirmed_hint(shown_file, error)
    return None


# After a refusal of a file given as an id: the id passed the check on a whole id, so it is one
# Onedata wrote, but it may be another file's.
_NOT_THE_FILE = (
    "If it is not the id of the file you mean, copy that file's id exactly from the earlier "
    "answer, or give its path, /<space_name>/<path_to_file>."
)


def _unsupported_by(error: OnedataApiError) -> tuple[str | None, str | None]:
    """(`details.providerId`, `details.spaceId`) of a `spaceNotSupportedBy` error, each None
    when it is not a non-empty string. ctool's `od_error_space_not_supported_by` gives both."""
    error_obj = error.body.get("error")
    details = error_obj.get("details") if isinstance(error_obj, dict) else None
    if not isinstance(details, dict):
        return None, None
    pid, sid = details.get("providerId"), details.get("spaceId")
    return (
        pid if isinstance(pid, str) and pid else None,
        sid if isinstance(sid, str) and sid else None,
    )


def _unsupported_space_lead(
    error: OnedataApiError, shown_file: str, shown_target: str, target: str, from_path: bool
) -> str:
    """The first sentence for `spaceNotSupportedBy`: which provider lacks support for which
    space, from the error's own details.

    A 25.0 replication request checks that this provider (the one the request went to)
    supports the file's space, then that the target does (op-worker
    `transfer_middleware_plugin:validate_file_transfer_creation/3`), and names the provider
    that failed. So a `providerId` equal to the target is the target, and any other is this
    provider: a target that supports the space would not change the answer then. The target is
    said to be no provider's id, not a provider without support: a provider's name given as the
    id reaches here when the providers could not be listed (M-46). Without a provider in the
    details, a path (which this provider resolved) points at the target, and an id at either."""
    pid, sid = _unsupported_by(error)
    space = f"space {_shown(sid)}" if sid else "the file's space"
    if pid is not None and pid == target:
        return (
            f"Cannot replicate {shown_file} to {shown_target}: Onedata reports that no provider "
            f"with the id {shown_target}, the target, supports {space}."
        )
    if pid is not None:
        return (
            f"Cannot replicate {shown_file}: Onedata reports that provider {_shown(pid)}, this "
            f"provider (the one the request went to, not the target {shown_target}), does not "
            f"support {space}, so it cannot schedule the replication."
        )
    if from_path:
        return (
            f"Cannot replicate to {shown_target}: no provider with that id supports the space of "
            f"{shown_file}."
        )
    return (
        f"Cannot replicate {shown_file} to {shown_target}: Onedata reports that {space} is not "
        f"supported, without saying by which provider: this provider (the one the request went "
        f"to) or the target {shown_target}."
    )


def _permission_hint(
    shown_file: str, error: OnedataApiError, *, copied_id: bool = False
) -> str | None:
    """For a 403, or a 4xx whose body says forbidden. The same body on a 5xx is not one.
    `copied_id`: the file was given as an id. It passed the check on a whole id, so it is one
    Onedata wrote; the refusal comes first, then what to do if it is another file's (the note
    for the id's space follows, `_replication_error_hint`)."""
    status, error_id, errno, _ = _error_parts(error)
    if status == 403 or (
        _is_client_error(status) and (error_id == "forbidden" or errno in {"eacces", "eperm"})
    ):
        if copied_id:
            return (
                f"Not allowed to schedule a replication of {shown_file}: the token may not "
                "schedule replications in this space (the space privilege "
                "space_schedule_replication), may not read the file, or is read-only. "
                f"{_NOT_THE_FILE}"
            )
        return (
            f"Not allowed to schedule a replication of {shown_file}: the token may not schedule "
            "replications in this space (the space privilege space_schedule_replication), may "
            "not read the file, or is read-only. Calling again will not help."
        )
    return None


async def _replication_error_hint(
    error: OnedataApiError, file_arg: str, file_id: str, target: str
) -> str | None:
    """What the caller can do about a failed schedule, or None to pass the error on unchanged.

    Keyed on the status the swagger lists (400, 403, 404), and on the error id, errno and
    `details.key` where Onedata gives them. Only a 4xx status gets a hint: a 5xx, and anything
    else, passes unchanged. In order:

    - permission;
    - a target that is the name of one of the space's providers: "Pass its id ...", whatever
      Onedata said about it, unless Onedata names the file field, and alone: Onedata's own
      message, which says the space is not supported by that "provider", contradicts it. (Only
      reached when `_refuse_unusable_target` could not list the providers before the request.)
    - Onedata says the space is not supported: first which provider lacks support for which
      space, from the error's details (`_unsupported_space_lead`: the target, or this provider);
      for a file given as an id, then what to do if it is another file's id (a 403 for an id
      says that too, after the refusal);
    - `details.key` names the provider field: not a valid provider id for the space (no claim
      about support);
    - the file: blamed only when the key names the file field;
    - a not-found that names neither field, for a file given as an id: Onedata did not say
      which argument it means, so both are pointed at, with the providers; for a path, which
      has already resolved to a file, it points at the target;
    - a bare 400: check both arguments.
    """
    status, error_id, errno, key = _error_parts(error)
    shown_file, shown_target = _shown(file_arg), _shown(target)
    from_path = file_arg.startswith("/")
    if hint := _permission_hint(shown_file, error, copied_id=not from_path):
        return hint + (await _id_space_note(file_arg, list_spaces=False) if not from_path else "")
    if not _is_client_error(status):
        return None
    not_found = status == 404 or error_id == "notFound" or errno == "enoent"
    file_blamed = key in _FILE_FIELDS
    provider_blamed = error_id == "spaceNotSupportedBy" or key in _PROVIDER_FIELDS
    if not (provider_blamed or file_blamed or not_found or status == 400):
        return None
    # The providers, to recognise a target that is a provider's name, which is wrong whatever
    # else is; not fetched when Onedata names the file field.
    providers = None if key in _FILE_FIELDS else await _space_providers(file_arg, file_id, error)
    if (pid := _id_of_name(providers, target)) is not None:
        # Alone: Onedata's "space is not supported by provider <name>" would contradict it.
        return _StandsAlone(_name_given_as_id_hint(pid, shown_target, shown_file, providers))
    if error_id == "spaceNotSupportedBy":
        lead = _unsupported_space_lead(error, shown_file, shown_target, target, from_path)
        # Without the provider in the details, whether the id's space is one this provider
        # supports for the token tells the two readings apart; with it, it repeats the lead.
        unseen = ""
        if not from_path and _unsupported_by(error)[0] is None:
            unseen = (await _id_space_note(file_arg, list_spaces=False)).strip()
        return " ".join(
            part
            for part in (
                lead,
                "" if from_path else _NOT_THE_FILE,
                unseen,
                _providers_sentence(providers),
            )
            if part
        )
    if key in _PROVIDER_FIELDS:
        return (
            f"Cannot replicate to {shown_target}: it is not a valid provider id for the space of "
            f"{shown_file}. {_providers_sentence(providers)}"
        )
    if file_blamed:
        return (
            f"No file is known by {shown_file}: give the file's id, or {_PATH_FORM}. "
            "A file name alone is neither."
        )
    if not_found and not from_path:  # an id: Onedata did not say which argument it means
        return (
            f"Onedata found nothing for the replication of {shown_file} to {shown_target}, and "
            f"did not say which argument it means. Check both: the file is a file id, or "
            f"{_PATH_FORM} (a file name alone is neither); the target is a provider id. "
            f"{_providers_sentence(providers)}"
        )
    if not_found:  # a path that resolved: the file is known, so look at the target
        return (
            f"Onedata found nothing for the replication of {shown_file} to {shown_target}. The "
            f"path resolved to a file, so check the target: it must be a provider id. "
            f"{_providers_sentence(providers)}"
        )
    return (
        f"Onedata rejected the replication of {shown_file} to {shown_target}. Check both "
        f"arguments: the file is a file id, or {_PATH_FORM}; the target is a provider id. "
        f"{_providers_sentence(providers)}"
    )


async def schedule_file_replication(
    file_id_or_path: str,
    target_provider_id: str,
    *,
    check_providers: bool = False,
) -> dict[str, Any]:
    """Schedule a replication transfer of a file to a target provider.

    Replication-only wrapper over :func:`create_transfer`: resolves the file
    (logical path or opaque id) and POSTs a ``type=replication`` transfer that
    copies the data TO ``target_provider_id``. Returns as soon as the transfer
    is scheduled, before any data is copied.

    Neither argument goes into a URL path: the file id and the provider id travel
    in the JSON body, and a path is percent-encoded whole into the lookup
    (``/lookup-file-id/<path>``). A value that cannot name a file or a provider is
    refused before any request, and so is, with `check_providers`, a target that is
    not the id of a provider of the file's space; a failure the caller can act on
    (unknown file, a target that does not support the space or is a name, no
    permission, the path lookup included) is raised with what to do about it,
    followed by Onedata's own message (except after the hint for a provider's
    name, which Onedata's message contradicts). Both arguments are stripped of
    surrounding whitespace first, and the stripped values are sent.

    Args:
        file_id_or_path: ``/<space>/<path>`` or an opaque file id.
        target_provider_id: provider the data is copied TO
            (``replicatingProviderId``).
        check_providers: refuse a target that is not the id of a provider supporting the
            file's space, before any transfer is scheduled (`_refuse_unusable_target`). Off by
            default: the MCP tool turns it on. The argument checks and the error hints apply
            either way.

    Returns: ``{"transferId": "<id>", "note": SCHEDULED_NOTE}``. The note repeats, where a
    model reads the result, what the tool's description says: scheduling is not copying, and
    the target's blocks, re-checked until they cover the file, show when the copy is complete.
    """
    file_arg, target = file_id_or_path.strip(), target_provider_id.strip()
    _check_replication_args(file_arg, target)
    try:
        file_id = await _lookup_path(file_arg) if file_arg.startswith("/") else file_arg
    except OnedataApiError as error:  # the path lookup, status-aware (`_lookup_path`)
        hint = _lookup_error_hint(_shown(file_arg), error)
        if hint is None:
            raise
        raise _hinted(hint, error) from error
    if check_providers:
        await _refuse_unusable_target(file_arg, file_id, target)
    try:
        scheduled = await create_transfer(file_id, "replication", replicating_provider_id=target)
    except OnedataApiError as error:
        hint = await _replication_error_hint(error, file_arg, file_id, target)
        if hint is None:
            raise
        raise _hinted(hint, error) from error
    return {**scheduled, "note": SCHEDULED_NOTE} if isinstance(scheduled, dict) else scheduled
