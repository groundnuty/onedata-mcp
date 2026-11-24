import json
import posixpath
import re
from asyncio.log import logger
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import httpx

from onedata_mcp.api.spaces import _space_ids_of_provider_listing, list_user_spaces
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.utils import (
    OnedataApiError,
    OnedataInvalidSpaceError,
    decode_response_body,
    http_failure_message,
    id_segment,
    path_segments,
    redact_endpoints,
    request,
    transport_failure,
)

DEFAULT_FILE_ATTRIBUTE_KEYS = (
    # "fileId",
    "path",
    # "parentFileId",
    "name",
    "type",
    "size",
    "posixPermissions",
    # "ownerUserId",
    # "originProviderId",
    "atime",
    "mtime",
    # "ctime",
    # "hardlinkCount",
)

DEPRECATED_ATTRIBUTE_NAME_MAPPING = {
    "file_id": "fileId",
    "mode": "posixPermissions",
    "parent_id": "parentFileId",
    "storage_group_id": "displayGid",
    "storage_user_id": "displayUid",
    "is_fully_replicated": "isFullyReplicatedLocally",
    "provider_id": "originProviderId",
    "shares": "directShareIds",
    "owner_id": "ownerUserId",
    "hardlinks_count": "hardlinkCount",
}


def _reject_deprecated_attributes(attributes: Iterable[str] | None) -> None:
    if attributes is None:
        return

    deprecated = [attr for attr in attributes if attr in DEPRECATED_ATTRIBUTE_NAME_MAPPING]
    if not deprecated:
        return

    replacements = ", ".join(
        f"{old}->{DEPRECATED_ATTRIBUTE_NAME_MAPPING[old]}" for old in sorted(set(deprecated))
    )
    raise ValueError(
        f"Deprecated attribute names are not supported. Use the new names instead: {replacements}"
    )


def _strip_deprecated_fields_in_list(
    response_body: dict[str, Any], list_key: str
) -> dict[str, Any]:
    items = response_body.get(list_key)
    if not isinstance(items, list):
        return response_body

    deprecated_keys = set(DEPRECATED_ATTRIBUTE_NAME_MAPPING.keys())
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in deprecated_keys:
            item.pop(key, None)

    return response_body


async def _raise_invalid_space_error_if_needed(error: OnedataApiError, path: str) -> None:
    if error.error_id != "spaceNotSupportedBy":
        return
    error_details = error.body.get("error", {}).get("details", {})
    requested_space_name = (
        error_details.get("spaceId") if isinstance(error_details.get("spaceId"), str) else None
    )
    if not requested_space_name and path.startswith("/"):
        requested_space_name = path.split("/")[1]

    try:
        spaces = await list_user_spaces()
        space_names = sorted(
            {space["name"] for space in spaces if isinstance(space.get("name"), str)}
        )
    except Exception:
        space_names = []

    if requested_space_name:
        # The id came from the service's answer: pass it through the endpoint redaction.
        requested_space_name = redact_endpoints(requested_space_name, get_oneprovider_config())
    requested_part = (
        f'Space "{requested_space_name}" does not exist.'
        if requested_space_name
        else "Space does not exist."
    )
    quoted_names = ", ".join(f'"{name}"' for name in space_names)
    hint = f" Available spaces: {quoted_names}." if quoted_names else ""
    raise OnedataInvalidSpaceError(f"{requested_part}{hint}", response=error.response) from error


async def get_file_id(path: str) -> str:
    config = get_oneprovider_config()
    normalized_path = path if path.startswith("/") else f"/{path}"
    encoded_path = quote(normalized_path, safe="")
    try:
        response = await request(config, "POST", f"/lookup-file-id/{encoded_path}")
    except OnedataApiError as e:
        logger.debug(f"Error getting file id for path {path}: {e}")
        await _raise_invalid_space_error_if_needed(e, path)
        if e.errno == "enoent":
            raise FileNotFoundError(f'Path "{path}" not found') from e
        raise
    return response["body"]["fileId"]


# --- A file id in a request path, and errors a caller can act on --------------------------------
#
# Used by get_file_attributes, list_children and create_directory (design/13-15). They keep
# their own refusals and answers. Every tool puts a file id into the path through
# `_file_id_segment` (`utils.id_segment`, issue #44), the id a path lookup returned included:
# the id is encoded, and an empty id, `.`, `..` or a `/` is refused before any request.

_SHOWN_MAX = 80
_HOW_TO_NAME_A_FILE = "Give a path, /<space_name>/<path>, or a file id."
_HOW_TO_NAME_A_FILE_PATH = "Give the full path, /<space_name>/<path>."


def _file_id_segment(file_id: str) -> str:
    """A file id as one percent-encoded path segment (`utils.id_segment`), the refusal saying
    how to name a file. The id is Onedata's own when a path was resolved first, and a caller's
    otherwise."""
    return id_segment(file_id, "file id", hint=_HOW_TO_NAME_A_FILE)


def _shown(value: str) -> str:
    """The value as echoed in an error: quoted, and cut short if it is long."""
    if len(value) <= _SHOWN_MAX:
        return repr(value)
    return repr(value[:_SHOWN_MAX]) + "..."


def _target(file_id_or_path: str) -> str:
    kind = "path" if file_id_or_path.startswith("/") else "file id"
    return f"{kind} {_shown(file_id_or_path)}"


def _refuse_unusable_id(file_id: str, file_id_or_path: str) -> None:
    """An empty id, `.` or `..` is refused before any request: the HTTP client resolves dot
    segments to another URL (`/data/..` is the API root)."""
    if not file_id.strip():
        raise ValueError(f"The file id or path is empty. {_HOW_TO_NAME_A_FILE}")
    if file_id.strip() in {".", ".."}:
        raise ValueError(
            f"{_shown(file_id_or_path)} is not a file id or path. {_HOW_TO_NAME_A_FILE}"
        )


def _id_in_path(file_id: str, file_id_or_path: str) -> str:
    """The file id as one percent-encoded path segment, through the shared guard
    (`_file_id_segment`): whether the caller gave it or a path lookup returned it.

    Unencoded, a `?` or `#` in an id cuts the request path short; a `/` would add segments, and
    is refused. An empty id, `.` or `..` is refused first with the caller's value echoed
    (`_refuse_unusable_id`)."""
    _refuse_unusable_id(file_id, file_id_or_path)
    return _file_id_segment(file_id)


