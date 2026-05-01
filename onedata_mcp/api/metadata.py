"""
Metadata query API.

Composes existing primitives (`list_files_recursively` + `get_file_metadata`)
into a depth- and result-bounded recursive predicate evaluator over JSON
metadata. No harvester / OpenSearch dependency — see
`design/02-query-by-metadata-no-harvester.md` for the rationale.
"""

from __future__ import annotations

from typing import Any

from onedata_mcp.api.files import get_file_metadata, list_files_recursively

DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_RESULTS = 50
MAX_FILES_VISITED = 1000  # hard cap regardless of caller-supplied bounds
LIST_PAGE_LIMIT = 100  # max per request to list_files_recursively


def _parse_predicate(predicate: str) -> list[tuple[str, str | None]]:
    """Parse 'key=value & key=* & ...' into [(key, value_or_None), ...].

    None marks 'key=*' (match-any). Whitespace around clauses is allowed.
    Empty clauses raise ValueError.
    """
    if not predicate or not predicate.strip():
        raise ValueError("predicate must be a non-empty string")

    parsed: list[tuple[str, str | None]] = []
    for raw in predicate.split("&"):
        clause = raw.strip()
        if not clause:
            raise ValueError(f"empty predicate clause in: {predicate!r}")
        if "=" not in clause:
            raise ValueError(
                f"predicate clause {clause!r} must be 'key=value' or 'key=*' "
                f"(use single '=', not '==')"
            )
        key, _, value = clause.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"predicate clause {clause!r} has empty key")
        parsed.append((key, None if value == "*" else value))
    return parsed


def _depth_of(file_path: str, root_path: str) -> int:
    """Number of path segments below root_path. /space/a/b given /space → 2."""
    rel = file_path[len(root_path) :].strip("/")
    if not rel:
        return 0
    return len(rel.split("/"))


def _matches(metadata: Any, clauses: list[tuple[str, str | None]]) -> tuple[bool, list[str]]:
    """Return (all_clauses_matched, list_of_matched_keys).

    Only top-level keys of a JSON object are considered. Non-object metadata
    (None, str, list) never matches a key=value clause.
    """
    if not isinstance(metadata, dict):
        return False, []
    matched: list[str] = []
    for key, expected in clauses:
        if key not in metadata:
            return False, []
        if expected is not None and str(metadata[key]) != expected:
            return False, []
        matched.append(key)
    return True, matched


async def query_by_metadata(
    space: str,
    predicate: str,
    *,
    path: str = "/",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """Find files in a space whose JSON metadata matches a predicate.

    Strategy: recursive `list_files_recursively` from `space + path`, then
    per-file `get_file_metadata(['json'])`, with client-side filtering.
    Bounded by `max_depth` (path depth from `path`), `max_results` (cap on
    returned matches), and the module-level `MAX_FILES_VISITED` (hard cap).

    Args:
        space: Onedata space name.
        predicate: 'key=value' or 'key=*' clauses, '&'-joined for AND.
            Examples: 'pipeline_stage=anonymised',
            'pipeline_stage=raw & reviewed=*'.
            Note: only single '=' is accepted (Python-trained-LLMs typing
            'key==value' will see a clear ValueError).
        path: subtree under the space to search. Default '/'.
        max_depth: path-depth cap below `path`.
        max_results: cap on returned matches.

    Returns:
        {
          "matches": [{"path": str, "fileId": str, "matched_keys": [str]}],
          "truncated": bool,    # True if any cap fired
          "files_visited": int, # actual count
        }
    """
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")
    if max_results < 1:
        raise ValueError("max_results must be >= 1")

    clauses = _parse_predicate(predicate)
    root_path = f"/{space.strip('/')}{path}" if path != "/" else f"/{space.strip('/')}"

    matches: list[dict[str, Any]] = []
    files_visited = 0
    truncated = False
    next_token: str | None = None

    while files_visited < MAX_FILES_VISITED and len(matches) < max_results:
        page_limit = min(
            LIST_PAGE_LIMIT,
            MAX_FILES_VISITED - files_visited,
        )
        page = await list_files_recursively(
            root_path,
            attributes=["fileId", "path"],
            limit=page_limit,
            token=next_token,
        )
        files = page.get("files", [])
        if not files:
            break

        for entry in files:
            files_visited += 1
            file_path = entry.get("path", "")
            if max_depth and _depth_of(file_path, root_path) > max_depth:
                continue

            file_id = entry.get("fileId")
            if not file_id:
                continue

            metadata_payload = await get_file_metadata(file_id, ["json"])
            json_meta = metadata_payload.get("json")
            ok, matched_keys = _matches(json_meta, clauses)
            if ok:
                matches.append(
                    {
                        "path": file_path,
                        "fileId": file_id,
                        "matched_keys": matched_keys,
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    break

        next_token = page.get("nextPageToken")
        if not next_token or page.get("isLast"):
            break

    if files_visited >= MAX_FILES_VISITED:
        truncated = True

    return {
        "matches": matches,
        "truncated": truncated,
        "files_visited": files_visited,
    }
