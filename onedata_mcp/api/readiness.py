"""Readiness: is the data an analysis declares complete and readable at this provider?

Two operations share one idea. `check_readiness` reads state and says whether the
declared data are ready here, or why not. `ensure_readiness` places a QoS rule on each
file (the system then copies it here from wherever it can and protects the replica from
eviction), waits, and answers the same way. Spec: design/07-readiness-tools.md.

Words used below:

- *local provider*: the Oneprovider this server talks to.
- *size*: the largest of the listing's `size`, every provider's `logicalSize` or
  `virtualSize`, and the largest block end (offset + length) at any readable provider. A
  file with no size from any source is `unchecked`: nothing can be judged covered. A
  listing `size` of exactly 0, with no other size evidence and every provider entry
  readable, is an empty file: there is nothing to read, and any provider that lists it
  covers it. One unreadable or malformed provider and it is not confirmed empty.
- *cover*: a provider covers a file when the union of its blocks over all its storages
  covers `[0, size)` (`GET /data/{id}/distribution`).
- *writes complete*: some provider other than the local one covers the file (or the file
  originates here and the local provider covers it). Local blocks alone are not trusted:
  a read of an unwritten range might materialise zero-filled local blocks (UNVERIFIED).
- *ready (per file)*: the local provider covers the file AND writes are complete. A
  transfer's `replicationStatus: completed` or a QoS `fulfilled` is never taken as proof.

Every pure rule (`name_date`, `covers`, `parse_distribution`, `writes_complete`,
`classify`) is a plain function with no I/O; the rest is REST plumbing on the existing
helpers in `api/files.py`, `api/qos.py`, `api/spaces.py` and `api/transfers.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from onedata_mcp.api.files import (
    _file_id_parts,
    get_file_attributes,
    get_file_distribution,
    get_file_id,
    list_files_recursively,
)
from onedata_mcp.api.qos import add_qos_requirement, get_qos_requirement
from onedata_mcp.api.transfers import get_transfer, list_space_transfers
from onedata_mcp.config import get_oneprovider_config, get_onezone_config
from onedata_mcp.utils import (
    OnedataApiError,
    OnedataError,
    OnedataInvalidSpaceError,
    id_segment,
    request,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits and timings. Module constants, so tests can monkeypatch them.
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 5.0
CHECK_BUDGET_S = 60.0  # check_readiness ends within this
# One ensure_readiness call works and waits at most this (and at most its `deadline_s`). With
# the grace, the whole call ends within 170 s, under the ~180 s a common MCP client allows a
# call (design/18). The env below may lower the wait, never raise it: the description says "at
# most about 2.5 minutes".
MAX_WAIT_DEFAULT_S = 150
MAX_WAIT_CAP_S = 150
MAX_WAIT_ENV = "ONEDATA_MCP_ENSURE_MAX_WAIT_S"
# The final read-back of an ensure call (files and holds), after its budget: at the default cap
# the whole call ends within 150 + 20 s. Scaled down with the cap (`ensure_timing`).
ENSURE_GRACE_S = 20.0
MAX_DEADLINE_S = 86_400
MAX_EXTENT_ITEMS = 20
MAX_FILES = 20_000  # files an extent may resolve to, and a dataset listing may hold
MAX_FILES_PER_ITEM = 20_000  # names in one item's `files`
MAX_DATE_RANGES_PER_ITEM = 400
LIST_PAGE_LIMIT = 1000
CHECK_CONCURRENCY = 16
PLACE_CONCURRENCY = 8
MAX_TRANSFERS = 50
MAX_EXAMPLES = 3
MAX_UNRESOLVED_SHOWN = 10
MAX_HOLD_IDS_SHOWN = 100  # ids of the holds an answer lists; the rest are counted
# How the ids not listed are found (names no tool: design/10, gate 2). Not "each file's QoS
# summary": that lists every requirement on the file, other people's and inherited ones too.
HOLD_IDS_ELSEWHERE = (
    "Not every id is listed here (the first ones, in the files' path order, are). To list the "
    "others, call again with fewer files: a hold this server placed is read back and listed, "
    "not placed again."
)
# The pointer to the other tool, given only where that tool is offered (decision 4, design/17).
ENSURE_POINTER = "ensure_readiness can wait for {them} and place {them} at your provider."

# The fixed expression form is the tag that marks this tool's placement rules.
HOLD_EXPRESSION = "providerId={provider_id} & anyStorage"
HOLD_EXPRESSION_PLAIN = "providerId={provider_id}"

LISTING_ATTRIBUTES = (
    "fileId",
    "path",
    "name",
    "type",
    "size",
    "isFullyReplicatedLocally",
    "localReplicationRate",
    # Not in the spec's list, but `writes_complete` needs the origin provider.
    "originProviderId",
)
POLL_ATTRIBUTES = ("isFullyReplicatedLocally", "size")

# A transfer brings data here only while it is in one of these states: the swagger's
# transferStatus values scheduled / enqueued / replicating, and `active`, which is what the
# replicationStatus phase (and this repo's older docs) call a running transfer.
MOVING_STATES = frozenset({"scheduled", "enqueued", "active", "replicating"})

# Why a file is not ready, most severe first. Ties for "dominant" go to the earlier one.
REASONS = ("unreachable", "being_written", "in_transfer", "at_other_site")

# One clock and one sleeper, so tests can run a whole wait on a fake clock.
_monotonic = time.monotonic
_sleep = asyncio.sleep

# Process-level state (reset by `reset_state`, which tests call).
_local_provider_id: str | None = None
_rules: dict[str, str] = {}  # file id -> id of the placement rule this process created
_hold_plain = False  # True once Onedata rejected the primary expression form


def reset_state() -> None:
    """Forget the cached provider id, the placement rules and the expression form."""
    global _local_provider_id, _hold_plain
    _local_provider_id = None
    _hold_plain = False
    _rules.clear()
    _placing.clear()


def max_wait_s() -> float:
    """How long one ensure call waits at most: the env value, else 150 s, never above 150 s."""
    raw = os.environ.get(MAX_WAIT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            if value > MAX_WAIT_CAP_S:
                logger.warning(
                    "%s above %s s is cut to %s s", MAX_WAIT_ENV, MAX_WAIT_CAP_S, MAX_WAIT_CAP_S
                )
            return min(value, float(MAX_WAIT_CAP_S))
        logger.warning("%s is not a positive number; using %s s", MAX_WAIT_ENV, MAX_WAIT_DEFAULT_S)
    return float(MAX_WAIT_DEFAULT_S)


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(?<!\d)\d{8}(?!\d)")


def name_date(name: str) -> date | None:
    """The date a file name carries: its first run of exactly 8 digits that is a real YYYYMMDD."""
    for match in _DATE_RE.finditer(name):
        digits = match.group()
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        except ValueError:
            continue
    return None


def _block(block: Any) -> tuple[int, int] | None:
    """`[offset, length]`: a list or tuple of exactly two non-negative ints, length above 0."""
    if not isinstance(block, list | tuple) or len(block) != 2:
        return None
    offset, length = block
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (offset, length)):
        return None
    return (offset, length) if length > 0 else None


def _merge(blocks: Iterable[Any], size: int) -> list[tuple[int, int]]:
    """The union of the valid blocks, clipped to `[0, size)`, as sorted spans."""
    spans: list[tuple[int, int]] = []
    for block in blocks:
        parsed = _block(block)
        if parsed is None:
            continue
        offset, length = parsed
        end = min(offset + length, size)
        if end > offset:
            spans.append((offset, end))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def covered_bytes(blocks: Iterable[Any], size: int) -> int:
    """How many bytes of `[0, size)` the blocks hold."""
    return sum(end - start for start, end in _merge(blocks, size))


def covers(blocks: Iterable[Any], size: int) -> bool:
    """Whether the blocks cover `[0, size)`. A size-0 span is covered by any block list."""
    return size <= 0 or covered_bytes(blocks, size) >= size


@dataclass
class Distribution:
    """What `GET /data/{id}/distribution` says, reduced to what readiness needs.

    `size` is the largest of the size the listing gave, every `logicalSize` or `virtualSize`
    reported, and the largest block end at a readable provider: a smaller, stale size must
    not let a partial replica look complete. It is 0 when no source gave a positive size.
    Then either the file is known to be empty (`empty`: the listing said exactly 0, nothing
    contradicts it, and every provider's entry is readable) and every provider that lists it
    covers it, or its size is unknown and nothing covers it (`known` is False).
    """

    size: int
    covering: frozenset[str]  # providers whose blocks cover [0, size)
    holders: frozenset[str]  # providers holding at least one block
    failed: frozenset[str]  # providers whose entry is unreadable: their blocks are unknown
    covered: dict[str, int] = field(default_factory=dict)  # bytes of [0, size) per provider
    empty: bool = False

    @property
    def known(self) -> bool:
        """Whether the file can be judged: it has a positive size, or is known to be empty."""
        return self.size > 0 or self.empty


def parse_distribution(body: Any, known_size: int | None = None) -> Distribution:
    """Reduce a distribution answer. `known_size` is the size from the file's listing, or
    None when the listing gave none.

    A provider is unreadable for this file (`failed`) when its entry says `success: false`,
    has no storage map, or carries anything but valid blocks: a block is accepted only as a
    list or tuple of exactly two non-negative ints with a length above 0.
    """
    per_provider = body.get("distributionPerProvider") if isinstance(body, dict) else None
    if not isinstance(per_provider, dict):
        per_provider = {}

    listing_size = (
        known_size
        if isinstance(known_size, int) and not isinstance(known_size, bool) and known_size >= 0
        else None
    )
    size = listing_size or 0
    sized = listing_size is not None  # some source has given a size (0 included)
    failed: set[str] = set()
    blocks_of: dict[str, tuple[list[tuple[int, int]], bool]] = {}
    for provider_id, entry in per_provider.items():
        # A failed entry carries an error and no other field (swagger DataRegDistribution).
        if not isinstance(entry, dict) or entry.get("success") is False:
            failed.add(provider_id)
            continue
        for size_field in ("logicalSize", "virtualSize"):  # 25.0 may say either
            reported = entry.get(size_field)
            if isinstance(reported, int) and not isinstance(reported, bool):
                size = max(size, reported)
                sized = True
        storages = entry.get("distributionPerStorageBackend")
        if not isinstance(storages, dict):
            failed.add(provider_id)
            continue
        blocks: list[tuple[int, int]] = []
        storage_failed = False
        malformed = False
        for storage in storages.values():
            if not isinstance(storage, dict) or storage.get("success") is False:
                storage_failed = True
                continue
            raw = storage.get("blocks")
            parsed = [_block(b) for b in raw] if isinstance(raw, list) else [None]
            if any(b is None for b in parsed):
                malformed = True
                break
            blocks.extend(b for b in parsed if b is not None)
        if malformed:
            failed.add(provider_id)
            continue
        blocks_of[provider_id] = (blocks, storage_failed)

    # A block that ends at some offset proves the file is at least that long. It raises a
    # size some source gave; it does not make one up where no source gave any.
    if sized:
        size = max([size, *(o + n for blocks, _ in blocks_of.values() for o, n in blocks)])
    all_readable = bool(blocks_of) and not failed and not any(sf for _, sf in blocks_of.values())
    empty = listing_size == 0 and size == 0 and all_readable

    covering: set[str] = set()
    holders: set[str] = set()
    covered: dict[str, int] = {}
    if empty:  # nothing to read: every provider that lists the file covers it
        covered = dict.fromkeys(blocks_of, 0)
        covering = set(blocks_of)
    if size > 0:  # with no positive size nothing is covered
        for provider_id, (blocks, storage_failed) in blocks_of.items():
            covered[provider_id] = covered_bytes(blocks, size)
            if covered[provider_id] > 0:
                holders.add(provider_id)
            if covers(blocks, size):
                covering.add(provider_id)
            elif storage_failed:
                failed.add(provider_id)  # a storage we could not read may hold the rest
    return Distribution(
        size, frozenset(covering), frozenset(holders), frozenset(failed), covered, empty
    )


def writes_complete(dist: Distribution, local_id: str, origin_id: str | None) -> bool:
    """Another provider covers the file, or it originates here and the local provider covers it."""
    if dist.covering - {local_id}:
        return True
    return origin_id == local_id and local_id in dist.covering


def is_ready(dist: Distribution, local_id: str, origin_id: str | None, flag: bool | None) -> bool:
    """The local provider covers the file and writes are complete.

    `flag` is `isFullyReplicatedLocally`: false means the file cannot be ready.
    """
    return (
        flag is not False
        and local_id in dist.covering
        and writes_complete(dist, local_id, origin_id)
    )


def classify(
    dist: Distribution,
    local_id: str,
    origin_id: str | None,
    *,
    flag: bool | None = None,
    offline: Iterable[str] = (),
    moving: bool = False,
) -> str | None:
    """None for a ready file, else the first matching reason (see REASONS).

    `offline`: providers Onezone says are not online. A provider whose distribution entry
    failed counts as down as well. `moving`: something is bringing the file here (an
    ongoing transfer to the local provider, or an unsatisfied placement rule).
    """
    if is_ready(dist, local_id, origin_id, flag):
        return None
    down = frozenset(offline) | dist.failed
    if not (dist.covering - {local_id} - down):
        # No online provider besides this one has the whole file. A down provider that
        # holds blocks of it makes it unreachable. A failed entry lists no blocks, so it
        # counts as holding some only when no online provider is known to hold any.
        remote_holders = dist.holders - {local_id}
        known_down = remote_holders & down
        unknown_down = dist.failed - {local_id}
        if known_down or (unknown_down and not (remote_holders - down)):
            return "unreachable"
    if not writes_complete(dist, local_id, origin_id):
        return "being_written"
    if moving:
        return "in_transfer"
    return "at_other_site"


def _within(path: str, ancestor: str) -> bool:
    ancestor = ancestor.rstrip("/")
    return path == ancestor or path.startswith(ancestor + "/")


# ---------------------------------------------------------------------------
# Extent: declare, then resolve to files
# ---------------------------------------------------------------------------


@dataclass
class FileInfo:
    file_id: str
    path: str  # absolute: /<space>/...
    space_id: str
    size: int = 0  # the largest size seen so far: listing, distribution, attributes
    size_known: bool = False  # some source gave a size (0 included)
    cover_size: int = 0  # the size the file was confirmed ready at
    flag: bool | None = None  # isFullyReplicatedLocally
    origin: str | None = None
    local_bytes: int = 0
    dist: Distribution | None = None
    state: str | None = None  # "ready", a reason in REASONS, "unchecked", or None (not looked at)

    @property
    def basename(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def known_size(self) -> int | None:
        return self.size if self.size_known else None


@dataclass
class _Item:
    dataset: str  # as declared
    space: str  # the space segment as declared: a space's name or its id
    # absolute directory path, /<space>[/dir...], with one leading or trailing slash dropped
    # (`my-dataset/sub` is read as /my-dataset/sub when its first segment is a space)
    root: str
    files: list[str]  # as declared: matched exactly, never trimmed or normalised
    ranges: list[tuple[date, date]]


_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_ITEM_KEYS = ("dataset", "files", "date_ranges")
_RANGE_KEYS = ("start", "end")


def _parse_day(value: Any, where: str) -> date:
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        raise ValueError(f"{where} must be a date in YYYY-MM-DD form, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{where} {value!r} is not a calendar date: give a real day, as YYYY-MM-DD"
        ) from None


def _unknown_keys(raw: Mapping[str, Any], known: tuple[str, ...], where: str) -> None:
    """A field the item does not have is refused: dropped silently, it would widen the item
    (an item with no `files` and no `date_ranges` is the whole dataset)."""
    unknown = sorted(str(key) for key in raw if key not in known)
    if unknown:
        raise ValueError(
            f"{where} has no field {', '.join(repr(k) for k in unknown)}: "
            f"its fields are {', '.join(known)}"
        )


_BAD_COMPONENTS = ("", ".", "..")


def _dataset_root(dataset: str, where: str) -> tuple[str, str]:
    """(the dataset's directory as an absolute path, its space segment).

    One leading and one trailing slash are dropped (`/my-dataset/sub/` is `/my-dataset/sub`,
    and `my-dataset/sub` is read as `/my-dataset/sub` when its first segment is a space). An
    empty, `.` or `..` component is refused, and so is a second leading or trailing slash: the
    provider would resolve such a path to another directory (`/my-dataset/..` is above every
    space)."""
    body = dataset[1:] if dataset.startswith("/") else dataset
    body = body[:-1] if body.endswith("/") else body
    parts = body.split("/")
    if any(part in _BAD_COMPONENTS for part in parts):
        space = parts[0] if parts[0] not in _BAD_COMPONENTS else "my-dataset"
        raise ValueError(
            f"{where}.dataset {dataset!r} has an empty, '.' or '..' component: give a space "
            f"name such as '{space}', or an absolute directory path inside it such as "
            f"'/{space}/sub/dir'"
        )
    return "/" + body, parts[0]


def _check_file_path(name: str, where: str) -> None:
    """A declared file path is matched exactly, so an empty, `.` or `..` component could only
    ever select nothing (or, read loosely, another file): it is refused, with how to give it."""
    parts = (name[1:] if name.startswith("/") else name).split("/")
    if "/" in name and any(part in _BAD_COMPONENTS for part in parts):
        raise ValueError(
            f"{where} {name!r} has an empty, '.' or '..' component: give the file's exact "
            "name, its path inside the dataset (such as 'sub/data_20180801.nc') or its "
            "absolute path, without them"
        )


# How an explicitly empty selector is refused: it selects no file, and read as left out it
# would mean the whole dataset (an empty search result passed on would hold every file).
_EMPTY_SELECTOR = {
    "files": "name the files you need",
    "date_ranges": "give at least one date range",
}


def _selector(raw: Mapping[str, Any], key: str, where: str) -> list[Any]:
    """`files` or `date_ranges`: [] when left out; refused when given as null or an empty list."""
    if key not in raw:
        return []
    value = raw[key]
    if value is None or (isinstance(value, list) and not value):
        what = "null" if value is None else "empty"
        raise ValueError(
            f"{where}.{key} is {what}, so it selects no file: {_EMPTY_SELECTOR[key]}, or leave "
            f"{key} out (an item with neither files nor date_ranges means its whole dataset)"
        )
    if not isinstance(value, list):
        raise ValueError(f"{where}.{key} must be a list")
    return value


def check_extent_item(raw: Mapping[str, Any], where: str) -> None:
    """What the MCP schema would refuse with a generic error, refused first with a hint: a field
    the item (or a date range) does not have, and a selector given as null or empty."""
    _unknown_keys(raw, _ITEM_KEYS, where)
    for key in ("files", "date_ranges"):
        _selector(raw, key, where)
    ranges = raw.get("date_ranges")
    for r_index, rng in enumerate(ranges if isinstance(ranges, list) else ()):
        if isinstance(rng, Mapping):
            _unknown_keys(rng, _RANGE_KEYS, f"{where}.date_ranges[{r_index}]")


def _normalise_extent(extent: Sequence[Mapping[str, Any]]) -> list[_Item]:
    """Validate the extent. Declared names and paths are kept exactly as given."""
    if not isinstance(extent, list | tuple) or not 1 <= len(extent) <= MAX_EXTENT_ITEMS:
        raise ValueError(f"extent must be a list of 1 to {MAX_EXTENT_ITEMS} items")
    items: list[_Item] = []
    named = 0
    for index, raw in enumerate(extent):
        where = f"extent[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{where} must be an object")
        _unknown_keys(raw, _ITEM_KEYS, where)
        dataset = raw.get("dataset")
        if not isinstance(dataset, str) or not dataset.strip("/"):
            raise ValueError(f"{where}.dataset must be a space name or a path inside a space")
        root, space = _dataset_root(dataset, where)
        files = _selector(raw, "files", where)
        if not all(isinstance(f, str) and f for f in files):
            raise ValueError(f"{where}.files must be a list of file names or paths")
        for f_index, name in enumerate(files):
            _check_file_path(name, f"{where}.files[{f_index}]")
        if len(files) > MAX_FILES_PER_ITEM:
            raise ValueError(f"{where}.files may name at most {MAX_FILES_PER_ITEM} files")
        raw_ranges = _selector(raw, "date_ranges", where)
        if len(raw_ranges) > MAX_DATE_RANGES_PER_ITEM:
            raise ValueError(
                f"{where}.date_ranges may hold at most {MAX_DATE_RANGES_PER_ITEM} ranges"
            )
        ranges: list[tuple[date, date]] = []
        for r_index, rng in enumerate(raw_ranges):
            r_where = f"{where}.date_ranges[{r_index}]"
            if not isinstance(rng, Mapping):
                raise ValueError(f"{r_where} must be an object with start and end")
            _unknown_keys(rng, _RANGE_KEYS, r_where)
            start = _parse_day(rng.get("start"), f"{r_where}.start")
            end = _parse_day(rng.get("end"), f"{r_where}.end")
            if start > end:
                raise ValueError(
                    f"{r_where}: start {start.isoformat()} is after end {end.isoformat()}; "
                    "give the earlier day as start"
                )
            ranges.append((start, end))
        # A name or a range declared twice is looked for once.
        unique = list(dict.fromkeys(files))
        named += len(unique)
        if named > MAX_FILES:
            raise ValueError(
                f"The extent names more than {MAX_FILES} files in all. "
                "Declare a smaller extent, or select files by date range."
            )
        items.append(_Item(dataset, space, root, unique, list(dict.fromkeys(ranges))))
    return items


def _absolute(root: str, path: str) -> str:
    """`path` under the directory `root`, unless it is absolute already."""
    return path if path.startswith("/") else f"{root.rstrip('/')}/{path}"


_SKIPPED: Any = object()  # a bounded task that did not start because time ran out


async def _bounded[T, R](
    limit: int,
    items: Sequence[T],
    fn: Callable[[T], Awaitable[R]],
    deadline: float | None = None,
) -> list[Any]:
    """Run `fn` over `items`, `limit` at a time; a task not started by `deadline` is _SKIPPED."""
    semaphore = asyncio.Semaphore(limit)

    async def run(item: T) -> Any:
        async with semaphore:
            if deadline is not None and _monotonic() >= deadline:
                return _SKIPPED
            return await fn(item)

    tasks = [asyncio.ensure_future(run(item)) for item in items]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        raise


@dataclass
class _Transfer:
    file_id: str | None
    path: str | None
    replicating: str | None
    status: str | None


@dataclass(frozen=True)
class _Space:
    """The one space an item's space segment names, chosen by its id or its name."""

    space_id: str
    name: str
    # The name is a path's first segment for this space only: no other listed space has it as
    # its name or its id. When it is not, no path is looked up by it (`_list_dataset`).
    name_unique: bool
    dir_id: str | None = None  # the space's own directory, when the provider's list gives it


class _AmbiguousSpace(ValueError):
    """An item's space segment names more than one space: refused, never guessed."""


def _dir_id_of(entry: Mapping[str, Any]) -> str | None:
    for key in ("dirId", "fileId"):  # the swagger's Space: dirId, and fileId (deprecated)
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _space_label(entry: Mapping[str, Any]) -> str:
    return f"the space '{entry.get('name')}' (id '{entry['spaceId']}')"


def _choose_space(space: str, entries: list[dict[str, Any]]) -> _Space:
    """The space `space` names among the provider's list: one whose id is exactly `space`, or
    whose name is. Refused (ValueError, with the candidates) when no space or more than one does:
    a name that is another space's id, or a name two spaces share. Pure."""
    matches = {e["spaceId"]: e for e in entries if e["spaceId"] == space or e.get("name") == space}
    if not matches:
        names = sorted(e["name"] for e in entries if isinstance(e.get("name"), str))
        # Only the spaces this provider supports for the token are listed: another space of
        # that name may still exist, so "not found" says where it was looked for.
        raise ValueError(
            f"your provider supports no space named {space!r} for this token "
            f"(the spaces it lists: {names})"
        )

    def names_one(value: object) -> bool:
        return sum(value in (e["spaceId"], e.get("name")) for e in entries) == 1

    if len(matches) > 1:
        shown = " and ".join(_space_label(e) for e in matches.values())
        forms = [
            repr(form)
            for e in matches.values()
            for form in (e.get("name"), e["spaceId"])
            if isinstance(form, str) and form and names_one(form)
        ]
        how = (
            f"Give {' or '.join(dict.fromkeys(forms))} instead: each names one space only."
            if forms
            else "No name or id names one of them alone."
        )
        raise _AmbiguousSpace(
            f"{space!r} names more than one space at your provider: {shown}. {how}"
        )
    (entry,) = matches.values()
    name = entry.get("name")
    name = name if isinstance(name, str) and name else entry["spaceId"]
    return _Space(entry["spaceId"], name, names_one(name), _dir_id_of(entry))


class _Run:
    """One tool call: its local provider, its time limit, its caches, its REST-call limit.

    `deadline` is the monotonic time by which the current phase must be done. Phases set it
    as they begin. Every read made through `within` or `call_in_time` is cut off by it, so a
    chain of reads cannot run past the budget.
    """

    def __init__(self, local_id: str, deadline: float) -> None:
        self.local_id = local_id
        self.deadline = deadline
        self._rest = asyncio.Semaphore(CHECK_CONCURRENCY)
        self._space_of: dict[str, _Space] = {}
        self._spaces: list[dict[str, Any]] | None = None
        self._online: dict[str, asyncio.Future[bool]] = {}
        self._transfers: dict[str, asyncio.Future[list[_Transfer] | None]] = {}

    def out_of_time(self) -> bool:
        return _monotonic() >= self.deadline

    async def within[R](self, work: Coroutine[Any, Any, R]) -> R:
        """Await `work`, but no longer than the budget allows: TimeoutError once it is gone."""
        remaining = self.deadline - _monotonic()
        if remaining <= 0:
            work.close()
            raise TimeoutError("time budget used up")
        return await asyncio.wait_for(work, remaining)

    async def call[R](self, fn: Callable[..., Awaitable[R]], *args: Any, **kwargs: Any) -> R:
        """One REST call, within the limit of CHECK_CONCURRENCY in flight for the whole call."""
        async with self._rest:
            return await fn(*args, **kwargs)

    async def call_in_time[R](
        self, fn: Callable[..., Awaitable[R]], *args: Any, **kwargs: Any
    ) -> R:
        """`call`, cut off by the budget (waiting for the limit counts against it)."""
        return await self.within(self.call(fn, *args, **kwargs))

    async def space(self, space: str) -> _Space:
        """The one space `space` names, by its exact id or its exact name (see `_choose_space`).
        ValueError when none or more than one does; OnedataApiError when the provider's list
        cannot be read as one."""
        if space not in self._space_of:
            self._space_of[space] = _choose_space(space, await self.within(self._space_list()))
        return self._space_of[space]

    async def space_id(self, space: str) -> str:
        return (await self.space(space)).space_id

    async def _space_list(self) -> list[dict[str, Any]]:
        """The local provider's own `GET /spaces`, read once per call.

        No Onezone lookup, so a token valid only at the local provider resolves names too (the
        existing resolver also reads each space's details from Onezone). An answer that is not
        a list is the provider's failure, not a missing space: OnedataApiError.
        """
        if self._spaces is None:
            response = await self.call(request, get_oneprovider_config(), "GET", "/spaces")
            body = response["body"]
            if not isinstance(body, list):
                raise OnedataApiError(
                    "Oneprovider's list of spaces (GET /spaces) is not a list, so no dataset's "
                    "space can be resolved. Call again; if it persists, the provider is at fault.",
                    response,
                )
            self._spaces = [
                entry
                for entry in body
                if isinstance(entry, dict)
                and isinstance(entry.get("spaceId"), str)
                and entry["spaceId"]
            ]
        return self._spaces

    async def space_dir_id(self, space: _Space) -> str | None:
        """The id of the space's own directory, bound to the space's id: from the provider's list
        when it gives one (`dirId`, or the deprecated `fileId`), else from `GET /spaces/{id}`.
        None when neither says. TODO(first live run): what 25.0 lists here."""
        if space.dir_id:
            return space.dir_id
        segment = id_segment(space.space_id, "space id")
        response = await self.call_in_time(
            request, get_oneprovider_config(), "GET", f"/spaces/{segment}"
        )
        body = response.get("body")
        if not isinstance(body, dict) or body.get("spaceId") != space.space_id:
            return None  # an answer about another space (or none) gives no directory
        return _dir_id_of(body)

    async def provider_online(self, provider_id: str) -> bool:
        """Onezone's `online` for a provider, looked up once per call.

        BEST EFFORT: a failed lookup, or none made because the budget is gone, means online.
        """
        if provider_id not in self._online:
            self._online[provider_id] = asyncio.ensure_future(self._lookup_online(provider_id))
        return await self._online[provider_id]

    async def _lookup_online(self, provider_id: str) -> bool:
        try:
            # The id comes from a distribution answer: one encoded path segment, and an id
            # that cannot be one (empty, `.`, `..`, a `/`) is not looked up (ValueError).
            segment = id_segment(provider_id, "provider id")
            response = await self.call_in_time(
                request, get_onezone_config(), "GET", f"/providers/{segment}"
            )
            return response["body"].get("online") is not False
        except Exception:  # noqa: BLE001 - best effort: never fail a tool on this lookup
            return True

    async def moving(self, file: FileInfo) -> bool | None:
        """Something is bringing the file to the local provider; None if the budget ran out."""
        transfers = await self._ongoing_transfers(file.space_id)
        if transfers is None:
            return None
        for transfer in transfers:
            if (
                transfer.replicating == self.local_id
                and transfer.status in MOVING_STATES
                and (
                    transfer.file_id == file.file_id
                    or (transfer.path and _within(file.path, transfer.path))
                )
            ):
                return True
        rule = _rules.get(file.file_id)
        if rule is None:
            return False
        try:
            return (await self.call_in_time(get_qos_requirement, rule)).get("status") == "pending"
        except TimeoutError:
            return None
        except OnedataApiError:
            return False

    async def _ongoing_transfers(self, space_id: str) -> list[_Transfer] | None:
        if space_id not in self._transfers:
            self._transfers[space_id] = asyncio.ensure_future(self._load_transfers(space_id))
        return await self._transfers[space_id]

    async def _load_transfers(self, space_id: str) -> list[_Transfer] | None:
        """Ongoing transfers of a space (at most MAX_TRANSFERS).

        An empty list if Onedata will not say: listing needs the `space_view_transfers`
        privilege, and without it nothing is known to be moving. None if the budget ran
        out first.
        """
        try:
            listing = await self.call_in_time(
                list_space_transfers, space_id, state="ongoing", limit=MAX_TRANSFERS
            )
        except TimeoutError:
            return None
        except OnedataApiError:
            return []
        ids = [t for t in listing.get("transfers") or [] if isinstance(t, str)][:MAX_TRANSFERS]
        timed_out = False

        async def one(transfer_id: str) -> _Transfer | None:
            nonlocal timed_out
            try:
                body = await self.call_in_time(get_transfer, transfer_id)
            except TimeoutError:
                timed_out = True
                return None
            except OnedataApiError:
                return None
            status = body.get("transferStatus", body.get("transferState"))
            return _Transfer(
                body.get("fileId"),
                body.get("filePath"),
                body.get("replicatingProviderId"),
                status if isinstance(status, str) else None,
            )

        results = await _bounded(CHECK_CONCURRENCY, ids, one, self.deadline)
        if timed_out or any(r is _SKIPPED for r in results):
            return None
        return [t for t in results if t is not None]


async def _local_id_within(deadline: float) -> str:
    """The local provider's id, the read cut off by what is left of the budget."""
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise TimeoutError("time budget used up")
    return await asyncio.wait_for(_local_id(), remaining)


async def _local_id() -> str:
    global _local_provider_id
    if _local_provider_id is None:
        response = await request(get_oneprovider_config(), "GET", "/configuration")
        body = response["body"]
        provider_id = body.get("providerId") if isinstance(body, dict) else None
        if not isinstance(provider_id, str) or not provider_id:
            raise OnedataApiError("Oneprovider configuration carried no providerId")
        _local_provider_id = provider_id
    return _local_provider_id


@dataclass
class _Listing:
    files: list[FileInfo] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # why this is not the whole dataset
    root: str = ""  # the dataset's directory, its space named by name (a space id replaced)
    # False when the listing stopped before its end (time, a refusal, a page it could not read
    # on, directories it may not list): a file not in `files` may still be there
    complete: bool = True


def _no_time(item: _Item) -> str:
    return (
        f"dataset '{item.dataset}': listing did not finish within the time budget. "
        "Call again, or give a smaller directory."
    )


def _no_such_directory(item: _Item) -> str:
    # A bare 404 does not say why (no such directory, or one this token cannot see): the
    # text names both and claims neither.
    return (
        f"dataset '{item.dataset}' not found at your provider: no such directory, or one this "
        "token cannot see. Give a space name or an existing absolute directory path."
    )


def _not_allowed(item: _Item) -> str:
    return (
        f"dataset '{item.dataset}' cannot be listed: the provider refused this token access "
        "to it. Declare data the token can read."
    )


def _shared_name(item: _Item, space: _Space) -> str:
    return (
        f"dataset '{item.dataset}': the name of its space, '{space.name}', is also another "
        "space's name or id at your provider, so no path inside it is looked up by that name. "
        f"Declare the space itself by its id ('{space.space_id}') and name the files by their "
        "path inside it."
    )


def _other_space(item: _Item, space: _Space, found: str) -> str:
    return (
        f"dataset '{item.dataset}' resolved to a directory of another space (id '{found}'), not "
        f"of the space '{space.name}' (id '{space.space_id}'), so nothing there is checked. "
        "Declare the space by its id, or an absolute directory path inside it."
    )


# An Onedata file or space id: a long run of hexadecimal digits. Given where a name or a path
# is asked for, it is named as an id in the hint.
_ID_LIKE = re.compile(r"[0-9A-Fa-f]{32,}")


def _looks_like_id(value: str) -> bool:
    return bool(_ID_LIKE.fullmatch(value.strip("/")))


def _status(exc: OnedataError) -> int | None:
    status = exc.response.get("status_code") if isinstance(exc.response, dict) else None
    return status if isinstance(status, int) else None


def _is_gone(exc: OnedataError) -> bool:
    """The provider answered that the path is not there: an unsupported space, a structured
    not-found (enoent, notFound) on a 4xx, or a bare 404. A 5xx is never read as gone."""
    if isinstance(exc, OnedataInvalidSpaceError):
        return True
    status = _status(exc)
    client_error = status is not None and 400 <= status < 500
    return status == 404 or (client_error and (exc.errno == "enoent" or exc.error_id == "notFound"))


def _is_forbidden(exc: OnedataError) -> bool:
    """A 403, or a 4xx with the `forbidden` error id or an `eacces`/`eperm` errno."""
    status = _status(exc)
    client_error = status is not None and 400 <= status < 500
    return status == 403 or (
        client_error and (exc.error_id == "forbidden" or exc.errno in {"eacces", "eperm"})
    )


_FILE_TYPES = frozenset({"REG", "DIR", "SYMLNK"})  # the swagger's FileType


async def _list_dataset(run: _Run, item: _Item) -> _Listing:
    """All regular files under a dataset, and every reason that is not the whole story.

    Never completes a listing silently: a page that says more follows but gives no way to
    ask for it, an entry without an id, or directories the token cannot list all make the
    dataset unresolved. More than MAX_FILES regular files is refused as soon as it is seen.
    """
    listing = _Listing()
    try:
        space = await run.space(item.space)
    except _AmbiguousSpace as exc:
        listing.problems.append(f"dataset '{item.dataset}': {exc}")
        return listing
    except ValueError as exc:
        if _looks_like_id(item.space):
            listing.problems.append(
                f"dataset '{item.dataset}' not found: it looks like an id, and no space your "
                "provider lists for this token has that id (a file's or a directory's id is not "
                "taken here). Give a space's name or id, or an absolute directory path such as "
                "'/my-dataset/sub'."
            )
        else:  # the message names the spaces the provider lists
            listing.problems.append(
                f"dataset '{item.dataset}' not found: {exc}. "
                "Give one of those names, or an absolute directory path inside one."
            )
        return listing
    except TimeoutError:
        listing.problems.append(_no_time(item))
        return listing
    # Once chosen, the space is bound to its id: its name is put in a path only when no other
    # space has that name or id, and the directory found is checked against the id.
    rest = item.root[len(item.space) + 1 :]  # "" for the space itself, else "/sub/dir"
    root = "/" + space.name + rest
    listing.root = root
    try:
        if space.name_unique:
            root_id = await run.within(get_file_id(root))
        elif rest:
            listing.problems.append(_shared_name(item, space))
            return listing
        elif (dir_id := await run.space_dir_id(space)) is not None:
            root_id = dir_id
        else:
            listing.problems.append(_shared_name(item, space))
            return listing
    except (FileNotFoundError, OnedataInvalidSpaceError):
        listing.problems.append(_no_such_directory(item))
        return listing
    except OnedataApiError as exc:
        if _is_forbidden(exc):
            listing.problems.append(_not_allowed(item))
            return listing
        if not _is_gone(exc):  # a plain 404 or notFound from the lookup is "not found"
            raise
        listing.problems.append(_no_such_directory(item))
        return listing
    except TimeoutError:
        listing.problems.append(_no_time(item))
        return listing
    parts = _file_id_parts(root_id)
    if parts is not None and parts[1] != space.space_id:
        listing.problems.append(_other_space(item, space, parts[1]))
        return listing
    space_id = space.space_id

    seen = 0
    incomplete = False
    inaccessible: list[str] = []
    token: str | None = None
    while True:
        # Paged by the directory's id: a path would be looked up again on every page.
        # The listing answers paths relative to the listed directory.
        try:
            page = await run.within(
                list_files_recursively(
                    root_id, attributes=LISTING_ATTRIBUTES, limit=LIST_PAGE_LIMIT, token=token
                )
            )
        except TimeoutError:
            listing.problems.append(_no_time(item))
            break
        except (OnedataApiError, OnedataInvalidSpaceError) as exc:
            if _is_forbidden(exc):
                listing.problems.append(_not_allowed(item))
                break
            if not _is_gone(exc):
                raise
            listing.problems.append(_no_such_directory(item))
            break
        except (AttributeError, TypeError):
            # The helper read a body that is not an object (null, a list, a page of HTML).
            incomplete = True
            break
        entries = page.get("files") if isinstance(page, dict) else None
        if not isinstance(entries, list):
            incomplete = True
            break
        for entry in entries:
            file_type = entry.get("type") if isinstance(entry, dict) else None
            if not isinstance(file_type, str) or file_type not in _FILE_TYPES:
                incomplete = True  # not an entry, or a type this tool does not know
                continue
            if file_type == "SYMLNK":
                continue
            rel = entry.get("path")
            if file_type == "DIR":
                incomplete = incomplete or not (isinstance(rel, str) and rel)
                continue
            seen += 1
            if seen > MAX_FILES:
                raise ValueError(
                    f"The dataset '{item.dataset}' has more than {MAX_FILES} files, and an "
                    f"extent of more than {MAX_FILES} files is refused. "
                    "Declare a smaller dataset, such as a subdirectory."
                )
            file_id = entry.get("fileId")
            if not isinstance(file_id, str) or not file_id or not isinstance(rel, str) or not rel:
                incomplete = True
                continue
            size = entry.get("size")
            size_known = isinstance(size, int) and not isinstance(size, bool) and size >= 0
            rate = entry.get("localReplicationRate")
            flag = entry.get("isFullyReplicatedLocally")
            origin = entry.get("originProviderId")
            info = FileInfo(
                file_id=file_id,
                path=_absolute(root, rel),
                space_id=space_id,
                size=size if size_known else 0,
                size_known=size_known,
                flag=flag if isinstance(flag, bool) else None,
                origin=origin if isinstance(origin, str) else None,
            )
            if isinstance(rate, int | float) and not isinstance(rate, bool):
                info.local_bytes = int(rate * info.size)
            elif info.flag:
                info.local_bytes = info.size
            listing.files.append(info)
        paths = page.get("inaccessiblePaths")
        if isinstance(paths, list):
            inaccessible.extend(p for p in paths if isinstance(p, str))
            incomplete = incomplete or not all(isinstance(p, str) for p in paths)
        elif paths is not None:
            incomplete = True
        next_token = page.get("nextPageToken")
        is_last = page.get("isLast")
        # Onedata 25.0 (measured live) omits `isLast` and ends with `nextPageToken: null`;
        # the swagger also documents `isLast`. Either marks the last page; `isLast: false`
        # with no usable token stays incomplete below.
        if is_last is True or (
            "isLast" not in page and "nextPageToken" in page and next_token is None
        ):
            break
        if not isinstance(next_token, str) or not next_token or next_token == token:
            incomplete = True  # not the last page, and no way to ask for the next one
            break
        token = next_token

    if incomplete:
        listing.problems.append(
            f"dataset '{item.dataset}': listing incomplete, not every page arrived. "
            "Call again, or give a smaller directory."
        )
    listing.complete = not (incomplete or inaccessible or listing.problems)
    for path in dict.fromkeys(inaccessible):
        listing.problems.append(
            f"directory '{_absolute(root, path)}' in '{item.dataset}' is not listed "
            "(no access), so files under it cannot be checked. "
            "Declare files outside it, or get access."
        )
    return listing


MAX_SAME_NAME_SHOWN = 5  # paths listed for a file name that more than one file has


def _same_name(item: _Item, name: str, matches: list[str]) -> str:
    shown = sorted(matches)
    listed = ", ".join(f"'{p}'" for p in shown[:MAX_SAME_NAME_SHOWN])
    if len(shown) > MAX_SAME_NAME_SHOWN:
        listed += f" and {len(shown) - MAX_SAME_NAME_SHOWN} more"
    inside = shown[0][len(item.root.rstrip("/")) + 1 :]
    return (
        f"file '{name}' in '{item.dataset}' matches {len(shown)} files ({listed}), so none of "
        f"them is taken: give the one you mean by its path inside the dataset (such as "
        f"'{inside}') or its absolute path."
    )


def select_paths(
    item: _Item, paths: Sequence[str], *, complete: bool = True
) -> tuple[list[str], list[str]]:
    """The paths of `paths` (every file listed under the item's dataset, absolute, named by the
    space's name) that an item selects, and a text for each thing it declares that selects no
    file or that cannot say which one. Pure: the tool's `_select` and the validation panel's
    scorer (`benchmark/k2val/readiness_oracles.extent_selects`) share this one rule.

    - neither `files` nor `date_ranges`: every file of the dataset;
    - `files`: each matched exactly as declared, by its absolute path, its path inside the
      dataset, or its name; a name that more than one file has selects none of them (the text
      lists them, to be given by path), never all of them;
    - `date_ranges`: the files whose name carries a YYYYMMDD inside one of the ranges;
    - both: the files that either one selects (their union).

    `complete` False: `paths` is a listing that stopped early. A file or range not seen in it is
    unknown, never "not found"; a name seen once is not taken (another file of that name may be
    in the rest of the listing); a path seen is taken.
    """
    unresolved: list[str] = []
    if not item.files and not item.ranges:
        if not paths:
            unresolved.append(
                f"dataset '{item.dataset}' holds no files. "
                "Give a directory that holds the data, or name the files."
            )
        return list(paths), unresolved

    known = set(paths)
    by_name: dict[str, list[str]] = {}
    for path in paths:
        by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    chosen: dict[str, None] = {}
    for name in item.files:
        # Names are matched exactly as declared: no trimming, no normalising.
        if name.startswith("/"):
            matches = [name] if name in known else []
        elif "/" in name:
            full = _absolute(item.root, name)
            matches = [full] if full in known else []
        else:
            matches = by_name.get(name, [])
            if len(matches) > 1:
                unresolved.append(_same_name(item, name, matches))
                continue
            if matches and not complete:
                unresolved.append(
                    f"file '{name}' in '{item.dataset}' is not taken: the listing stopped early, "
                    "so another file of that name may be in the rest of it. Give it by its path "
                    "inside the dataset or its absolute path."
                )
                continue
        if not matches and not complete:
            unresolved.append(
                f"file '{name}' in '{item.dataset}' is unknown: it is not among the files listed "
                "before the listing stopped, so whether it is there is not known."
            )
        elif not matches and _looks_like_id(name):
            unresolved.append(
                f"file '{name}' in '{item.dataset}' not found: it looks like a file id, and "
                "files are given by their exact name or their absolute path."
            )
        elif not matches:
            unresolved.append(
                f"file '{name}' in '{item.dataset}' not found: "
                "give the exact file name, or its absolute path."
            )
        chosen.update(dict.fromkeys(matches))

    dated = [(p, name_date(p.rsplit("/", 1)[-1])) for p in paths] if item.ranges else []
    for start, end in item.ranges:
        matches = [p for p, day in dated if day is not None and start <= day <= end]
        if not matches and not complete:
            unresolved.append(
                f"date range {start.isoformat()}..{end.isoformat()} in '{item.dataset}' is "
                "unknown: it matched no file among those listed before the listing stopped."
            )
        elif not matches:
            unresolved.append(
                f"date range {start.isoformat()}..{end.isoformat()} in '{item.dataset}' "
                "matched no file: dates are read from the YYYYMMDD in file names. "
                "Widen the range or name the files."
            )
        chosen.update(dict.fromkeys(matches))
    return list(chosen), unresolved


def _select(
    item: _Item, files: list[FileInfo], unresolved: list[str], *, complete: bool = True
) -> list[FileInfo]:
    """The files an item declares (`select_paths`); what selects nothing goes to `unresolved`."""
    by_path = {f.path: f for f in files}
    selected, problems = select_paths(item, list(by_path), complete=complete)
    unresolved.extend(problems)
    return [by_path[path] for path in selected]


def _merge_file(into: FileInfo, other: FileInfo) -> None:
    """The same file listed twice: keep the most that either listing says (never `setdefault`)."""
    if other is into:
        return
    into.size = max(into.size, other.size)
    into.size_known = into.size_known or other.size_known
    flags = {into.flag, other.flag} - {None}
    into.flag = False if False in flags else (True if flags else None)
    into.origin = into.origin or other.origin
    into.local_bytes = min(into.local_bytes, other.local_bytes)


async def _resolve(run: _Run, items: list[_Item]) -> tuple[list[FileInfo], list[str]]:
    """The files the extent declares, and every item that did not resolve."""
    unresolved: list[str] = []
    resolved: dict[str, FileInfo] = {}
    listings: dict[str, _Listing] = {}
    for item in items:
        if item.root not in listings:
            listings[item.root] = await _list_dataset(run, item)
            unresolved.extend(listings[item.root].problems)
        listing = listings[item.root]
        if not listing.files and listing.problems:
            continue  # nothing to pick from; the problems are reported above
        named = replace(item, root=listing.root) if listing.root else item
        for f in _select(named, listing.files, unresolved, complete=listing.complete):
            if f.file_id in resolved:
                _merge_file(resolved[f.file_id], f)
            else:
                resolved[f.file_id] = f
        if len(resolved) > MAX_FILES:
            raise ValueError(
                f"The extent resolves to more than {MAX_FILES} files. "
                "Declare a smaller extent: name files or date ranges."
            )
    return list(resolved.values()), unresolved


# ---------------------------------------------------------------------------
# Assess: classify files against their distribution
# ---------------------------------------------------------------------------


async def _assess(run: _Run, files: list[FileInfo], *, raise_errors: bool = True) -> None:
    """Set `state` (and `dist`, `local_bytes`) on each file; files not reached are "unchecked".

    A file is also unchecked when its distribution cannot be read, or no positive size is
    known for it, or the budget ran out before its follow-up reads. If every file was
    attempted and every read failed, the first error is raised: that is a broken
    connection, not an unready file. If some files were skipped at the deadline, the
    failures are not raised: all of those files are unchecked. With `raise_errors` False
    (the final pass of ensure) nothing is ever raised.
    """
    errors: list[OnedataApiError] = []

    async def one(f: FileInfo) -> None:
        try:
            body = await run.call_in_time(get_file_distribution, f.file_id)
        except TimeoutError:
            f.state = "unchecked"
            return
        except OnedataApiError as exc:
            errors.append(exc)
            f.state = "unchecked"
            return
        dist = parse_distribution(body, f.known_size)
        f.dist = dist
        if not dist.known:
            f.state = "unchecked"  # no size from any source: coverage cannot be judged
            return
        f.size = dist.size
        f.size_known = True
        f.local_bytes = min(dist.covered.get(run.local_id, 0), f.size)
        if is_ready(dist, run.local_id, f.origin, f.flag):
            f.state = "ready"
            f.cover_size = dist.size
            return
        if run.out_of_time():
            # No time left for the follow-up reads (provider, transfers, rule): the file is
            # known not to be ready, but why is not known.
            f.state = "unchecked"
            return
        remote = sorted((dist.holders | dist.covering) - {run.local_id} - dist.failed)
        online = await asyncio.gather(*(run.provider_online(p) for p in remote))
        offline = [p for p, up in zip(remote, online, strict=True) if not up]
        moving = False
        if writes_complete(dist, run.local_id, f.origin):
            moving = await run.moving(f)
            if moving is None:
                f.state = "unchecked"
                return
        f.state = classify(
            dist, run.local_id, f.origin, flag=f.flag, offline=offline, moving=moving
        )

    results = await _bounded(CHECK_CONCURRENCY, files, one, run.deadline)
    skipped = 0
    for f, result in zip(files, results, strict=True):
        if result is _SKIPPED:
            f.state = "unchecked"
            skipped += 1
    if raise_errors and files and not skipped and len(errors) == len(files):
        raise errors[0]


async def _reconfirm(run: _Run, files: list[FileInfo]) -> None:
    """The final pass before ensure answers ready: read each confirmed file's attributes again.

    A file stays ready only if the read says it is still fully replicated here and has not
    grown past the size it was confirmed at. A false `isFullyReplicatedLocally` is final:
    the file is not ready, and no assessment may make it ready again. A lost replica, or a
    bigger file, sends the file back to be judged anew. A read that fails, says nothing
    certain, or is skipped for want of time leaves the file `unchecked`. Never raises.
    """

    async def one(f: FileInfo) -> None:
        try:
            attributes = await run.call_in_time(
                get_file_attributes, f.file_id, attributes=POLL_ATTRIBUTES
            )
        except (OnedataApiError, TimeoutError):
            f.state = "unchecked"
            return
        flag = attributes.get("isFullyReplicatedLocally")
        size = attributes.get("size")
        if not isinstance(flag, bool) or not isinstance(size, int) or isinstance(size, bool):
            f.state = "unchecked"
            return
        f.flag = flag  # what the final read says, whatever the earlier ones said
        if size >= 0:
            f.size = max(f.size, size)  # a bigger file is judged at its bigger size
            f.size_known = True
        if flag and 0 <= size <= f.cover_size:
            return
        f.state = None  # not ready as it stands: judged anew, with this flag

    results = await _bounded(CHECK_CONCURRENCY, files, one, run.deadline)
    for f, result in zip(files, results, strict=True):
        if result is _SKIPPED:
            f.state = "unchecked"


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def _noun(n: int) -> str:
    return f"{n} file" if n == 1 else f"{n} files"


def _phrase(reason: str, n: int, *, ensure: bool) -> str:
    """What a reason means, for the advice."""
    be = "is" if n == 1 else "are"
    poss = "its" if n == 1 else "their"
    if reason == "being_written":
        tail = "" if ensure else f", so {poss} bytes are incomplete"
        return f"{_noun(n)} {be} still being written at {poss} owner{tail}."
    if reason == "at_other_site":
        return f"{_noun(n)} {be} complete at another provider and not yet at yours."
    if reason == "in_transfer":
        return f"{_noun(n)} {be} on the way to your provider."
    return f"the only copies of {_noun(n)} are at a provider that is down."


# Why a file's hold is missing, as far as the provider's answers show, in the order the advice
# names them. Only a refusal (403, `forbidden`, `eacces`, `eperm`) is called one.
HOLD_CAUSES = ("refused", "rejected", "no_answer", "not_confirmed", "gone", "out_of_time")


def _hold_sentence(cause: str, n: int) -> str:
    files = _noun(n)
    if cause == "refused":
        return (
            f"A hold could not be placed for {files}: the provider refused this token the right "
            "to add QoS requirements."
        )
    if cause == "rejected":
        return (
            f"A hold could not be placed for {files}: the provider rejected the request; the "
            "next call tries again."
        )
    if cause == "no_answer":
        return (
            f"For {files}, the request to place a hold got no answer in time: a hold may have "
            "been placed, and is not listed; the next call places one again."
        )
    if cause == "not_confirmed":
        return (
            f"For {files}, the hold could not be read back to confirm it (its id is under "
            "hold_failed); the next call reads it again."
        )
    if cause == "gone":
        return (
            f"For {files}, the hold was removed before this answer; the next call places it again."
        )
    return f"For {files}, no hold was placed: the time ran out first; the next call places it."


_CAN_WAIT_FOR = ("being_written", "in_transfer", "at_other_site")


def _advice(
    *,
    ready: bool,
    counts: Mapping[str, int],
    unresolved: int,
    unchecked: int,
    waited_s: int | None,
    data_ready: bool = False,
    hold_causes: Mapping[str, int] | None = None,
    ensure_offered: bool = False,
) -> str:
    """Plain sentences: what the answer means. Every reason a file is not ready is named, in
    REASONS order, then what was not resolved or checked, then why holds are missing (ensure),
    in HOLD_CAUSES order. It names a tool only where that tool is offered (design/10, gate 2):
    check's answer points to ensure_readiness when the caller says it is offered
    (`ensure_offered`), for files whose reason ensure_readiness can act on.

    Never advice on what the agent does with the data.
    """
    ensure = waited_s is not None
    if ready:
        text = "Ready: all declared files are complete and readable at your provider now"
        return text + (", and each is held there." if ensure else ".")

    hold_causes = hold_causes or {}
    sentences: list[str] = []
    if data_ready:  # ensure only: the data are, the holds are not
        missing = sum(hold_causes.values())
        be = "is" if missing == 1 else "are"
        sentences.append(
            "the data are complete and readable at your provider now (data_ready), but "
            f"{_noun(missing)} {be} not held there."
        )
    for reason in REASONS:
        if counts.get(reason):
            sentences.append(_phrase(reason, counts[reason], ensure=ensure))
    if unresolved:
        what = "item was" if unresolved == 1 else "items were"
        sentences.append(f"{unresolved} declared {what} not resolved (see unresolved).")
    if unchecked:
        sentences.append(f"{_noun(unchecked)} could not be checked.")
    for cause in HOLD_CAUSES:
        if hold_causes.get(cause):
            sentences.append(_hold_sentence(cause, hold_causes[cause]))
    waitable = sum(counts.get(reason, 0) for reason in _CAN_WAIT_FOR)
    if ensure and waitable:
        sentences.append("Call again to keep waiting.")
    elif ensure_offered and waitable:
        sentences.append(ENSURE_POINTER.format(them="it" if waitable == 1 else "them"))
    lead = f"Not ready after {waited_s} s:" if ensure else "Not ready:"
    return f"{lead} {' '.join(sentences)}"


@dataclass
class _Hold:
    """What one ensure call holds: kept in the call's own result, never re-read from what the
    process remembers (another call may have changed that meanwhile)."""

    expression: str
    held: dict[str, str] = field(default_factory=dict)  # file id -> the rule confirmed for it
    failed: dict[str, tuple[FileInfo, str]] = field(default_factory=dict)  # id -> (file, cause)
    # rules this call could not read back, but may well exist: file id -> rule id
    unconfirmed: dict[str, str] = field(default_factory=dict)

    def fail(self, file: FileInfo, cause: str, rule_id: str | None = None) -> None:
        self.held.pop(file.file_id, None)
        self.failed[file.file_id] = (file, cause)
        if rule_id is not None:
            self.unconfirmed[file.file_id] = rule_id

    def ids(self, files: list[FileInfo]) -> list[str]:
        """The ids of the rules that hold the files, in the files' path order."""
        return [
            self.held[f.file_id]
            for f in sorted(files, key=lambda f: f.path)
            if f.file_id in self.held
        ]

    def causes(self) -> dict[str, int]:
        found = [cause for _, cause in self.failed.values()]
        return {cause: found.count(cause) for cause in HOLD_CAUSES if cause in found}


def _examples(files: Iterable[FileInfo]) -> list[str]:
    return sorted(f.path for f in files)[:MAX_EXAMPLES]


def _with_examples(files: list[FileInfo]) -> dict[str, Any]:
    """A count and at most MAX_EXAMPLES paths; how many are not shown, when any are not."""
    out: dict[str, Any] = {"files": len(files), "examples": _examples(files)}
    if len(files) > MAX_EXAMPLES:
        out["examples_not_shown"] = len(files) - MAX_EXAMPLES
    return out


def _answer(
    files: list[FileInfo],
    unresolved: list[str],
    *,
    hold: _Hold | None = None,
    waited_s: int | None = None,
    ensure_offered: bool = False,
) -> dict[str, Any]:
    """The answer of either tool. `resolved_files` counts the files the extent resolved to (not
    those unresolved), `ready_files` those of them confirmed ready. ensure's `ready` asks more
    than its `data_ready`: every resolved file also held, its hold confirmed for this answer."""
    data_ready = bool(files) and not unresolved and all(f.state == "ready" for f in files)
    ready = data_ready and (hold is None or len(hold.held) == len(files))
    by_reason: dict[str, list[FileInfo]] = {}
    unchecked = 0
    for f in files:
        if f.state in REASONS:
            by_reason.setdefault(f.state, []).append(f)
        elif f.state != "ready":
            unchecked += 1

    answer: dict[str, Any] = {"ready": ready}
    if hold is not None:
        answer["data_ready"] = data_ready
    answer["resolved_files"] = len(files)
    # How many of them are ready: the examples per reason are at most three, so the count says
    # how many the answer is about (design/17, decision 5).
    answer["ready_files"] = sum(f.state == "ready" for f in files)
    answer["bytes"] = sum(f.size for f in files)
    if not data_ready:
        answer["bytes_local"] = sum(min(f.local_bytes, f.size) for f in files)
        if by_reason:
            answer["not_ready"] = {
                reason: _with_examples(by_reason[reason])
                for reason in REASONS
                if reason in by_reason
            }
        shown = unresolved[:MAX_UNRESOLVED_SHOWN]
        if len(unresolved) > len(shown):
            shown.append(f"... and {len(unresolved) - len(shown)} more")
        answer["unresolved"] = shown
        if unchecked:
            answer["unchecked"] = unchecked
    answer["advice"] = _advice(
        ready=ready,
        counts={r: len(fs) for r, fs in by_reason.items()},
        unresolved=len(unresolved),
        unchecked=unchecked,
        waited_s=waited_s,
        data_ready=data_ready,
        hold_causes=hold.causes() if hold else None,
        ensure_offered=ensure_offered and hold is None,
    )
    if hold is not None:
        answer["waited_s"] = waited_s
        ids = hold.ids(files)
        # Each id is a QoS requirement that keeps its file here until it is removed: the
        # answer says which, so the holds can be released (design/18, decision 3).
        answer["held"] = {
            "files": len(hold.held),
            "rule": hold.expression,
            "qos_requirement_ids": ids[:MAX_HOLD_IDS_SHOWN],
        }
        if len(ids) > MAX_HOLD_IDS_SHOWN:
            answer["held"]["qos_requirement_ids_not_shown"] = len(ids) - MAX_HOLD_IDS_SHOWN
            answer["held"]["note"] = HOLD_IDS_ELSEWHERE
        if hold.failed:
            failed = [f for f, _ in hold.failed.values()]
            answer["hold_failed"] = {**_with_examples(failed), "causes": hold.causes()}
            if hold.unconfirmed:
                answer["hold_failed"]["unconfirmed_qos_requirement_ids"] = [
                    hold.unconfirmed[f.file_id]
                    for f in sorted(failed, key=lambda f: f.path)
                    if f.file_id in hold.unconfirmed
                ][:MAX_HOLD_IDS_SHOWN]
    return answer


# ---------------------------------------------------------------------------
# check_readiness
# ---------------------------------------------------------------------------


def _config_unavailable(waited_s: int | None = None) -> dict[str, Any]:
    """The answer when the provider's own id could not be read in time: nothing was checked."""
    answer: dict[str, Any] = {"ready": False}
    if waited_s is not None:
        answer["data_ready"] = False
    answer.update(resolved_files=0, ready_files=0, bytes=0, bytes_local=0, unresolved=[])
    if waited_s is None:
        answer["advice"] = (
            "Not ready: provider configuration unavailable, so nothing could be checked. "
            "Call again."
        )
        return answer
    answer["advice"] = (
        f"Not ready after {waited_s} s: provider configuration unavailable, so nothing could "
        "be placed or checked. Call again."
    )
    answer["waited_s"] = waited_s
    answer["held"] = {
        "files": 0,
        "rule": HOLD_EXPRESSION.format(provider_id="<local provider>"),
        "qos_requirement_ids": [],
    }
    return answer


async def check_readiness(
    extent: Sequence[Mapping[str, Any]], *, ensure_offered: bool = False
) -> dict[str, Any]:
    """Whether the declared data are ready at the local provider, or why not. Read-only.

    `ensure_offered`: ensure_readiness is offered beside this tool, so the answer may point to
    it (the MCP wrapper says, from the server's own tool list)."""
    items = _normalise_extent(extent)
    deadline = _monotonic() + CHECK_BUDGET_S
    try:
        local_id = await _local_id_within(deadline)
    except TimeoutError:
        return _config_unavailable()
    run = _Run(local_id, deadline)
    files, unresolved = await _resolve(run, items)
    await _assess(run, files)
    return _answer(files, unresolved, ensure_offered=ensure_offered)


# ---------------------------------------------------------------------------
# ensure_readiness
# ---------------------------------------------------------------------------


async def _nudge(file: FileInfo) -> bool:
    """Hook: push a file whose rule reports fulfilled but whose replica is not local.

    Called on each poll for a file that has writes complete, is not local, and whose rule
    reports fulfilled. A no-op for now. If the live test shows QoS does not re-sync blocks
    written after the rule was added, this will schedule a replication with
    `create_transfer`. Returns whether it did something.
    """
    return False


def _hold_expression(local_id: str) -> str:
    return (HOLD_EXPRESSION_PLAIN if _hold_plain else HOLD_EXPRESSION).format(provider_id=local_id)


async def _place_one(run: _Run, file: FileInfo) -> tuple[str, str | None]:
    """Add the placement rule for a file; falls back to the plain expression on a 400.

    ("held", the new rule's id), remembered for the file; else (the cause, None), a cause of
    HOLD_CAUSES: "refused" (403, `forbidden`, `eacces`, `eperm`), "no_answer" (no answer in
    time, or the connection failed after sending: a rule may have been made), "rejected"
    (another error, or an answer without an id)."""
    global _hold_plain
    templates = [HOLD_EXPRESSION_PLAIN] if _hold_plain else [HOLD_EXPRESSION, HOLD_EXPRESSION_PLAIN]
    for index, template in enumerate(templates):
        try:
            created = await run.within(
                add_qos_requirement(
                    file.file_id, template.format(provider_id=run.local_id), replicas_num=1
                )
            )
        except TimeoutError:
            return "no_answer", None
        except OnedataApiError as exc:
            status = exc.response.get("status_code") if exc.response else None
            if _is_forbidden(exc):
                return "refused", None
            if status == 400 and index + 1 < len(templates):
                continue  # Onedata rejected the expression form: try the plain one
            return ("no_answer" if exc.kind is not None else "rejected"), None
        rule_id = created.get("qosRequirementId") if isinstance(created, dict) else None
        if not isinstance(rule_id, str) or not rule_id:
            return "rejected", None
        _rules[file.file_id] = rule_id
        if index == 1:
            _hold_plain = True  # the primary form was rejected, the plain one accepted
        return "held", rule_id
    return "rejected", None


# What a read-back of a remembered rule showed.
CONFIRMED, ABSENT, UNREADABLE = "confirmed", "absent", "unreadable"


async def _rule_state(run: _Run, file: FileInfo, rule_id: str) -> str:
    """Whether the rule `rule_id` is there and holds `file`, read from `GET /qos_requirements/{id}`.

    CONFIRMED: a 200 JSON object that names that rule or that file, and names no other rule or
    file. ABSENT: the provider said it is not there (a 404, or a 4xx with `enoent` or
    `notFound`), or the id cannot be one path segment (it names no rule). UNREADABLE: anything
    else (a timeout, a 5xx, a refusal, a redirect, another shape, an answer that names another
    file or rule): the rule may well be there, so it is not placed again."""
    try:
        # One encoded path segment; an id that cannot be one names no rule (ValueError).
        segment = id_segment(rule_id, "QoS requirement id")
    except ValueError:
        return ABSENT
    try:
        response = await run.call_in_time(
            request, get_oneprovider_config(), "GET", f"/qos_requirements/{segment}"
        )
    except TimeoutError:
        return UNREADABLE
    except OnedataApiError as exc:
        return ABSENT if exc.kind is None and _is_gone(exc) else UNREADABLE
    body = response.get("body") if isinstance(response, dict) else None
    if response.get("status_code") != 200 or not isinstance(body, dict):
        return UNREADABLE
    named_rule, named_file = body.get("qosRequirementId"), body.get("fileId")
    if named_rule not in (None, rule_id) or named_file not in (None, file.file_id):
        return UNREADABLE  # it names another rule or another file: not this file's hold
    return CONFIRMED if (named_rule, named_file) != (None, None) else UNREADABLE


async def _reconfirm_holds(run: _Run, files: list[FileInfo], hold: _Hold) -> None:
    """Read every rule counted as held once more, for the answer: `held` counts only the
    rules confirmed here. A rule that is gone is listed as failed ("gone"); one unreadable or
    not read in time as not confirmed, with its id. Never raises."""
    held = [f for f in files if f.file_id in hold.held]
    results = await _bounded(
        CHECK_CONCURRENCY, held, lambda f: _rule_state(run, f, hold.held[f.file_id]), run.deadline
    )
    for f, state in zip(held, results, strict=True):
        if state == ABSENT:
            hold.fail(f, "gone")
        elif state != CONFIRMED:
            hold.fail(f, "not_confirmed", hold.held[f.file_id])


# One lock per file while its hold is read back or placed (process-wide): two calls at once on
# one file must not both find no rule and both place one. Weak, so a file's lock goes with the
# last call that uses it.
_placing: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


async def _hold_one(run: _Run, file: FileInfo, hold: _Hold) -> None:
    """Hold one file, under its lock (waiting for it is cut off by the budget): a remembered
    rule is read back and kept when confirmed, placed again only when the provider said it is
    gone, and neither when its read-back was unreadable (it may be there: no duplicate)."""
    lock = _placing.setdefault(file.file_id, asyncio.Lock())
    try:
        await run.within(lock.acquire())
    except TimeoutError:
        hold.fail(file, "out_of_time")
        return
    try:
        remembered = _rules.get(file.file_id)
        if remembered is not None:
            state = await _rule_state(run, file, remembered)
            if state == CONFIRMED:
                hold.held[file.file_id] = remembered
                return
            if state == UNREADABLE:
                hold.fail(file, "not_confirmed", remembered)
                return
            if _rules.get(file.file_id) == remembered:
                del _rules[file.file_id]  # the provider says it is gone: forget it
        if run.out_of_time():
            hold.fail(file, "out_of_time")
            return
        outcome, rule_id = await _place_one(run, file)
        if outcome == "held" and rule_id is not None:
            hold.held[file.file_id] = rule_id
        else:
            hold.fail(file, outcome)
    finally:
        lock.release()


async def _place(run: _Run, files: list[FileInfo]) -> _Hold:
    """Hold every file: a placement rule that is known to exist, kept in this call's result.

    The first file settles which expression form Onedata takes; the others follow it,
    PLACE_CONCURRENCY at a time. A file not reached in time is "out_of_time".
    """
    hold = _Hold("")
    if files:
        first, rest = files[0], files[1:]
        if run.out_of_time():
            hold.fail(first, "out_of_time")
        else:
            await _hold_one(run, first, hold)
        results = await _bounded(
            PLACE_CONCURRENCY, rest, lambda f: _hold_one(run, f, hold), run.deadline
        )
        for f, result in zip(rest, results, strict=True):
            if result is _SKIPPED:
                hold.fail(f, "out_of_time")
    hold.expression = _hold_expression(run.local_id)
    return hold


async def _poll_round(run: _Run, pending: list[FileInfo]) -> None:
    """Re-read each not-yet-ready file; confirm a candidate with its distribution."""

    async def one(f: FileInfo) -> None:
        try:
            attributes = await run.call_in_time(
                get_file_attributes, f.file_id, attributes=POLL_ATTRIBUTES
            )
            flag = attributes.get("isFullyReplicatedLocally")
            if isinstance(flag, bool):
                f.flag = flag
            size = attributes.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                f.size = max(f.size, size)
                f.size_known = True
            if f.flag is False:
                rule = _rules.get(f.file_id)
                if rule is None:
                    return
                if (await run.call_in_time(get_qos_requirement, rule)).get("status") != "fulfilled":
                    return
                body = await run.call_in_time(get_file_distribution, f.file_id)
                dist = parse_distribution(body, f.known_size)
                if dist.known and writes_complete(dist, run.local_id, f.origin):
                    await _nudge(f)
                return
            dist = parse_distribution(
                await run.call_in_time(get_file_distribution, f.file_id), f.known_size
            )
        except (OnedataApiError, TimeoutError):
            return  # a failed look is not a verdict; the next poll looks again
        if dist.known and is_ready(dist, run.local_id, f.origin, f.flag):
            f.dist = dist
            f.state = "ready"
            f.size = f.cover_size = f.local_bytes = dist.size
            f.size_known = True

    await _bounded(CHECK_CONCURRENCY, pending, one, run.deadline)


def ensure_timing(deadline_s: float) -> tuple[float, float]:
    """(budget, grace) of one ensure call: every phase (the provider's id, resolving, placing,
    waiting) ends within `budget`, the caller's effective budget `min(deadline_s, cap)`; the
    final read-back of files and holds then takes at most `grace`, ENSURE_GRACE_S scaled down
    with the cap when the operator lowers it (150 s and 20 s by default: 170 s in all)."""
    cap = max_wait_s()
    return min(float(deadline_s), cap), ENSURE_GRACE_S * cap / MAX_WAIT_DEFAULT_S


async def ensure_readiness(extent: Sequence[Mapping[str, Any]], deadline_s: int) -> dict[str, Any]:
    """Make the declared data ready at the local provider and hold them there.

    On one clock: the provider's id, resolve, place, poll, all within the caller's effective
    budget (`ensure_timing`: `min(deadline_s, max_wait_s())`, never above 150 s); then the
    final pass (re-confirm the files and the holds, classify the rest) within the grace (20 s
    at the default cap). The answer's `waited_s` is the call's time, measured at return.
    """
    if (
        not isinstance(deadline_s, int)
        or isinstance(deadline_s, bool)
        or not 1 <= deadline_s <= MAX_DEADLINE_S
    ):
        raise ValueError(f"deadline_s must be an integer from 1 to {MAX_DEADLINE_S}")
    items = _normalise_extent(extent)
    start = _monotonic()
    budget, grace = ensure_timing(deadline_s)
    work_end = start + budget  # no new work after this, only the final look
    hard_end = work_end + grace

    def elapsed() -> int:
        return int(round(_monotonic() - start))

    try:
        local_id = await _local_id_within(work_end)
    except TimeoutError:
        return _config_unavailable(elapsed())
    run = _Run(local_id, work_end)
    files, unresolved = await _resolve(run, items)
    hold = await _place(run, files)

    # Wait: poll every file not yet confirmed ready, until the budget is used up.
    pending = list(files)
    while pending:
        await _poll_round(run, pending)
        pending = [f for f in pending if f.state != "ready"]
        now = _monotonic()
        if not pending or now >= work_end:
            break
        await _sleep(min(POLL_INTERVAL_S, work_end - now))

    # A file confirmed ready during the wait is not ready for good: read every one of them
    # again in one final pass, and judge what failed it (and what was never confirmed) anew.
    # Every rule counted as held is read again too. This pass never raises: a read that
    # fails leaves its file unchecked, or its hold not confirmed.
    run.deadline = work_end + grace / 2
    await _reconfirm(run, [f for f in files if f.state == "ready"])
    await _reconfirm_holds(run, files, hold)
    run.deadline = hard_end - grace / 20
    await _assess(run, [f for f in files if f.state is None], raise_errors=False)
    return _answer(files, unresolved, hold=hold, waited_s=elapsed())
