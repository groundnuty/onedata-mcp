"""
Transfers API client for Oneprovider.

Endpoints (Onedata 25.0, oneprovider-swagger):
- GET /spaces/{sid}/transfers      -> get_all_transfers (returns IDs only)
- GET /transfers/{tid}             -> get_transfer (per-transfer detail)

Note: list_space_transfers requires the `space_view_transfers` privilege on
the calling token. State filter values are: 'waiting', 'ongoing', 'ended'
(NOT 'completed' / 'failed' / 'all' — paper spec §3.8 was wrong).

The list endpoint returns IDs only; agents needing source/dest/state/bytes
must follow up with get_transfer per id. We expose both as separate tools
to keep the cost of the list call bounded.
"""

from __future__ import annotations

from typing import Any, Literal

from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.utils import request

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
