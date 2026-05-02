from asyncio.log import logger
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import httpx

from onedata_mcp.api.spaces import list_user_spaces
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.utils import OnedataApiError, OnedataInvalidSpaceError, request

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


async def get_file_attributes(
    file_id_or_path: str,
    *,
    attributes: Iterable[str] | None = DEFAULT_FILE_ATTRIBUTE_KEYS,
) -> dict[str, Any]:
    config = get_oneprovider_config()
    requested_attributes = tuple[str, ...](attributes or DEFAULT_FILE_ATTRIBUTE_KEYS)

    file_id = await _normalize_path_to_file_id(file_id_or_path)
    response = await request(
        config,
        "GET",
        f"/data/{file_id}",
        json_body=({"attributes": list(requested_attributes)} if requested_attributes else None),
    )
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
    config = get_oneprovider_config()
    parent_id = await _normalize_path_to_file_id(parent_id_or_path)
    requested_attributes = tuple[str, ...](attributes or DEFAULT_FILE_ATTRIBUTE_KEYS)
    _reject_deprecated_attributes(requested_attributes)
    request_body: dict[str, Any] = {"limit": limit, "offset": offset}
    if token is not None:
        request_body["token"] = token
    if requested_attributes:
        request_body["attributes"] = list(requested_attributes)

    try:
        response = await request(
            config,
            "GET",
            f"/data/{parent_id}/children",
            json_body=request_body,
        )
    except OnedataApiError as e:
        await _raise_invalid_space_error_if_needed(e, parent_id_or_path)
        raise
    return _strip_deprecated_fields_in_list(response["body"], "children")


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
            f"/data/{parent_id}/files",
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

    async with httpx.AsyncClient(
        base_url=config.base_url, headers=headers, verify=config.verify_ssl
    ) as client:
        response = await client.get(f"/data/{file_id}/content")

    if response.is_error:
        raise RuntimeError(
            f"Onedata API request failed: GET /data/{file_id}/content "
            f"(status={response.status_code}) - {response.text}"
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
        root_id = await get_file_id(f"/{space_name}")
        encoded_path = quote(relative_path, safe="/")
        try:
            response = await request(
                config,
                "PUT",
                f"/data/{root_id}/path/{encoded_path}",
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

    try:
        response = await request(
            config,
            "POST",
            f"/data/{parent_id}/children",
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


async def create_directory(path: str, *, create_parents: bool = True) -> dict[str, Any]:
    """Create a directory at the given /<space>/<path> location.

    Wraps ``PUT /data/{space_id}/path/{relative}?type=DIR&create_parents=...``
    (Oneprovider 25.0 — see oneprovider-swagger paths/data/id/path.yaml,
    operationId ``create_file_at_path``). Returns ``{fileId, path}``.

    Raises ``FileExistsError`` if the path already exists. Raises
    ``ValueError`` if ``path`` does not include a relative path under a
    space root (i.e. the form must be ``/<space>/<dir...>``).

    See research/empirical-mcp-server-findings.md M-11.
    """
    config = get_oneprovider_config()
    normalized = path.strip("/")
    if "/" not in normalized:
        raise ValueError(
            "path must be /<space_name>/<path_to_directory>; got: "
            f"{path!r}. To target the space root, use the existing "
            f"namespace tools instead — the root always exists."
        )
    space_name, relative_path = normalized.split("/", 1)
    if not relative_path:
        raise ValueError("path must include a directory path under the space")
    root_id = await get_file_id(f"/{space_name}")
    encoded_path = quote(relative_path, safe="/")
    try:
        response = await request(
            config,
            "PUT",
            f"/data/{root_id}/path/{encoded_path}",
            params={"type": "DIR", "create_parents": create_parents},
        )
    except OnedataApiError as e:
        if e.errno == "eexist":
            raise FileExistsError(f"Directory {path} already exists") from e
        logger.error(f"Error creating directory {path}: {e}")
        raise
    file_id = response["body"]["fileId"]
    return {"fileId": file_id, "path": path}


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
    await request(config, "DELETE", f"/data/{file_id}")


async def get_file_metadata(file_id_or_path: str, metadata_types: list[str]) -> dict[str, Any]:
    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    allowed_types = {"json", "rdf", "xattrs"}

    invalid_types = sorted(set(metadata_types) - allowed_types)
    if invalid_types:
        supported = ", ".join(sorted(allowed_types))
        invalid = ", ".join(invalid_types)
        raise ValueError(f"Unsupported metadata type(s): {invalid}. Supported types: {supported}")

    result: dict[str, Any] = {}
    for metadata_type in metadata_types:
        try:
            additional_headers = (
                {"Accept": "application/rdf+xml"} if metadata_type == "rdf" else None
            )
            response = await request(
                config,
                "GET",
                f"/data/{file_id}/metadata/{metadata_type}",
                additional_headers=additional_headers,
            )
            result[metadata_type] = response["body"]
        except OnedataApiError as e:
            if e.errno == "enodata":
                result[metadata_type] = None
                continue
            raise

    return result


async def set_file_metadata(
    file_id_or_path: str, metadata_type: str, metadata: str | bytes
) -> None:
    config = get_oneprovider_config()
    file_id = await _normalize_path_to_file_id(file_id_or_path)
    additional_headers = (
        {"Content-Type": "application/rdf+xml"}
        if metadata_type == "rdf"
        else {"Content-Type": "application/json"}
    )
    return await request(
        config,
        "PUT",
        f"/data/{file_id}/metadata/{metadata_type}",
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
    response = await request(config, "GET", f"/data/{file_id}/distribution")
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
    encoded_dst_inner = quote(dst_inner, safe="/")
    cdmi_url = f"{cdmi_base}/cdmi/{quote(dst_space, safe='')}/{encoded_dst_inner}"

    headers = dict(config.auth_headers)
    headers["X-CDMI-Specification-Version"] = "1.1.1"
    headers["Content-Type"] = "application/cdmi-object"
    # Onedata 25.0 CDMI returns 406 without an explicit Accept matching the
    # request body type. Verified live 2026-05-01; without Accept: 406, with
    # `application/cdmi-object`: 201. See research/empirical-onedata-25.0-findings.md.
    headers["Accept"] = "application/cdmi-object"

    body = {"move": f"{src_space}/{src_inner}"}

    async with httpx.AsyncClient(verify=config.verify_ssl) as client:
        response = await client.put(cdmi_url, headers=headers, json=body)

    if response.is_error:
        raise OnedataApiError(
            f"CDMI move failed: PUT {cdmi_url} (status={response.status_code}) - {response.text}",
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