# --- A complete file id, and a refusal that may only mean a bad copy ----------------------------
#
# A model copies a 180-character hex id out of an earlier answer, and sometimes drops, changes,
# doubles or splices characters (design/12-14, gate 4). Onedata answers such an id with 403,
# `spaceNotSupportedBy`, `badValueIdentifier` or a bare 400, none of which says "bad copy", or,
# for a header spliced onto another file's payload, acts on that other file. So the tools refuse,
# before any request, an id that is not one Onedata's encoder could have written. Only for an id
# the caller copied (`copied_id`, which the MCP tool sets): readiness and the stable download
# tool pass ids that came from Onedata and keep their own handling.

_FILE_ID_HEADER = 8  # bytes
_FILE_ID_MAX_PAYLOAD = 320  # bytes: ctool's `file_id:build_objectid/2` refuses a longer one
_UNSEEN_SPACE = " The space in the id is not one this token sees at this provider."
# After a refusal of a whole id: the id is one Onedata wrote, but perhaps another file's.
_NOT_THE_FILE = (
    "If it is not the id of the file you mean, copy that file's id exactly from the earlier "
    "answer, or give the path, /<space_name>/<path>."
)
# The payload of the id of an older Onedata: the Erlang term {guid, Uuid, SpaceId}, as
# `83 h 03 d 00 04 "guid"` and two binaries (`m`, a 4-byte length, the bytes).
_TERM_GUID = b"\x83h\x03d\x00\x04guid"


