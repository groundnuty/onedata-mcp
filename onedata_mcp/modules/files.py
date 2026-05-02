import re
from typing import Any, Optional
from urllib.parse import urlparse

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from onedata_mcp.api.files import (
    create_file,
    delete_file,
    download_file,
    get_file_attributes,
    get_file_distribution,
    get_file_id,
    get_file_metadata,
    grep_file_content,
    list_children,
    list_files_recursively,
    move_file,
    set_file_metadata,
)

ONEDATA_FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{20,}$")


def _is_probable_file_id(value: str) -> bool:
    return bool(ONEDATA_FILE_ID_PATTERN.fullmatch(value))


def _root_uri_to_path(root_uri: str) -> str | None:
    parsed = urlparse(root_uri)
    if parsed.scheme == "onedata":
        space_id = parsed.netloc.strip()
        if not space_id:
            return None
        return f"/{space_id}"

    path = parsed.path.strip()
    if not path:
        return None
    return path if path.startswith("/") else f"/{path}"


async def _resolve_with_mcp_root(path_or_id: str, ctx: Optional[Context]) -> str:
    if not path_or_id or path_or_id.startswith("/"):
        return path_or_id

    # Preserve explicit file identifiers; only relative paths are root-resolved.
    if _is_probable_file_id(path_or_id):
        return path_or_id

    if ctx is None:
        return path_or_id

    roots = await ctx.list_roots()
    if not roots:
        return path_or_id

    root_path = _root_uri_to_path(str(roots[0].uri))
    if not root_path:
        return path_or_id

    return f"{root_path.rstrip('/')}/{path_or_id.lstrip('/')}"


