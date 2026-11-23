"""MCP tool registration for the readiness tools: check_readiness, ensure_readiness."""

from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from onedata_mcp.api.readiness import (
    MAX_DATE_RANGES_PER_ITEM,
    MAX_DEADLINE_S,
    MAX_EXTENT_ITEMS,
    MAX_FILES_PER_ITEM,
)
from onedata_mcp.api.readiness import check_readiness as api_check_readiness
from onedata_mcp.api.readiness import ensure_readiness as api_ensure_readiness

# How both tools take the data. Each description says it in full: either tool may be offered
# without the other, so neither names the other (design/10, gate 2).
EXTENT_SENTENCE = (
    "Describe the data as `extent`: for each dataset (a space name such as 'my-dataset', or a "
    "directory path inside it), optionally the files or date ranges you need; dates are read "
    "from the YYYYMMDD in each file name."
)

CHECK_DESCRIPTION = (
    "Check whether the data your analysis needs are ready to read at your provider: "
    "complete there, with all writes finished. "
    + EXTENT_SENTENCE
    + " The answer is ready, or not ready and why: being_written (the owner is still writing "
    "them, so their bytes are incomplete), at_other_site (complete at another provider, not "
    "yet at yours), in_transfer (on their way to your provider), or unreachable (only at a "
    "provider that is down). Read-only."
)

ENSURE_DESCRIPTION = (
    "Make the data your analysis needs ready at your provider, and keep them there. "
    + EXTENT_SENTENCE
    + " `deadline_s` is how long you can wait. The system chooses where to copy from and "
    "waits for unfinished writes and transfers. Each file is held by a QoS requirement that "
    "keeps it at your provider until that requirement is removed by its id; the answer lists "
    "the ids. The answer is ready (the data are complete and readable at your provider now, "
    "and held there), or not ready with the reason. One call waits at most about 2.5 "
    "minutes; if the answer is not ready because data are being written or in transfer, "
    "call it again."
)

# A concrete `extent`, so that a model need not guess its shape (one guessed
# `{space, paths, date_from, date_to}`). Each item shows one of the two dataset forms.
EXTENT_EXAMPLE = (
    '[{"dataset": "my-dataset", "files": ["data_20180801.nc"]}, '
    '{"dataset": "/my-dataset/sub/dir", '
    '"date_ranges": [{"start": "2018-06-01", "end": "2018-08-31"}]}]'
)
EXTENT_FIELD_DESCRIPTION = "The data you need: one item per dataset. Example: " + EXTENT_EXAMPLE


class DateRange(BaseModel):
    # An unknown field is refused, not dropped.
    model_config = ConfigDict(extra="forbid")

    start: str = Field(
        description="First day, inclusive, as YYYY-MM-DD",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    end: str = Field(
        description="Last day, inclusive, as YYYY-MM-DD",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


class ExtentItem(BaseModel):
    # An unknown field is refused, not dropped: an item left with no `files` and no
    # `date_ranges` would mean the whole dataset, which ensure_readiness would then hold.
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(
        description=(
            "A space name such as 'my-dataset', or an absolute directory path inside a "
            "space such as '/my-dataset/sub/dir'"
        )
    )
    files: list[str] | None = Field(
        default=None,
        max_length=MAX_FILES_PER_ITEM,
        description=(
            "File names or absolute paths inside the dataset. With neither files nor "
            "date_ranges, the whole dataset is meant"
        ),
    )
    date_ranges: list[DateRange] | None = Field(
        default=None,
        max_length=MAX_DATE_RANGES_PER_ITEM,
        description=(
            "Inclusive day ranges. They select the files whose name carries a YYYYMMDD "
            "date inside any range"
        ),
    )


def _as_dicts(extent: list[Any]) -> list[dict[str, Any]]:
    return [
        item.model_dump(exclude_none=True) if isinstance(item, BaseModel) else dict(item)
        for item in extent
    ]


def register_module(mcp: FastMCP) -> None:
    """Register the readiness tools with the MCP server."""

    @mcp.tool(
        name="check_readiness",
        description=CHECK_DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def mcp_check_readiness(
        extent: list[ExtentItem] = Field(
            description=EXTENT_FIELD_DESCRIPTION,
            min_length=1,
            max_length=MAX_EXTENT_ITEMS,
        ),
    ) -> dict[str, Any]:
        return await api_check_readiness(_as_dicts(extent))

    @mcp.tool(
        name="ensure_readiness",
        description=ENSURE_DESCRIPTION,
        annotations=ToolAnnotations(destructiveHint=False),
    )
    async def mcp_ensure_readiness(
        extent: list[ExtentItem] = Field(
            description=EXTENT_FIELD_DESCRIPTION,
            min_length=1,
            max_length=MAX_EXTENT_ITEMS,
        ),
        deadline_s: int = Field(
            ge=1,
            le=MAX_DEADLINE_S,
            description="How many seconds in total you are willing to wait",
        ),
    ) -> dict[str, Any]:
        return await api_ensure_readiness(_as_dicts(extent), deadline_s)