def _crc16(data: bytes) -> int:
    """The CRC-16 of ctool's `file_id:crc16/1`: polynomial 0x8005, reflected, initial value 0,
    no final XOR (its check value, of `123456789`, is 0xBB3D)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _object_id_payload(value: str) -> bytes | None:
    """The payload of `value` if it is an object id as Onedata's encoder writes it; None else.

    ctool `src/utils/file_id.erl` (`build_objectid/2`) writes, in hex, an 8-byte header and the
    payload: a zero byte, the 3-byte enterprise number, a zero byte, the payload's length as one
    byte (`Length:8`, so modulo 256), and a CRC-16 of the whole id written with that field zero,
    big-endian. It refuses a payload over 320 bytes. On decode (`objectid_to_guid/1`) 25.0 checks
    only the two zero bytes, so a changed digit or a header spliced onto another file's payload
    reaches it as another, valid-looking id; the CRC is what shows them."""
    if not value or len(value) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", value):
        return None
    raw = bytes.fromhex(value)
    header, payload = raw[:_FILE_ID_HEADER], raw[_FILE_ID_HEADER:]
    if not payload or len(payload) > _FILE_ID_MAX_PAYLOAD:
        return None
    if header[0] != 0 or header[4] != 0 or header[5] != len(payload) % 256:
        return None
    if int.from_bytes(header[6:8], "big") != _crc16(header[:6] + b"\0\0" + payload):
        return None
    return payload


def _term_ids(payload: bytes) -> list[bytes] | None:
    """The uuid and space id of an older id's payload, `{guid, Uuid, SpaceId}` as an Erlang term;
    None if it is not exactly that, each binary within what is left of the payload."""
    rest, ids = payload[len(_TERM_GUID) :], []
    for _ in range(2):
        if len(rest) < 5 or rest[0] != 0x6D:
            return None
        size = int.from_bytes(rest[1:5], "big")
        if len(rest) < 5 + size:
            return None
        ids.append(rest[5 : 5 + size])
        rest = rest[5 + size :]
    return None if rest else ids


def _file_id_parts(value: str) -> tuple[str, str] | None:
    """(uuid, space id) of a whole file id; None for anything else.

    The id must be an object id as the encoder writes it (`_object_id_payload`: the zero bytes,
    the length byte, the 320-byte limit and the CRC, for every form). Its payload is one of:

    - `guid#<uuid>#<space id>`: what a 25.0 Oneprovider returns (ctool `file_id:pack_guid/2`);
    - `shareGuid#<uuid>#<space id>#<share id>`, the id of a file seen through a share
      (`pack_share_guid/3`, encoded by the same `build_objectid`);
    - the Erlang term `{guid, Uuid, SpaceId}`, the older id of the swagger README's example.
      25.0's own decoder (`unpack_share_guid/1`) reads only the two `#` forms, so it would refuse
      this one itself; it is accepted here so that no id Onedata documents is refused locally.

    The space id is not empty (`pack_guid/2` requires that). The parts have no length cap: the
    payload limit bounds them, and under a longer `datastore_doc_key_length` a trash or archives
    root's uuid is over 128 characters. All the ids a 25.0 Oneprovider returned in the
    validation runs pass (`test_file_ids.py`).
    """
    payload = _object_id_payload(value)
    if payload is None:
        return None
    ids: list[bytes] | None
    if payload.startswith(_TERM_GUID):
        ids = _term_ids(payload)
    else:
        parts = payload.split(b"#")
        if parts[0] == b"guid" and len(parts) == 3:
            ids = parts[1:]
        elif parts[0] == b"shareGuid" and len(parts) == 4:
            ids = parts[1:3]
        else:
            return None
    if ids is None or not ids[1]:
        return None
    try:
        uuid, space_id = (part.decode("ascii") for part in ids)
    except UnicodeDecodeError:  # Onedata's keys are alphanumeric (cluster-worker datastore_key)
        return None
    return uuid, space_id


def _incomplete_file_id_message(value: str) -> str:
    """What to tell the caller about a value that is not a path and cannot be a whole id."""
    if "/" in value:  # a relative path
        return (
            f"Paths must start with '/': use /<space_name>/<path> (got {_shown(value)}), "
            "or give a file id."
        )
    return (
        f"{_shown(value)} is not a complete file id; copy it exactly from the earlier answer, "
        "or give the path, /<space_name>/<path>."
    )


def _require_complete_id(value: str) -> None:
    """Refuse, before any request, an id that cannot be whole (`_file_id_parts`). A path is not
    checked."""
    _refuse_unusable_id(value, value)
    if _file_id_parts(value) is None:
        raise ValueError(_incomplete_file_id_message(value))


async def _space_of_id_unseen(file_id: str) -> bool:
    """The id is whole, and its space is not among the token's spaces at this provider
    (`GET /spaces`, the ids only). False when that cannot be established: it is a hint."""
    parts = _file_id_parts(file_id)
    if parts is None:
        return False
    try:
        response = await request(get_oneprovider_config(), "GET", "/spaces")
        seen = set(_space_ids_of_provider_listing(response["body"]))
    except Exception:  # noqa: BLE001 - the hint is best effort
        return False
    return bool(seen) and parts[1] not in seen


async def _id_space_note(file_id: str, *, list_spaces: bool) -> str:
    """For a refused id: that its space is not one the token sees (when that is so), and with
    `list_spaces` the names of the spaces it does see. On the error path only."""
    note = _UNSEEN_SPACE if await _space_of_id_unseen(file_id) else ""
    return note + (await _visible_spaces() if list_spaces else "")


def _status(error: OnedataApiError) -> int | None:
    response = error.response if isinstance(error.response, dict) else {}
    status = response.get("status_code")
    return status if isinstance(status, int) else None


# A specific diagnosis needs a structured Onedata error (an error id or an errno in the body) on
# a 4xx status. The same body on a 5xx says the provider failed, not what the failure was
# about: an `enoent` in a 500 does not show that the file is missing. A 5xx therefore gets
# either no hint (the error as it is) or, for a not-found, the uncertain one
# (`_unconfirmed_hint`), and never "not found" or "missing parent".


def _is_client_error(error: OnedataApiError) -> bool:
    status = _status(error)
    return status is not None and 400 <= status < 500


def _errno_is(error: OnedataApiError, errno: str) -> bool:
    """The errno is `errno`, on a 4xx status."""
    return _is_client_error(error) and error.errno == errno


def _is_forbidden(error: OnedataApiError) -> bool:
    return _status(error) == 403 or (
        _is_client_error(error)
        and (error.error_id == "forbidden" or error.errno in {"eacces", "eperm"})
    )


def _is_not_found(error: OnedataApiError) -> bool:
    """A structured not-found on a 4xx status: an `enoent` errno or the `notFound` error id. A
    bare 404 (no such body) is not one: a provider that cannot resolve or disclose a target
    answers 404 too. A 5xx carrying one is `_is_unconfirmed_not_found`."""
    return _is_client_error(error) and (error.errno == "enoent" or error.error_id == "notFound")


def _is_unconfirmed_not_found(error: OnedataApiError) -> bool:
    """A 5xx whose body mentions a missing item: the provider failed, and what is missing is
    not established."""
    status = _status(error)
    return (
        status is not None
        and status >= 500
        and (error.errno == "enoent" or error.error_id == "notFound")
    )


def _unconfirmed_hint(target: str, error: OnedataApiError, *, creating: bool = False) -> str:
    """For a 5xx that mentions a missing item. It claims neither that the target is missing nor
    (when creating) that nothing was created."""
    status = _status(error)
    if creating:
        return (
            f"This provider answered {status} and mentioned a missing item: the provider "
            f"failed, and a parent of {target} may also not exist. It is not known whether "
            "anything was created; check what exists at the path before calling again."
        )
    return (
        f"This provider answered {status} and mentioned a missing item: the provider failed, "
        f"and {target} may also not exist. That does not show which; check the path or id, "
        "the space and the provider, and try again."
    )


def _is_bare_404(error: OnedataApiError) -> bool:
    return _status(error) == 404 and not _is_not_found(error)


def _rejects_the_id(error: OnedataApiError) -> bool:
    """A 400 about the id itself (`badValueIdentifier`, or a bad value whose key is `id`), not
    about another input such as an unknown attribute name, which passes on unchanged."""
    if _status(error) != 400:
        return False
    body_error = error.body.get("error")
    details = body_error.get("details") if isinstance(body_error, dict) else None
    key = details.get("key") if isinstance(details, dict) else None
    return error.error_id == "badValueIdentifier" or (
        (error.error_id or "").startswith("badValue") and key == "id"
    )


async def _visible_spaces() -> str:
    """` Spaces this token sees at this provider: "a", "b".`, or "" (best effort)."""
    try:
        names = sorted(
            {s["name"] for s in await list_user_spaces() if isinstance(s.get("name"), str)}
        )
    except Exception:  # noqa: BLE001 - the hint is best effort
        return ""
    if not names:
        return ""
    return " Spaces this token sees at this provider: " + ", ".join(f'"{n}"' for n in names) + "."


def _bare_404_hint(target: str, what: str) -> str:
    return (
        f"This provider answered 404 for {target} without saying why: {what}, its space may not "
        "be supported by this provider, or the token may not see it. Check the path or id, the "
        "space and the token's access."
    )


async def _file_error(
    error: OnedataApiError,
    file_id_or_path: str,
    action: str,
    *,
    lookup: bool = False,
    copied_id: bool = False,
) -> Exception:
    """What a failed call on a file or directory tells the caller, from what this provider
    established; the error itself when there is nothing to add. A hint is followed by Onedata's
    own message. Specific diagnoses need distinguishing evidence: a 403 or an access errno, a
    structured not-found, `spaceNotSupportedBy`, an `enotdir` while resolving a path, an id
    rejected as such. A bare 404 gets the possible causes, not one of them. `lookup`: the error
    came from resolving the path. `copied_id`: the caller typed the id from an earlier answer.
    Such an id passed `_require_complete_id`, so it is one Onedata wrote, but it may be another
    file's: for an id, a 403 or `spaceNotSupportedBy` gives the provider's refusal first and then,
    as a condition, what to do if it is not the file meant (a path keeps the diagnoses above)."""
    target = _target(file_id_or_path)
    copied = copied_id and not lookup and not file_id_or_path.startswith("/")
    if _is_forbidden(error) and copied:
        hint = (
            f"Not allowed to {action} {target}: the token has no access to it. {_NOT_THE_FILE}"
            f"{await _id_space_note(file_id_or_path, list_spaces=False)}"
        )
    elif _is_forbidden(error):
        hint = (
            f"Not allowed to {action} {target}: the token has no access to it. Calling again "
            "will not help."
        )
    elif _is_client_error(error) and error.error_id == "spaceNotSupportedBy" and copied:
        hint = (
            f"This provider does not support the space of {target}, so it cannot {action} it "
            f"here; the space may still exist. {_NOT_THE_FILE}"
            f"{await _id_space_note(file_id_or_path, list_spaces=True)}"
        )
    elif _is_client_error(error) and error.error_id == "spaceNotSupportedBy":
        hint = (
            f"This provider does not support the space of {target}, so it cannot {action} it "
            f"here; the space may still exist.{await _visible_spaces()}"
        )
    elif lookup and _errno_is(error, "enotdir"):
        hint = (
            f"A component of {target} before its last part is not a directory, so this provider "
            "cannot resolve the path."
        )
    elif lookup and _is_not_found(error):
        return FileNotFoundError(f'Path "{file_id_or_path}" not found')
    elif _is_not_found(error):
        hint = (
            f"This provider reports nothing at {target}: it may have been deleted, or the value "
            f"is not a file id. {_HOW_TO_NAME_A_FILE}"
        )
    elif _rejects_the_id(error):
        hint = f"This provider rejected {target} as a file id. {_HOW_TO_NAME_A_FILE}"
    elif _is_bare_404(error):
        hint = _bare_404_hint(target, "it may not exist")
    elif _is_unconfirmed_not_found(error):
        hint = _unconfirmed_hint(target, error)
    else:
        return error
    return _with_hint(hint, error)


def _with_hint(hint: str, error: OnedataApiError) -> OnedataApiError:
    """The hint first, then Onedata's own message."""
    return OnedataApiError(f"{hint} [{error}]", response=error.response)


