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

from onedata_mcp.api.files import _normalize_path_to_file_id
from onedata_mcp.api.spaces import resolve_space_id_or_name
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.utils import OnedataApiError, redact_endpoints, request

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
    params: dict[str, Any] = {"state": state, "limit": limit}
    if page_token:
        params["page_token"] = page_token
    response = await request(
        config,
        "GET",
        f"/spaces/{space_id}/transfers",
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
    response = await request(config, "GET", f"/transfers/{transfer_id}")
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
# when it was given as an id (a path has already resolved to a file by then) or the key names
# the file field. A target that is the NAME of one of the space's providers is answered with
# that provider's id, first. Onedata does not refuse a second replication of a file that is
# already being copied: no status in the swagger says so, and nothing here pretends otherwise.
# ---------------------------------------------------------------------------

_SHOWN_MAX = 80
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
    least: `/log.dat` is missing its space. `.` and `..` name nothing. Ids are otherwise opaque,
    so nothing stricter is checked: a provider NAME passes here and is answered with its id
    after Onedata refuses it.
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
) -> list[tuple[str, str]] | None:
    """(name, id) of the providers supporting the file's space, or None if not found.

    Only on the error path: the space comes from `_space_id_for`, and one `GET /spaces/{sid}`
    (the id percent-encoded) lists its providers. Any failure here gives None, so the hint
    falls back to plain words.
    """
    space_id = await _space_id_for(file_arg, file_id, error)
    if not space_id:
        return None
    config = get_oneprovider_config()
    try:
        response = await request(config, "GET", f"/spaces/{quote(space_id, safe='')}")
    except Exception:  # noqa: BLE001 - the hint is best-effort; the original error is raised
        return None
    body = response["body"]
    providers = body.get("providers") if isinstance(body, dict) else None
    out: list[tuple[str, str]] = []
    for entry in providers if isinstance(providers, list) else ():
        if not isinstance(entry, dict):
            continue
        pid, name = entry.get("providerId"), entry.get("providerName")
        if isinstance(pid, str) and pid:
            shown_name = name if isinstance(name, str) and name else "(no name)"
            out.append((redact_endpoints(shown_name, config), pid))
    return out or None


def _providers_sentence(providers: list[tuple[str, str]] | None) -> str:
    """Which ids are valid targets."""
    if not providers:
        return "Give the id (not the name) of a provider that supports the file's space."
    listed = ", ".join(f"{name} (id {pid!r})" for name, pid in providers)
    return f"The providers that support the file's space are: {listed}."


def _id_of_name(providers: list[tuple[str, str]] | None, target: str) -> str | None:
    """The id of the space's provider whose NAME the target is, if it is one."""
    return next((pid for name, pid in providers or () if name == target), None)


def _error_parts(error: OnedataApiError) -> tuple[int | None, str | None, str | None, Any]:
    response = error.response if isinstance(error.response, dict) else {}
    error_obj = error.body.get("error")
    details = error_obj.get("details") if isinstance(error_obj, dict) else None
    key = details.get("key") if isinstance(details, dict) else None
    return response.get("status_code"), error.error_id, error.errno, key


def _permission_hint(shown_file: str, error: OnedataApiError) -> str | None:
    status, error_id, errno, _ = _error_parts(error)
    if status == 403 or error_id == "forbidden" or errno in {"eacces", "eperm"}:
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
    `details.key` where Onedata gives them. 5xx and anything else pass unchanged. In order:

    - permission;
    - a target that is the name of one of the space's providers: "Pass its id ...", whatever
      Onedata said about it (it never says the provider does not support the space), unless
      Onedata names the file field;
    - Onedata says the space is not supported by that id: no provider with that id supports
      the space (true of a name, too, when the providers could not be listed);
    - `details.key` names the provider field: not a valid provider id for the space (no claim
      about support);
    - the file: blamed when the key names the file field, or on a not-found when the file was
      given as an id; a path has already resolved to a file, so its not-found points at the
      target;
    - a bare 400: check both arguments.
    """
    status, error_id, errno, key = _error_parts(error)
    shown_file, shown_target = _shown(file_arg), _shown(target)
    if hint := _permission_hint(shown_file, error):
        return hint
    not_found = status == 404 or error_id == "notFound" or errno == "enoent"
    from_path = file_arg.startswith("/")
    file_blamed = key in _FILE_FIELDS or (not_found and not from_path)
    provider_blamed = error_id == "spaceNotSupportedBy" or key in _PROVIDER_FIELDS
    if not (provider_blamed or file_blamed or not_found or status == 400):
        return None
    # The providers, to recognise a target that is a provider's name, which is wrong whatever
    # else is; not fetched when Onedata names the file field.
    providers = None if key in _FILE_FIELDS else await _space_providers(file_arg, file_id, error)
    if (pid := _id_of_name(providers, target)) is not None:
        return (
            f"Pass its id {pid!r}, not its name: {shown_target} is the name of a provider that "
            f"supports the space of {shown_file}. {_providers_sentence(providers)}"
        )
    if error_id == "spaceNotSupportedBy":
        return (
            f"Cannot replicate to {shown_target}: no provider with that id supports the space of "
            f"{shown_file}. {_providers_sentence(providers)}"
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
) -> dict[str, Any]:
    """Schedule a replication transfer of a file to a target provider.

    Replication-only wrapper over :func:`create_transfer`: resolves the file
    (logical path or opaque id) and POSTs a ``type=replication`` transfer that
    copies the data TO ``target_provider_id``. Returns as soon as the transfer
    is scheduled, before any data is copied.

    Neither argument goes into a URL path: the file id and the provider id travel
    in the JSON body, and a path is percent-encoded whole into the lookup
    (``/lookup-file-id/<path>``). A value that cannot name a file or a provider is
    refused before any request; a failure the caller can act on (unknown file,
    a target that does not support the space or is a name, no permission, the
    path lookup included) is raised with what to do about it, followed by
    Onedata's own message. Both arguments are stripped of surrounding whitespace
    first, and the stripped values are sent.

    Args:
        file_id_or_path: ``/<space>/<path>`` or an opaque file id.
        target_provider_id: provider the data is copied TO
            (``replicatingProviderId``).

    Returns: ``{"transferId": "<id>"}``.
    """
    file_arg, target = file_id_or_path.strip(), target_provider_id.strip()
    _check_replication_args(file_arg, target)
    try:
        file_id = await _normalize_path_to_file_id(file_arg)
    except OnedataApiError as error:  # the path lookup: only a permission error has a hint
        hint = _permission_hint(_shown(file_arg), error)
        if hint is None:
            raise
        raise OnedataApiError(f"{hint} [{error}]", response=error.response) from error
    try:
        return await create_transfer(file_id, "replication", replicating_provider_id=target)
    except OnedataApiError as error:
        hint = await _replication_error_hint(error, file_arg, file_id, target)
        if hint is None:
            raise
        raise OnedataApiError(f"{hint} [{error}]", response=error.response) from error
