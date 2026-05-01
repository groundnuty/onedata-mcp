import asyncio
from typing import Any, TypedDict

from onedata_mcp.config import get_oneprovider_config, get_onezone_config
from onedata_mcp.utils import request


class SpacesListResponse(TypedDict):
    spaces: list[str]


class MarketplaceListItem(TypedDict):
    spaceId: str
    index: str


class MarketplaceListResponse(TypedDict):
    spaces: list[MarketplaceListItem]
    isLast: bool
    nextPageToken: str | None


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