async def _lookup(path: str, action: str) -> str:
    """The file id of `path` at this provider (`POST /lookup-file-id/{path}`), with failures
    mapped by `_file_error` (a structured not-found is `Path "..." not found`). The stable tools
    keep `get_file_id`, whose own mapping reads `spaceNotSupportedBy` as a missing space."""
    try:
        response = await request(
            get_oneprovider_config(), "POST", f"/lookup-file-id/{quote(path, safe='')}"
        )
    except OnedataApiError as e:
        replacement = await _file_error(e, path, action, lookup=True)
        if replacement is e:
            raise
        raise replacement from e
    return response["body"]["fileId"]


async def _file_id_for(file_id_or_path: str, action: str, *, copied_id: bool = False) -> str:
    """A path resolved with `_lookup`; an id as given, refused first when `copied_id` and it
    cannot be whole (`_require_complete_id`)."""
    if file_id_or_path.startswith("/"):
        return await _lookup(file_id_or_path, action)
    if copied_id:
        _require_complete_id(file_id_or_path)
    return file_id_or_path


async def get_file_attributes(
    file_id_or_path: str,
    *,
    attributes: Iterable[str] | None = DEFAULT_FILE_ATTRIBUTE_KEYS,
    copied_id: bool = False,
) -> dict[str, Any]:
    """The attributes of a file or directory, given by path or id.

    Without `attributes` (or with an empty list), the default set is asked for. A deprecated
    attribute name is refused with its new name; a failure is raised with what this provider
    established about it (`_file_error`), followed by Onedata's own message.

    `copied_id` is set by the MCP tool, whose id a model copied from an earlier answer: an id
    that Onedata's encoder could not have written is then refused before any request, and a
    refusal of an id says what to do if it is not the file meant. A path's id, from the lookup,
    is not checked as a copy. The other callers (the stable download tool, readiness) pass ids
    that came from Onedata and keep their handling."""
    config = get_oneprovider_config()
    requested_attributes = tuple[str, ...](attributes or DEFAULT_FILE_ATTRIBUTE_KEYS)
    _reject_deprecated_attributes(requested_attributes)

    action = "read the attributes of"
    file_id = await _file_id_for(file_id_or_path, action, copied_id=copied_id)
    try:
        response = await request(
            config,
            "GET",
            f"/data/{_id_in_path(file_id, file_id_or_path)}",
            json_body={"attributes": list(requested_attributes)},
        )
    except OnedataApiError as e:
        replacement = await _file_error(e, file_id_or_path, action, copied_id=copied_id)
        if replacement is e:
            raise
        raise replacement from e
    logger.debug(f"Fetched file attributes for file {file_id_or_path}: {response['body']}")
    return response["body"]


async def _normalize_path_to_file_id(file_id_or_path: str) -> str:
    if not file_id_or_path.startswith("/"):
        return file_id_or_path

    return await get_file_id(file_id_or_path)


async def list_children(
    parent_id_or_path: str,
    *,
    attributes: Iterable[str] | None = DEFAULT_FILE_ATTRIBUTE_KEYS,
    limit: int,
    offset: int,
    token: str | None = None,
) -> dict[str, Any]:
    """One page of a directory's entries, given by path or id.

    `token` (a previous page's `nextPageToken`) and `offset` are exclusive: the swagger says
    `offset` "cannot be provided alongside the `token` parameter", so with a token no offset is
    sent, and a non-zero offset with a token is refused. A failure is raised with what this
    provider established about it (`_file_error`; not a directory), followed by Onedata's own
    message. 25.0 lists a regular file as its own only entry instead of failing; that first
    page is refused here (`_lists_itself`). An id that Onedata's encoder could not have written
    is refused before any request, and a 403 or `spaceNotSupportedBy` for an id says what to do
    if it is not the directory meant. When `isLast` is false the answer says so in `more`
    (`_more_note`)."""
    config = get_oneprovider_config()
    requested_attributes = tuple[str, ...](attributes or DEFAULT_FILE_ATTRIBUTE_KEYS)
    _reject_deprecated_attributes(requested_attributes)
    if token is not None and offset:
        raise ValueError(
            "Pass token or offset, not both: token continues after the page it came with, "
            "offset counts entries from the start of the directory."
        )
    request_body: dict[str, Any] = {"limit": limit, "attributes": list(requested_attributes)}
    if token is not None:
        request_body["token"] = token
    else:
        request_body["offset"] = offset

    parent_id = await _file_id_for(parent_id_or_path, "list", copied_id=True)
    try:
        response = await request(
            config,
            "GET",
            f"/data/{_id_in_path(parent_id, parent_id_or_path)}/children",
            json_body=request_body,
        )
    except OnedataApiError as e:
        if _errno_is(e, "enotdir"):  # the target itself resolved, and is not a directory
            hint = (
                f"The {_target(parent_id_or_path)} is not a directory: only a directory has "
                "entries to list."
            )
            raise _with_hint(hint, e) from e
        status = (e.response or {}).get("status_code") if isinstance(e.response, dict) else None
        if token is not None and isinstance(status, int) and status >= 500:
            # Onedata 25.0 answers a made-up or stale page token with 500 internalServerError
            # (seen live 2026-11-21); the uncertainty stays, but the token is the likely cause.
            hint = (
                "The provider failed on this page token. A token is valid only as returned in "
                "the previous page's nextPageToken; to start over, list again without a token."
            )
            raise _with_hint(hint, e) from e
        replacement = await _file_error(e, parent_id_or_path, "list", copied_id=True)
        if replacement is e:
            raise
        raise replacement from e
    body = response["body"]
    if token is None and not offset and await _lists_itself(body, parent_id, parent_id_or_path):
        raise NotADirectoryError(
            f"{_shown(parent_id_or_path)} is a regular file, not a directory; it has no children."
        )
    return _more_note(_strip_deprecated_fields_in_list(body, "children"))


