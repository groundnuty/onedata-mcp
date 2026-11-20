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
# id `spaceNotSupportedBy`, or `details.key` naming the provider field). Onedata does not refuse
# a second replication of a file that is already being copied: no status in the swagger says
# so, and nothing here pretends otherwise.
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


def _check_replication_args(file_id_or_path: str, target_provider_id: str) -> None:
    """Refuse, before any request, a value that cannot name a file or a provider.

    A value with a `/` that does not start with one is a relative path (Onedata would read it
    as a malformed file id); `.` and `..` name nothing. Ids are otherwise opaque, so nothing
    stricter is checked: a provider NAME passes here and is answered by Onedata, with the hint
    below.
    """
    file_arg = file_id_or_path.strip()
    if not file_arg:
        raise ValueError(f"file_id_or_path is empty: give a file id, or {_PATH_FORM}.")
    if "/" in file_id_or_path and not file_id_or_path.startswith("/"):
        raise ValueError(
            "Paths must start with '/': use /<space_name>/<path_to_file> "
            f"(got {_shown(file_id_or_path)})."
        )
    if file_arg in {".", ".."}:
        raise ValueError(
            f"file_id_or_path {_shown(file_id_or_path)} is not a file id or a path: give a file "
            f"id, or {_PATH_FORM}."
        )
    target = target_provider_id.strip()
    if not target:
        raise ValueError(
            "target_provider_id is empty: give the id of a provider that supports the file's space."
        )
    if "/" in target_provider_id or target in {".", ".."}:
        raise ValueError(
            f"target_provider_id {_shown(target_provider_id)} is not a provider id: give the id "
            "of a provider that supports the file's space."
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


async def _space_providers(file_id: str, error: OnedataApiError) -> list[tuple[str, str]] | None:
    """(name, id) of the providers supporting the file's space, or None if not found.

    Only on the error path: the space comes from Onedata's error details or from the file id,
    and one `GET /spaces/{sid}` (the id percent-encoded) lists its providers. Any failure here
    gives None, so the hint falls back to plain words.
    """
    error_obj = error.body.get("error")
    details = error_obj.get("details") if isinstance(error_obj, dict) else None
    space_id = details.get("spaceId") if isinstance(details, dict) else None
    if not isinstance(space_id, str) or not _ID_RE.fullmatch(space_id):
        space_id = _space_id_of_file_id(file_id)
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


def _providers_sentence(providers: list[tuple[str, str]] | None, target: str) -> str:
    """Which ids are valid targets; and, for a provider NAME given as the id, its id."""
    if not providers:
        return "Give the id (not the name) of a provider that supports the file's space."
    listed = ", ".join(f"{name} (id {pid!r})" for name, pid in providers)
    sentence = f"The providers that support the file's space are: {listed}."
    named = [pid for name, pid in providers if name == target.strip()]
    if named:
        sentence = f"{_shown(target)} is a provider's name; its id is {named[0]!r}. " + sentence
    return sentence


def _error_parts(error: OnedataApiError) -> tuple[int | None, str | None, str | None, Any]:
    response = error.response if isinstance(error.response, dict) else {}
    error_obj = error.body.get("error")
    details = error_obj.get("details") if isinstance(error_obj, dict) else None
    key = details.get("key") if isinstance(details, dict) else None
    return response.get("status_code"), error.error_id, error.errno, key


async def _replication_error_hint(
    error: OnedataApiError, file_id_or_path: str, file_id: str, target: str
) -> str | None:
    """What the caller can do about a failed schedule, or None to pass the error on unchanged.

    Keyed on the status the swagger lists (400, 403, 404), and on the error id, errno and
    `details.key` where Onedata gives them. 5xx and anything else pass unchanged.
    """
    status, error_id, errno, key = _error_parts(error)
    shown_file = _shown(file_id_or_path)
    if status == 403 or error_id == "forbidden" or errno in {"eacces", "eperm"}:
        return (
            f"Not allowed to schedule a replication of {shown_file}: the token may not schedule "
            "replications in this space (the space privilege space_schedule_replication), may "
            "not read the file, or is read-only. Calling again will not help."
        )
    if error_id == "spaceNotSupportedBy" or key in _PROVIDER_FIELDS:
        providers = await _space_providers(file_id, error)
        return (
            f"Cannot replicate to {_shown(target)}: it is not a provider that supports the "
            f"space of {shown_file}. {_providers_sentence(providers, target)}"
        )
    if key in _FILE_FIELDS or status == 404 or error_id == "notFound" or errno == "enoent":
        return (
            f"No file is known by {shown_file}: give the file's id, or {_PATH_FORM}. "
            "A file name alone is neither."
        )
    if status == 400:
        providers = await _space_providers(file_id, error)
        return (
            f"Onedata rejected the replication of {shown_file} to {_shown(target)}. Check both "
            f"arguments: the file is a file id, or {_PATH_FORM}; the target is a provider id. "
            f"{_providers_sentence(providers, target)}"
        )
    return None


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
    a target that does not support the space or is a name, no permission) is
    raised with what to do about it, followed by Onedata's own message.

    Args:
        file_id_or_path: ``/<space>/<path>`` or an opaque file id.
        target_provider_id: provider the data is copied TO
            (``replicatingProviderId``).

    Returns: ``{"transferId": "<id>"}``.
    """
    _check_replication_args(file_id_or_path, target_provider_id)
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    try:
        return await create_transfer(
            file_id, "replication", replicating_provider_id=target_provider_id
        )
    except OnedataApiError as error:
        hint = await _replication_error_hint(error, file_id_or_path, file_id, target_provider_id)
        if hint is None:
            raise
        raise OnedataApiError(f"{hint} [{error}]", response=error.response) from error
