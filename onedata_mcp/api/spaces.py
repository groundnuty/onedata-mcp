import asyncio
from typing import Any, Literal, TypedDict

from onedata_mcp.config import get_oneprovider_config, get_onezone_config
from onedata_mcp.utils import request

DatasetState = Literal["attached", "detached"]


class SpacesListResponse(TypedDict):
    spaces: list[str]


class MarketplaceListItem(TypedDict):
    spaceId: str
    index: str


class MarketplaceListResponse(TypedDict):
    spaces: list[MarketplaceListItem]
    isLast: bool
    nextPageToken: str | None


async def resolve_space_id_or_name(value: str) -> str:
    """Polymorphic resolver: return a hex spaceId for either a hex
    spaceId or a human-readable space name.

    Per research/empirical-mcp-server-findings.md M-3, MCP tools that
    take `space_id` previously rejected names with a 403 from the
    Onedata REST. Accepting either preserves the simpler-surface
    principle without forcing the agent to chain a name→id lookup.

    Resolution rules:
    1. If `value` looks like a hex spaceId (≥30 hex chars + a `ch`
       suffix marker, the Onedata 25.0 ID shape), return as-is.
    2. Otherwise, list user spaces and match by `name` (preferred) or
       `spaceId` field equality.
    3. Raise ValueError listing the available names if no match.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("space identifier must be a non-empty string")
    # Heuristic: hex spaceId in Onedata 25.0 is ~38 chars with a `ch`
    # separator near the tail (e.g. '9742830720c0ef94496dad1d96595736ch776e').
    is_likely_id = (
        len(value) >= 30
        and "ch" in value
        and all(c in "0123456789abcdef" for c in value.replace("ch", ""))
    )
    if is_likely_id:
        return value

    spaces = await list_user_spaces()
    for s in spaces:
        if s.get("name") == value or s.get("spaceId") == value:
            sid = s.get("spaceId")
            if isinstance(sid, str):
                return sid
    available = sorted(s.get("name", "?") for s in spaces if s.get("name"))
    raise ValueError(f"Space {value!r} not found. Available names: {available}")


async def list_space_datasets(
    space_id_or_name: str,
    *,
    state: DatasetState = "attached",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Top-level datasets in a space.

    Endpoint: GET /api/v3/oneprovider/spaces/{sid}/datasets (oneprovider).
    `state`: 'attached' follows the current file layout; 'detached' is the
    hierarchy frozen at detachment time. Adapted from upstream 63c21b5.
    """
    if state not in ("attached", "detached"):
        raise ValueError("state must be 'attached' or 'detached'")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    space_id = await resolve_space_id_or_name(space_id_or_name)
    config = get_oneprovider_config()
    response = await request(
        config,
        "GET",
        f"/spaces/{space_id}/datasets",
        params={"state": state, "limit": limit, "offset": offset},
    )
    body = response["body"]
    return body if isinstance(body, dict) else {"datasets": []}


async def list_user_spaces() -> list[dict[str, Any]]:
    config = get_onezone_config()
    response = await request(config, "GET", "/spaces")
    space_ids = SpacesListResponse(**response["body"])["spaces"]

    details = await asyncio.gather(*(get_space_details(space_id) for space_id in space_ids))
    return [
        {
            "tags": space.get("tags"),
            "description": space.get("description"),
            "spaceId": space.get("spaceId"),
            "providers": space.get("providers"),
            "organizationName": space.get("organizationName"),
            "name": space.get("name"),
            "creationTime": space.get("creationTime"),
        }
        for space in details
    ]


async def get_space_details(space_id: str) -> dict[str, Any]:
    config = get_onezone_config()
    response = await request(config, "GET", f"/spaces/{space_id}")
    return response["body"]


async def get_space_providers(space_id: str) -> dict[str, Any]:
    """Return the providers supporting a space, from the Oneprovider side.

    Endpoint: GET /api/v3/oneprovider/spaces/{sid}  (operation: get_space)
    Source of truth for which providers actually serve this space.

    Response shape (per oneprovider Space definition, Onedata 25.0):
        {
          "name": "<space name>",
          "spaceId": "<id>",
          "providers": [
            {"providerId": "<id>", "providerName": "<name>"},
            ...
          ],
          "fileId": "<root dir id>",
          "dirId": "<root dir id>",
          "trashDirId": "<id>",
          "archivesDirId": "<id>"
        }

    Note: this endpoint does NOT include geographic / storage-class
    attributes per provider. For richer attributes, follow up with the
    onezone /providers/{id} endpoint per providerId. The benchmark's
    `list_space_providers` MCP tool wraps this two-call enrichment.
    """
    config = get_oneprovider_config()
    response = await request(config, "GET", f"/spaces/{space_id}")
    return response["body"]


async def get_marketplace_space_details(space_id: str) -> dict[str, Any]:
    """Get details for a space advertised in the marketplace."""
    config = get_onezone_config()
    response = await request(config, "GET", f"/spaces/marketplace/{space_id}")
    return response["body"]


async def list_marketplace_spaces(
    *,
    tags: list[str] | None = None,
    limit: int = 20,
    token: str | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """
    List spaces advertised in the Marketplace with detailed information.

    Returns pagination metadata (`isLast`, `nextPageToken`) and a `spaces` list
    enriched with details fetched from the marketplace details endpoint.
    """
    if limit < 1 or limit > 50:
        raise ValueError("Parameter 'limit' must be between 1 and 50")

    request_body: dict[str, Any] = {"limit": limit, "offset": offset}
    if tags:
        request_body["tags"] = tags
    if token:
        request_body["token"] = token

    config = get_onezone_config()
    response = await request(config, "POST", "/spaces/marketplace/list", json_body=request_body)
    listing = MarketplaceListResponse(**response["body"])

    details = await asyncio.gather(
        *(get_marketplace_space_details(space["spaceId"]) for space in listing["spaces"])
    )

    detailed_spaces = [
        {
            **space_details,
            "spaceId": listed_space["spaceId"],
            "index": space_details.get("index", listed_space["index"]),
        }
        for listed_space, space_details in zip(listing["spaces"], details, strict=True)
    ]

    return {
        "spaces": detailed_spaces,
        "isLast": listing["isLast"],
        "nextPageToken": listing["nextPageToken"],
    }