_MORE = "More entries follow: call again with token set to nextPageToken."


def _more_note(body: Any) -> Any:
    """While `isLast` is false, say it in the answer itself (`more`), where a model reads the
    page, not only in the tool's description: gate 4 had models stop at a page that said
    `isLast: false` and answer for the whole directory. Nothing is added to a last page, or to
    an answer that does not say."""
    if isinstance(body, dict) and body.get("isLast") is False:
        body["more"] = _MORE if body.get("nextPageToken") else "More entries follow."
    return body


async def _kind_of(file_id: str) -> str | None:
    """The `type` of the file with this id (REG, DIR or SYMLNK); None if it cannot be read, or
    if the id cannot be one path segment (`_file_id_segment`)."""
    try:
        response = await request(
            get_oneprovider_config(),
            "GET",
            f"/data/{_file_id_segment(file_id)}",
            json_body={"attributes": ["type"]},
        )
    except Exception:  # noqa: BLE001 - then nothing is said about what the target is
        return None
    body = response["body"]
    kind = body.get("type") if isinstance(body, dict) else None
    return kind if isinstance(kind, str) else None


def _canonical_path(path: str) -> str:
    """`path` without empty components, with `.` and `..` resolved: `/a//b/` is `/a/b`."""
    return posixpath.normpath("/" + "/".join(part for part in path.split("/") if part))


async def _lists_itself(body: Any, file_id: str, file_id_or_path: str) -> bool:
    """Onedata 25.0 does not refuse to list a regular file: it answers with the file itself as
    the only entry (measured live), which reads as "the directory holds a file of this name".
    True when this first page is that: exactly one entry, which is the target.

    A directory never lists itself, so when the target was given as a path and the entry has a
    `path`, the two decide it with no further request: equal, it is the file; different, it is a
    directory's own entry. Otherwise (the target was given as an id, or `path` was not asked
    for) a single entry of type REG is confirmed by reading the target's own type, one request;
    an entry of another type cannot be a regular file's own."""
    children = body.get("children") if isinstance(body, dict) else None
    if not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], dict):
        return False
    entry = children[0]
    kind = entry.get("type")
    if isinstance(kind, str) and kind != "REG":
        return False
    path = entry.get("path")
    if file_id_or_path.startswith("/") and isinstance(path, str):
        return _canonical_path(path) == _canonical_path(file_id_or_path)
    return await _kind_of(file_id) == "REG"


async def list_files_recursively(
    parent_id_or_path: str,
    *,
    attributes: Iterable[str] | None = DEFAULT_FILE_ATTRIBUTE_KEYS,
    limit: int,
    token: str | None = None,
    start_after: str | None = None,
    prefix: str | None = None,
) -> dict[str, Any]:
    config = get_oneprovider_config()
    parent_id = await _normalize_path_to_file_id(parent_id_or_path)
    parent_segment = _file_id_segment(parent_id)
    requested_attributes = tuple[str, ...](attributes or DEFAULT_FILE_ATTRIBUTE_KEYS)
    _reject_deprecated_attributes(requested_attributes)
    request_body: dict[str, Any] = {"limit": limit}
    if token is not None:
        request_body["token"] = token
    if start_after is not None:
        request_body["start_after"] = start_after
    if prefix is not None:
        request_body["prefix"] = prefix
    if requested_attributes:
        request_body["attributes"] = list(requested_attributes)

    try:
        response = await request(
            config,
            "GET",
            f"/data/{parent_segment}/files",
            json_body=request_body,
        )
    except OnedataApiError as e:
        await _raise_invalid_space_error_if_needed(e, parent_id_or_path)
        raise
    return _strip_deprecated_fields_in_list(response["body"], "files")


async def download_file(file_id_or_path: str) -> bytes:
    """Return raw file content as bytes (legacy single-return form)."""
    content, _ = await download_file_with_meta(file_id_or_path)
    return content


async def download_file_with_meta(file_id_or_path: str) -> tuple[bytes, str | None]:
    """Return ``(raw_bytes, content_type_or_None)``.

    Used by the MCP wrapper (M-10) to surface ``content_type`` alongside
    the body without forcing every caller of ``download_file`` to handle
    the tuple form. ``content_type`` is the upstream HTTP
    ``Content-Type`` header verbatim, or ``None`` if absent.
    """
    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    file_segment = _file_id_segment(file_id)  # before the attributes request below

    file_attributes = await get_file_attributes(file_id_or_path)
    if file_attributes["type"] == "DIR":
        raise ValueError("Cannot download content of a directory")

    if file_attributes["size"] > 5 * 1024 * 1024:
        size_in_mb = file_attributes["size"] / 1024 / 1024
        raise ValueError(
            f"File size is too large to download (max 5MB), actual: {size_in_mb:.2f}MB"
        )

    headers = dict(config.auth_headers)
    headers["Accept"] = "*/*"
    headers.pop("Content-Type", None)

    content_path = f"/data/{file_segment}/content"
    try:
        async with httpx.AsyncClient(
            base_url=config.base_url, headers=headers, verify=config.verify_ssl
        ) as client:
            response = await client.get(content_path)
    except Exception as exc:
        # Same host-free answer as `request()`: no URL, address, port or library text.
        raise transport_failure(exc, config, "GET", content_path) from None

    if not 200 <= response.status_code < 300:  # a redirect is not file data either
        raise OnedataApiError(
            http_failure_message(
                config, "GET", content_path, response.status_code, decode_response_body(response)
            ),
            response={"status_code": response.status_code, "body": response.text},
        )

    content_type = response.headers.get("Content-Type")
    return response.content, content_type