def register_module(mcp: FastMCP) -> None:
    """Register onedata files module tools and prompts with the MCP server."""

    @mcp.tool(name="get_file_id", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_file_id(
        path: str = Field(description="Path to the file in format /<space_name>/<path_to_file>"),
        ctx: Optional[Context] = None,
    ) -> str:
        """
        Get the file id for a given path.
        """
        path = await _resolve_with_mcp_root(path, ctx)
        return await get_file_id(path)

    @mcp.tool(name="get_file_attributes", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_file_attributes(
        file_id_or_path: str = Field(
            description="File id or path to the file in format /<space_name>/<path_to_file>"
        ),
        attributes: Optional[list[str]] = Field(
            default=None,
            description="""
            (Optional) List of attribute names to request from Oneprovider. 
            Allowed values:
            - Identity/location: fileId, parentFileId, index, name, conflictingName, path, type
            - Permissions/access: activePermissionsType, posixPermissions, acl
            - Ownership/provider/shares: ownerUserId, originProviderId, directShareIds
            - Links: hardlinkCount, symlinkValue
            - Display ids: displayUid, displayGid
            - Timestamps/size: creationTime, atime, mtime, ctime, size
            - Replication: isFullyReplicatedLocally, localReplicationRate
            - Metadata: hasCustomMetadata, hasJsonMetadata, jsonMetadata, xattr.*
            """,
        ),
        ctx: Optional[Context] = None,
    ) -> dict[str, Any]:
        """
        Get attributes for a file id or a logical path.
        """
        file_id_or_path = await _resolve_with_mcp_root(file_id_or_path, ctx)
        return await get_file_attributes(file_id_or_path, attributes=attributes)

    @mcp.tool(name="list_children", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_list_children(
        parent_id_or_path: str = Field(
            description="File id or path to the parent file in format /<space_name>/<path_to_file>"
        ),
        *,
        attributes: Optional[list[str]] = Field(
            default=None,
            description="""
            (Optional) List of attribute names to request from Oneprovider. 
            Use the same allowed values as in get_file_attributes.
            """,
        ),
        limit: int = Field(
            default=10,
            ge=1,
            le=100,
            description="Maximum number of children",
        ),
        offset: int = Field(
            default=0,
            description="Starting offset of the children",
        ),
        token: Optional[str] = Field(
            default=None,
            description="Token to continue listing from the next page of results",
        ),
        ctx: Optional[Context] = None,
    ) -> dict[str, Any]:
        """
        List children (files and directories) of a given file id or path.

        """
        parent_id_or_path = await _resolve_with_mcp_root(parent_id_or_path, ctx)
        return await list_children(
            parent_id_or_path, attributes=attributes, limit=limit, offset=offset, token=token
        )

    @mcp.tool(name="list_files_recursively", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_list_files_recursively(
        parent_id_or_path: str = Field(
            description="File id or path to the parent file in format /<space_name>/<path_to_file>"
        ),
        *,
        attributes: Optional[list[str]] = Field(
            default=None,
            description="""
            (Optional) List of attribute names to request from Oneprovider. 
            Use the same allowed values as in get_file_attributes.
            """,
        ),
        limit: int = Field(
            default=10,
            ge=1,
            le=100,
            description="Maximum number of files to return",
        ),
        token: Optional[str] = Field(
            default=None,
            description="Token to continue listing from the next page of results",
        ),
        start_after: Optional[str] = Field(
            default=None,
            description=(
                "Start listing from first file path lexicographically greater than this value"
            ),
        ),
        prefix: Optional[str] = Field(
            default=None,
            description="Only files with paths starting with this value are listed",
        ),
        ctx: Optional[Context] = None,
    ) -> dict[str, Any]:
        """
        Recursively list non-directory files under a given file id or path.

        ## Path-format normalization

        Onedata's recursive listing returns paths *relative to the
        listed parent* (e.g. 'msg00.txt' when listing `/space/a2/inbox/`).
        This wrapper rewrites each entry's `path` to be absolute
        (e.g. '/space/a2/inbox/msg00.txt') for consistency with every
        other tool in the surface and with how briefs phrase paths.
        See research/empirical-mcp-server-findings.md M-6.

        Only applied when `parent_id_or_path` was passed as a path
        (starts with '/'). When called with a fileId, the relative
        paths are passed through unchanged — the caller chose the id
        form and presumably knows what to compose with.
        """
        original_arg = parent_id_or_path
        parent_id_or_path = await _resolve_with_mcp_root(parent_id_or_path, ctx)
        result = await list_files_recursively(
            parent_id_or_path,
            attributes=attributes,
            limit=limit,
            token=token,
            start_after=start_after,
            prefix=prefix,
        )
        # Enrich each returned file with absolute path when the caller
        # gave us a path to begin with. We use the caller's original
        # absolute path (after MCP root resolution) as the prefix.
        if isinstance(original_arg, str) and original_arg.startswith("/"):
            prefix_path = original_arg.rstrip("/")
            files = result.get("files")
            if isinstance(files, list):
                for entry in files:
                    if isinstance(entry, dict):
                        rel = entry.get("path")
                        if isinstance(rel, str) and not rel.startswith("/"):
                            entry["path"] = f"{prefix_path}/{rel.lstrip('/')}"
        return result

    @mcp.tool(name="download_file", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_download_file(
        file_id_or_path: str = Field(
            description="File id or path to the file in format /<space_name>/<path_to_file>"
        ),
        ctx: Optional[Context] = None,
    ) -> bytes:
        """
        Download the content of a given file id or path.
        """
        file_id_or_path = await _resolve_with_mcp_root(file_id_or_path, ctx)
        return await download_file(file_id_or_path)

    @mcp.tool(name="grep_file_content", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_grep_file_content(
        file_id_or_path: str = Field(
            description="File id or path to the file in format /<space_name>/<path_to_file>"
        ),
        pattern: str = Field(
            description="Pattern to search for in the file content",
        ),
        ctx: Optional[Context] = None,
    ) -> str:
        """
        Search for a pattern in the content of a given file id or path.
        """
        file_id_or_path = await _resolve_with_mcp_root(file_id_or_path, ctx)
        return await grep_file_content(file_id_or_path, pattern)

    @mcp.tool(name="create_file", annotations=ToolAnnotations(destructiveHint=True))
    async def mcp_create_file(
        path: str = Field(description="Path to the file in format /<space_name>/<path_to_file>"),
        content: str = Field(
            description="Content of the file as a string",
        ),
        create_parents: bool = Field(
            default=False,
            description="Create missing directories under the space root via Oneprovider path API",
        ),
        ctx: Optional[Context] = None,
    ) -> str:
        """
        Create a new file with the given content.

        Returns the file id of the created file.
        """
        path = await _resolve_with_mcp_root(path, ctx)
        return await create_file(path, content, create_parents=create_parents)

    @mcp.tool(name="delete_file", annotations=ToolAnnotations(destructiveHint=True))
    async def mcp_delete_file(
        file_id_or_path: str = Field(
            description="File id or path to the file in format /<space_name>/<path_to_file>"
        ),
        ctx: Optional[Context] = None,
    ) -> None:
        """
        Delete a given file or directory (recursively) by id or path.
        """
        file_id_or_path = await _resolve_with_mcp_root(file_id_or_path, ctx)
        return await delete_file(file_id_or_path)

    @mcp.tool(name="get_file_metadata", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_file_metadata(
        file_id_or_path: str = Field(
            description="File id or path to the file in format /<space_name>/<path_to_file>"
        ),
        metadata_types: list[str] = Field(
            description="List of metadata types to get",
            default=["json", "rdf", "xattrs"],
        ),
        ctx: Optional[Context] = None,
    ) -> dict[str, Any]:
        """
        Get metadata for a given file id or path by metadata types.

        For many metadata values from a single request, use
        get_file_attributes with metadata-related attributes.
        """
        file_id_or_path = await _resolve_with_mcp_root(file_id_or_path, ctx)
        return await get_file_metadata(file_id_or_path, metadata_types)

    @mcp.tool(name="set_file_metadata", annotations=ToolAnnotations(destructiveHint=True))
    async def mcp_set_file_metadata(
        file_id_or_path: str = Field(
            description="File id or path to the file in format /<space_name>/<path_to_file>"
        ),
        metadata_type: str = Field(
            description=(
                "Metadata type — accepts 'json' (custom JSON metadata, the "
                "common case), 'rdf' (RDF/XML), or 'xattrs' (POSIX xattrs). "
                "Common alias 'custom' is mapped to 'json'. Anything else is "
                "rejected at tool-call time with a clear list of valid values."
            ),
        ),
        metadata: str = Field(
            description="Metadata content to set",
        ),
        ctx: Optional[Context] = None,
    ) -> None:
        """
        Set metadata for a given file id or path by metadata type.

        ## Validation

        `metadata_type` accepts one of {'json', 'rdf', 'xattrs'} or the
        alias 'custom' (mapped to 'json'). Any other value raises
        ValueError immediately — Onedata's REST returns an opaque 404
        for invalid types, which makes the failure look like a missing
        file. See research/empirical-mcp-server-findings.md M-4.
        """
        valid = ("json", "rdf", "xattrs")
        normalized = "json" if metadata_type == "custom" else metadata_type
        if normalized not in valid:
            raise ValueError(
                f"metadata_type must be one of {valid} (or alias 'custom' "
                f"→ 'json'). Got: {metadata_type!r}"
            )
        file_id_or_path = await _resolve_with_mcp_root(file_id_or_path, ctx)
        return await set_file_metadata(file_id_or_path, normalized, metadata)

    @mcp.tool(name="get_file_distribution", annotations=ToolAnnotations(readOnlyHint=True))
    async def mcp_get_file_distribution(
        file_id_or_path: str = Field(
            description="File id or path to the file in format /<space_name>/<path_to_file>"
        ),
        ctx: Optional[Context] = None,
    ) -> dict[str, Any]:
        """Return per-provider, per-storage-backend block distribution.

        For each provider supporting the file's space, reports which blocks
        are physically held (the steady state is partial replication).
        Symbolic links are not supported (server returns 400). For
        directories, returns aggregate distribution.

        ## Provider-name enrichment

        The Oneprovider REST returns the distribution keyed by
        providerId hex strings only. This wrapper performs an inline
        join with `list_user_spaces` to add a `providerName` field on
        each provider entry, so callers don't have to chain a
        secondary lookup. See research/empirical-mcp-server-findings.md
        M-5.
        """
        import asyncio as _asyncio

        from onedata_mcp.api.spaces import get_space_providers, list_user_spaces

        file_id_or_path = await _resolve_with_mcp_root(file_id_or_path, ctx)
        result = await get_file_distribution(file_id_or_path)

        # Build providerId → providerName lookup. The user's spaces
        # are bounded (typically a handful), and `get_space_providers`
        # for each is cheap. Run in parallel; ignore individual
        # failures since enrichment is best-effort.
        try:
            spaces = await list_user_spaces()
        except Exception:  # noqa: BLE001
            spaces = []

        async def _fetch(sid: str) -> dict | None:
            try:
                return await get_space_providers(sid)
            except Exception:  # noqa: BLE001
                return None

        space_ids = [s.get("spaceId") for s in spaces if isinstance(s.get("spaceId"), str)]
        details = await _asyncio.gather(*(_fetch(sid) for sid in space_ids))

        pid_to_name: dict[str, str] = {}
        for detail in details:
            if not isinstance(detail, dict):
                continue
            for entry in detail.get("providers") or []:
                if isinstance(entry, dict):
                    pid = entry.get("providerId")
                    name = entry.get("providerName")
                    if isinstance(pid, str) and isinstance(name, str):
                        pid_to_name.setdefault(pid, name)

        per_provider = result.get("distributionPerProvider")
        if isinstance(per_provider, dict):
            for pid, info in per_provider.items():
                if isinstance(info, dict) and pid in pid_to_name:
                    info["providerName"] = pid_to_name[pid]
        return result

    @mcp.tool(name="move_file", annotations=ToolAnnotations(destructiveHint=True))
    async def mcp_move_file(
        src_path: str = Field(
            description="Source logical path /<space_name>/<path_to_file> (intra-space only)"
        ),
        dst_path: str = Field(
            description=(
                "Destination logical path /<space_name>/<path_to_file> "
                "(must be in same space as src)"
            )
        ),
        ctx: Optional[Context] = None,
    ) -> str:
        """Atomically move/rename a file or directory within a single space.

        Implementation uses CDMI (PUT /cdmi/{dst_space}/{dst_path} with
        body {"move": "<src_space>/<src_path>"}) — same protocol the
        Onedata Python client wraps. **Intra-space only.** Cross-space
        moves return a clear ValueError directing the agent to compose
        download_file + create_file + delete_file (loses atomicity).

        Returns the fileId of the moved entity at the destination.
        """
        src_path = await _resolve_with_mcp_root(src_path, ctx)
        dst_path = await _resolve_with_mcp_root(dst_path, ctx)
        return await move_file(src_path, dst_path)
