"""MCP tool registration for the readiness tools: check_readiness, ensure_readiness."""

from collections.abc import Mapping
from typing import Annotated, Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from onedata_mcp.api.readiness import (
    MAX_DATE_RANGES_PER_ITEM,
    MAX_DEADLINE_S,
    MAX_EXTENT_ITEMS,
    MAX_FILES_PER_ITEM,
    check_extent_item,
)
from onedata_mcp.api.readiness import check_readiness as api_check_readiness
from onedata_mcp.api.readiness import ensure_readiness as api_ensure_readiness
from onedata_mcp.tool_maturity import is_offered

# How both tools take the data. Each description says it in full: either tool may be offered
# without the other, so neither names the other (design/10, gate 2).
EXTENT_SENTENCE = (
    "Describe the data as `extent`: for each dataset (a space, by its name such as "
    "'my-dataset' or its id, or a directory path inside it), optionally the files or the date "
    "ranges you need (with both, every file that either selects); dates are read from the "
    "YYYYMMDD in each file name."
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


def _no_default(schema: dict[str, Any]) -> None:
    """A selector may be left out, never given as null: the schema shows no `default: null`."""
    schema.pop("default", None)


class ExtentItem(BaseModel):
    # An unknown field is refused, not dropped: an item left with no `files` and no
    # `date_ranges` would mean the whole dataset, which ensure_readiness would then hold. For
    # the same reason a selector given as null or as an empty list is refused (minItems 1, and
    # no null): left out, it means the whole dataset; empty, it would select nothing.
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(
        description=(
            "A space's name such as 'my-dataset' or its id, or an absolute directory path "
            "inside a space such as '/my-dataset/sub/dir'"
        )
    )
    files: list[str] = Field(
        default=None,
        min_length=1,
        max_length=MAX_FILES_PER_ITEM,
        description=(
            "File names, or paths (inside the dataset, or absolute). A name that several "
            "files share is refused: give such a file by its path. Leave files and "
            "date_ranges out for the whole dataset"
        ),
        json_schema_extra=_no_default,
    )
    date_ranges: list[DateRange] = Field(
        default=None,
        min_length=1,
        max_length=MAX_DATE_RANGES_PER_ITEM,
        description=(
            "Inclusive day ranges. They select the files whose name carries a YYYYMMDD "
            "date inside any range"
        ),
        json_schema_extra=_no_default,
    )


def _hint_first(value: Any) -> Any:
    """Before the schema refuses an item with its own generic words ("Extra inputs are not
    permitted", "Input should be a valid list"), refuse it with the API's hint: the fields an
    item has, and that an explicitly empty selector selects nothing (design/17, gate 1)."""
    if isinstance(value, list):
        for index, raw in enumerate(value):
            if isinstance(raw, Mapping):
                check_extent_item(raw, f"extent[{index}]")
    return value


Extent = Annotated[list[ExtentItem], BeforeValidator(_hint_first)]


def _as_dicts(extent: list[Any]) -> list[dict[str, Any]]:
    """Each item as the caller gave it: a field left out stays out (never sent as null)."""
    return [
        item.model_dump(exclude_unset=True) if isinstance(item, BaseModel) else dict(item)
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
        extent: Extent = Field(
            description=EXTENT_FIELD_DESCRIPTION,
            min_length=1,
            max_length=MAX_EXTENT_ITEMS,
        ),
    ) -> dict[str, Any]:
        # The answer points to ensure_readiness only where it is offered: a read-only token or a
        # selection may remove it (design/10, gate 2).
        offered = await is_offered(mcp, "ensure_readiness")
        return await api_check_readiness(_as_dicts(extent), ensure_offered=offered)

    @mcp.tool(
        name="ensure_readiness",
        description=ENSURE_DESCRIPTION,
        annotations=ToolAnnotations(destructiveHint=False),
    )
    async def mcp_ensure_readiness(
        extent: Extent = Field(
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