async def grep_file_content(
    file_id_or_path: str,
    pattern: str,
) -> str:

    content = await download_file(file_id_or_path)
    content_str = content.decode("utf-8", errors="replace")
    return "\n".join(line for line in content_str.splitlines() if pattern in line)


async def create_file(path: str, content: str, *, create_parents: bool = False) -> str:
    config = get_oneprovider_config()
    normalized = path.strip("/")

    if create_parents:
        if "/" not in normalized:
            raise ValueError(
                "path must be /<space_name>/<path_to_file> when create_parents is true"
            )
        space_name, relative_path = normalized.split("/", 1)
        if not relative_path:
            raise ValueError("path must include a file path under the space")
        # Refused before the lookup below: the HTTP client resolves `a/../b` to `b`.
        encoded_path = path_segments(relative_path, "path", hint=_HOW_TO_NAME_A_FILE_PATH)
        root_id = await get_file_id(f"/{space_name}")
        root_segment = _file_id_segment(root_id)
        try:
            response = await request(
                config,
                "PUT",
                f"/data/{root_segment}/path/{encoded_path}",
                params={"create_parents": True},
                body=content.encode("utf-8"),
                additional_headers={"Content-Type": "application/octet-stream"},
            )
        except OnedataApiError as e:
            if e.errno == "eexist":
                raise FileExistsError(f"File {path} already exists") from e
            logger.error(f"Error creating file {path}: {e}")
            raise e
        return response["body"]["fileId"]

    parent_path, file_name = path.rsplit("/", 1)

    parent_id = await _normalize_path_to_file_id(parent_path)
    parent_segment = _file_id_segment(parent_id)

    try:
        response = await request(
            config,
            "POST",
            f"/data/{parent_segment}/children",
            params={"name": file_name, "type": "REG"},
            body=content.encode("utf-8"),
            additional_headers={"Content-Type": "application/octet-stream"},
        )
        return response["body"]["fileId"]
    except OnedataApiError as e:
        if e.errno == "eexist":
            raise FileExistsError(f"File {path} already exists") from e

        logger.error(f"Error creating file {path}: {e}")
        raise e


_HOW_TO_NAME_A_DIRECTORY = "Give the new directory's full path, /<space_name>/<path_to_directory>."


def _directory_path(path: str) -> tuple[str, str]:
    """(the space's name, the new directory's path under its root), or a ValueError saying what
    to give. The path must be absolute: a relative one left unresolved (a client with an empty
    list of roots) would otherwise read its first component as a space. The space component and
    every later one must be a name: an empty, `.` or `..` component is refused, since in the
    request path the HTTP client resolves `a/../b` to `b`, which would make the directory
    somewhere else."""
    if not path.startswith("/"):
        raise ValueError(
            "Paths must start with '/': use /<space_name>/<path_to_directory> "
            f"(got {_shown(path)})."
        )
    body = path[1:].rstrip("/")
    if not body:
        raise ValueError(f"The path is empty. {_HOW_TO_NAME_A_DIRECTORY}")
    space_name, *rest = body.split("/")
    if space_name in ("", ".", ".."):
        raise ValueError(
            f"{_shown(path)} does not start with a space name. {_HOW_TO_NAME_A_DIRECTORY}"
        )
    if not rest:
        raise ValueError(
            f"{_shown(path)} is the root directory of a space, which always exists. "
            f"{_HOW_TO_NAME_A_DIRECTORY}"
        )
    if any(part in ("", ".", "..") for part in rest):
        raise ValueError(
            f"{_shown(path)} has an empty, '.' or '..' component. {_HOW_TO_NAME_A_DIRECTORY}"
        )
    return space_name, "/".join(rest)


async def _type_at(path: str) -> str | None:
    """What is at `path` (REG, DIR or SYMLNK), for the "already exists" error; None if unknown."""
    try:
        found = await get_file_attributes(path, attributes=["type"])
    except Exception:  # noqa: BLE001 - then nothing is said about what is there
        return None
    kind = found.get("type") if isinstance(found, dict) else None
    return kind if isinstance(kind, str) else None


_EXISTING = {
    "DIR": "a directory, so nothing was created",
    "REG": "a regular file, so no directory was created",
    "SYMLNK": "a symbolic link, so no directory was created",
}

# The most components, counted from the space down, that `_regular_file_on` reads. Each costs
# up to two requests, on an error path only.
_MAX_COMPONENTS_READ = 8


async def _regular_file_on(path: str) -> str | None:
    """The first component of `path`, above its last, that is a regular file, as a path; None
    when there is none or it cannot be established. Onedata 25.0 answers a creation under a
    regular file with `eacces`, the same errno as a missing permission, so the errno alone does
    not say which it was.

    Walked from the space down, reading each component's type. It stops at the first one that is
    not a directory, since a missing component means nothing below it exists, and a failed read
    means nothing is established."""
    space_name, *rest = path[1:].rstrip("/").split("/")
    current = f"/{space_name}"
    for part in rest[:-1][:_MAX_COMPONENTS_READ]:
        current = f"{current}/{part}"
        kind = await _type_at(current)
        if kind == "REG":
            return current
        if kind != "DIR":
            return None
    return None


