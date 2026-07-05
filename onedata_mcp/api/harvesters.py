import asyncio
import json
from typing import Any

from onedata_mcp.config import get_onezone_config
from onedata_mcp.utils import request


def coerce_harvesters_index_query(query: dict[str, Any] | str) -> dict[str, Any]:
    """Tolerate a harvester query passed as a JSON-object string.

    Onezone expects a JSON object; weaker models often pass the whole query
    pre-serialized as a string. Accepting either is a pure tolerance
    improvement (a dict still works unchanged). Adapted from upstream c474a93.
    """
    if isinstance(query, dict):
        return query
    if isinstance(query, str):
        stripped = query.strip()
        if not stripped:
            raise ValueError("harvester query string is empty")
        try:
            parsed: Any = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise ValueError(f"harvester query is not valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise TypeError("harvester query JSON must deserialize to an object")
        return parsed
    raise TypeError(f"harvester query must be dict or str, got {type(query).__name__}")


async def get_user_harvester(harvester_id: str) -> dict[str, Any]:
    config = get_onezone_config()
    response = await request(config, "GET", f"/user/harvesters/{harvester_id}")
    return response["body"]


async def list_harvester_indices(harvester_id: str) -> list[str]:
    config = get_onezone_config()
    response = await request(config, "GET", f"/harvesters/{harvester_id}/indices")
    return response["body"]["indices"]


async def get_harvester_index(harvester_id: str, index_id: str) -> dict[str, Any]:
    config = get_onezone_config()
    response = await request(config, "GET", f"/harvesters/{harvester_id}/indices/{index_id}")
    return response["body"]


def _without_schema(index_details: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in index_details.items() if key != "schema"}


async def list_user_harvesters() -> list[dict[str, Any]]:
    config = get_onezone_config()
    response = await request(config, "GET", "/user/harvesters")
    harvester_ids = response["body"]["harvesters"]

    async def _fetch_harvester_with_indices(harvester_id: str) -> dict[str, Any]:
        harvester = await get_user_harvester(harvester_id)
        index_ids = await list_harvester_indices(harvester_id)
        index_details = await asyncio.gather(
            *(get_harvester_index(harvester_id, index_id) for index_id in index_ids)
        )
        harvester["indices"] = [_without_schema(index) for index in index_details]
        return harvester

    return await asyncio.gather(*(_fetch_harvester_with_indices(hid) for hid in harvester_ids))


async def get_harvester_index_schema(harvester_id: str, index_id: str) -> dict[str, Any]:
    return await get_harvester_index(harvester_id, index_id)


async def query_harvester_index(
    harvester_id: str, index_id: str, query: dict[str, Any] | str
) -> dict[str, Any]:
    config = get_onezone_config()
    body = coerce_harvesters_index_query(query)
    response = await request(
        config,
        "POST",
        f"/harvesters/{harvester_id}/indices/{index_id}/query",
        json_body=body,
    )
    return response["body"]