async def _create_directory_error(
    error: OnedataApiError, path: str, create_parents: bool
) -> Exception:
    """What a failed creation tells the caller, from what this provider established; the error
    itself when there is nothing to add.

    "Already exists" needs evidence: an `eexist` errno, or a 409 whose path then resolves. With
    a type read at the path, it says what is there. A 409 that nothing explains is a conflict,
    not an existing entry. A missing parent needs a structured not-found on a 4xx status (with
    create_parents false); a bare 404 gets the possible causes; a 5xx that mentions a missing
    item gets the uncertain hint (the provider failed; a parent may also not exist; whether
    anything was created is not known). Every errno or error id counts on a 4xx status only.

    `eacces` is also what 25.0 answers for a creation under a regular file (`enotdir` is not
    sent): the components of the path are then read, and one that is a regular file is named; the
    permission hint is given only when none is. A 403 or `eperm` is a permission refusal as it
    is, and nothing is read."""
    shown = _shown(path)
    eexist = _errno_is(error, "eexist")
    if eexist or _status(error) == 409:
        kind = await _type_at(path)
        if kind in _EXISTING:
            return FileExistsError(f"{shown} already exists as {_EXISTING[kind]}.")
        if eexist:
            return FileExistsError(
                f"Something already exists at {shown} (eexist), so no directory was created."
            )
        hint = (
            f"This provider refused to create {shown} with a conflict (409) and did not say "
            "why, so no directory was created. Check what is at the path."
        )
    elif _errno_is(error, "enotdir"):
        hint = (
            f"A component of {shown} above the new directory is not a directory, so nothing "
            "was created."
        )
    elif _errno_is(error, "eacces") and (component := await _regular_file_on(path)):
        hint = (
            f"A component of the path, {_shown(component)}, is a regular file, so nothing can "
            "be created under it."
        )
    elif _is_forbidden(error):
        hint = (
            f"Not allowed to create {shown}: the token may be read-only or have no write "
            "access in this space. Calling again will not help."
        )
    elif _is_client_error(error) and error.error_id == "spaceNotSupportedBy":
        hint = (
            f"This provider does not support the space of {shown}, so nothing was created "
            f"here; the space may still exist.{await _visible_spaces()}"
        )
    elif _is_not_found(error) and not create_parents:
        hint = (
            f"The parent directory of {shown} does not exist, so nothing was created. Create "
            "it first, or call again with create_parents set to true."
        )
    elif _is_not_found(error):
        hint = (
            f"This provider reports part of {shown} missing although create_parents is true, "
            "so nothing was created. Check the path."
        )
    elif _is_bare_404(error):
        hint = _bare_404_hint(shown, "a parent may not exist") + " Nothing was created."
    elif _is_unconfirmed_not_found(error):
        hint = _unconfirmed_hint(shown, error, creating=True)
    else:
        return error
    return _with_hint(hint, error)


async def create_directory(path: str, *, create_parents: bool = True) -> dict[str, Any]:
    """Create a directory at `/<space>/<path>`; returns `{fileId, path}`.

    `PUT /data/{space root id}/path/{relative}?type=DIR&create_parents=..` (swagger
    `create_file_at_path`). Every component of the relative path is percent-encoded, so a `?`
    or `#` in a name is a character of the name. A path that cannot name a new directory is
    refused before any request; a failure says what this provider established
    (`_create_directory_error`)."""
    space_name, relative = _directory_path(path)
    config = get_oneprovider_config()
    try:
        root_id = await _lookup(f"/{space_name}", "create a directory in")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"No space named {_shown(space_name)} is visible at this provider, so nothing was "
            f"created.{await _visible_spaces()}"
        ) from None
    try:
        response = await request(
            config,
            "PUT",
            f"/data/{_file_id_segment(root_id)}/path/{path_segments(relative, 'path')}",
            params={"type": "DIR", "create_parents": create_parents},
        )
    except OnedataApiError as e:
        logger.debug(f"Error creating directory {path}: {e}")
        replacement = await _create_directory_error(e, path, create_parents)
        if replacement is e:
            raise
        raise replacement from e
    return {"fileId": response["body"]["fileId"], "path": path}


def _looks_like_directory_intent(path: str, content: str) -> bool:
    """Return True if a create_file call looks like a mis-aimed mkdir.

    Heuristic: empty content AND the basename has no recognizable file
    extension. Covers the V3/GLM A4 trap of
    ``create_file(path="archive", content="", create_parents=True)`` —
    which would silently create a regular file at that path and then
    every subsequent ``archive/x`` op fails with ``enotdir``.

    See research/empirical-mcp-server-findings.md M-11.
    """
    if content != "":
        return False
    basename = path.rstrip("/").rsplit("/", 1)[-1]
    if not basename:
        return False
    # Common no-extension files (Makefile, README, Dockerfile etc.) are
    # legitimate empty-file creations. We're conservative — only flag when
    # there's NO dot at all in the basename. Hidden files (.gitignore)
    # have a leading dot and pass this check; that's deliberate (an empty
    # .gitignore is a valid intentional file).
    return "." not in basename


async def delete_file(file_id_or_path: str) -> None:
    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    # Encoded and guarded: an id with a `?` or `#` would otherwise delete the object named by
    # the part before it (issue #44).
    await request(config, "DELETE", f"/data/{_file_id_segment(file_id)}")


async def get_file_metadata(file_id_or_path: str, metadata_types: list[str]) -> dict[str, Any]:
    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    allowed_types = {"json", "rdf", "xattrs"}

    invalid_types = sorted(set(metadata_types) - allowed_types)
    if invalid_types:
        supported = ", ".join(sorted(allowed_types))
        invalid = ", ".join(invalid_types)
        raise ValueError(f"Unsupported metadata type(s): {invalid}. Supported types: {supported}")

    file_segment = _file_id_segment(file_id)
    result: dict[str, Any] = {}
    for metadata_type in metadata_types:
        try:
            additional_headers = (
                {"Accept": "application/rdf+xml"} if metadata_type == "rdf" else None
            )
            response = await request(
                config,
                "GET",
                f"/data/{file_segment}/metadata/{metadata_type}",
                additional_headers=additional_headers,
            )
            result[metadata_type] = response["body"]
        except OnedataApiError as e:
            if e.errno == "enodata":
                result[metadata_type] = None
                continue
            raise

    return result


async def set_file_xattrs(file_id_or_path: str, xattrs: dict[str, str] | str) -> None:
    """Merge extended attributes (string values only); omitted keys unchanged.

    Convenience wrapper over ``set_file_metadata(..., "xattrs", ...)``. Accepts a
    dict or a JSON-object string (some models pass the payload pre-serialized).
    """
    payload = json.dumps(xattrs) if isinstance(xattrs, dict) else xattrs
    await set_file_metadata(file_id_or_path, "xattrs", payload)


async def set_file_metadata(
    file_id_or_path: str, metadata_type: str, metadata: str | bytes
) -> None:
    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    file_segment = _file_id_segment(file_id)
    type_segment = id_segment(metadata_type, "metadata type")
    additional_headers = (
        {"Content-Type": "application/rdf+xml"}
        if metadata_type == "rdf"
        else {"Content-Type": "application/json"}
    )
    return await request(
        config,
        "PUT",
        f"/data/{file_segment}/metadata/{type_segment}",
        body=metadata if isinstance(metadata, bytes) else metadata.encode("utf-8"),
        additional_headers=additional_headers,
    )


async def get_file_distribution(file_id_or_path: str) -> dict[str, Any]:
    """Return per-provider, per-storage block distribution for a file.

    Endpoint: GET /api/v3/oneprovider/data/{file_id}/distribution
    operationId: get_data_distribution (Onedata 25.0).

    Symbolic links are not supported (server returns 400). Directories
    are returned as DataDirDistribution; regular files as DataRegDistribution.

    Response shape (DataDistribution swagger):
        {
          "type": "REG" | "DIR",
          "distributionPerProvider": {
            "<providerId>": {
              "success": true,
              "logicalSize": N,
              "locationsPerStorageBackend": {
                "<storageId>": {"success": true, "location": "..."}
                  | {"success": false, "error": {...}}
              },
              "distributionPerStorageBackend": {
                "<storageId>": {"success": true, "blocks": [[start,len],...], "physicalSize": N}
              }
            } | {"success": false, "error": {...}}
          }
        }
    """
    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    response = await request(config, "GET", f"/data/{_file_id_segment(file_id)}/distribution")
    return response["body"]


async def move_file(
    src_path: str,
    dst_path: str,
) -> str:
    """Atomically move/rename a file or directory within a single space.

    Implementation: CDMI move (Cloud Data Management Interface, standardised
    spec, but not exposed in Onedata's `/api/v3/oneprovider/` swagger).
    Endpoint: PUT https://{oneprovider}/cdmi/{dst_space}/{dst_path}
    Headers:  X-Auth-Token, X-CDMI-Specification-Version: 1.1.1,
              Content-Type: application/cdmi-object
    Body:     {"move": "<src_space>/<src_path>"}

    See `design/01-move-file-strategy.md` for the alternatives considered.
    Source: ported from `OnedataFileRESTClient.move()` in the official
    onedatafilerestclient package (commit 6887661).

    Args:
        src_path: source logical path in the form '/<space>/<path>'.
        dst_path: destination logical path in the form '/<space>/<path>'.
                  src_space MUST equal dst_space — Onedata 25.0 CDMI move
                  rejects cross-space operations.

    Returns: fileId of the moved entity at the destination.

    Raises:
        ValueError: if src_path or dst_path is not in '/<space>/<path>' form,
            or if the spaces differ (cross-space move not supported).
    """
    src_space, src_inner = _parse_space_path(src_path, "src_path")
    dst_space, dst_inner = _parse_space_path(dst_path, "dst_path")
    if src_space != dst_space:
        raise ValueError(
            f"Cross-space moves are not supported by Onedata 25.0 CDMI: "
            f"src_space={src_space!r}, dst_space={dst_space!r}. "
            f"Use download_file + create_file + delete_file instead, "
            f"accepting the loss of atomicity."
        )

    config = get_oneprovider_config()
    # CDMI is at the host root (/cdmi/...), not under /api/v3/oneprovider/.
    cdmi_base = config.base_url.removesuffix("/api/v3/oneprovider")
    # Guarded: the HTTP client resolves a `..` component, so a destination `/a/../../b` would
    # be sent to another space.
    encoded_dst_inner = path_segments(dst_inner, "dst_path", hint=_HOW_TO_NAME_A_FILE_PATH)
    cdmi_url = f"{cdmi_base}/cdmi/{id_segment(dst_space, 'space name')}/{encoded_dst_inner}"

    headers = dict(config.auth_headers)
    headers["X-CDMI-Specification-Version"] = "1.1.1"
    headers["Content-Type"] = "application/cdmi-object"
    # Onedata 25.0 CDMI returns 406 without an explicit Accept matching the
    # request body type. Verified live 2026-05-01; without Accept: 406, with
    # `application/cdmi-object`: 201. See research/empirical-onedata-25.0-findings.md.
    headers["Accept"] = "application/cdmi-object"

    body = {"move": f"{src_space}/{src_inner}"}

    # Errors name the CDMI path, never `cdmi_url`, which holds the provider's host.
    cdmi_path = cdmi_url.removeprefix(cdmi_base)
    try:
        async with httpx.AsyncClient(verify=config.verify_ssl) as client:
            response = await client.put(cdmi_url, headers=headers, json=body)
    except Exception as exc:
        raise transport_failure(exc, config, "PUT", cdmi_path, label="CDMI move failed") from None

    if not 200 <= response.status_code < 300:  # a redirect is not a completed move
        raise OnedataApiError(
            http_failure_message(
                config,
                "PUT",
                cdmi_path,
                response.status_code,
                decode_response_body(response),
                label="CDMI move failed",
            ),
            response={"status_code": response.status_code, "body": response.text},
        )

    # CDMI move returns no fileId; fetch it via the standard lookup.
    return await get_file_id(dst_path)


def _parse_space_path(path: str, arg_name: str) -> tuple[str, str]:
    """Parse '/<space>/<inner_path>' into (space, inner_path)."""
    if not path.startswith("/"):
        raise ValueError(
            f"{arg_name} must be a logical path starting with '/' "
            f"(form: /<space>/<path>); got {path!r}"
        )
    parts = path.lstrip("/").split("/", 1)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"{arg_name} must be a logical path of form /<space>/<path>; got {path!r}")
    return parts[0], parts[1]
