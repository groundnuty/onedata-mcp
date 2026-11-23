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
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from onedata_mcp.api.files import (
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
# One ensure_readiness call waits at most this (env below). With the grace, the whole call
# ends within 170 s, under the ~180 s a common MCP client allows a call (design/18).
MAX_WAIT_DEFAULT_S = 150
MAX_WAIT_CAP_S = 240  # never more, whatever the env says: the harness bounds a call at 300 s
MAX_WAIT_ENV = "ONEDATA_MCP_ENSURE_MAX_WAIT_S"
ENSURE_GRACE_S = 20.0  # the whole ensure call ends within max wait + this
CLASSIFY_RESERVE_S = 20.0  # tail of an ensure call kept for the final classification
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


def max_wait_s() -> float:
    """How long one ensure call waits at most: the env value, else 150 s, never above 240 s."""
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
    space: str  # space name
    root: str  # absolute directory path: /<space>[/dir...]
    files: list[str]  # as declared: matched exactly, never trimmed or normalised
    ranges: list[tuple[date, date]]
    # A "/" but no leading one: read as /<dataset> when its first segment is a space.
    misspelled: bool = False


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


def _check_dataset(dataset: str, space: str, where: str) -> None:
    """An empty, `.` or `..` component is refused: the provider would resolve it to another
    directory (`/my-dataset/..` is above every space)."""
    parts = dataset.strip("/").split("/") if dataset.strip("/") else [""]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(
            f"{where}.dataset {dataset!r} has an empty, '.' or '..' component: give a space "
            f"name such as '{space or 'my-dataset'}', or an absolute directory path inside it "
            "such as '/my-dataset/sub/dir'"
        )


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
        root = dataset if dataset.startswith("/") else "/" + dataset
        space = root.split("/")[1]
        _check_dataset(dataset, space, where)
        files = raw.get("files") or []
        if not isinstance(files, list) or not all(isinstance(f, str) and f for f in files):
            raise ValueError(f"{where}.files must be a list of file names or paths")
        if len(files) > MAX_FILES_PER_ITEM:
            raise ValueError(f"{where}.files may name at most {MAX_FILES_PER_ITEM} files")
        raw_ranges = raw.get("date_ranges") or []
        if not isinstance(raw_ranges, list):
            raise ValueError(f"{where}.date_ranges must be a list")
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
        misspelled = not dataset.startswith("/") and "/" in dataset
        # A name or a range declared twice is looked for once.
        unique = list(dict.fromkeys(files))
        named += len(unique)
        if named > MAX_FILES:
            raise ValueError(
                f"The extent names more than {MAX_FILES} files in all. "
                "Declare a smaller extent, or select files by date range."
            )
        items.append(_Item(dataset, space, root, unique, list(dict.fromkeys(ranges)), misspelled))
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
        self._space_ids: dict[str, str] = {}
        self._space_names: dict[str, str] = {}  # a space declared by its id -> its name
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

    async def space_id(self, space: str) -> str:
        if space not in self._space_ids:
            self._space_ids[space] = await self.within(self._space_id_from_provider(space))
        return self._space_ids[space]

    async def _space_id_from_provider(self, space: str) -> str:
        """A space's id from the local provider's own `GET /spaces`, matched by name or id.

        One provider call per tool call and no Onezone lookup, so a token valid only at the
        local provider resolves names too (the existing resolver also reads each space's
        details from Onezone).
        """
        if self._spaces is None:
            response = await self.call(request, get_oneprovider_config(), "GET", "/spaces")
            body = response["body"]
            if not isinstance(body, list):
                raise ValueError("the provider's list of spaces has an unexpected shape")
            self._spaces = [entry for entry in body if isinstance(entry, dict)]
        for entry in self._spaces:
            if entry.get("name") == space or entry.get("spaceId") == space:
                space_id = entry.get("spaceId")
                if isinstance(space_id, str) and space_id:
                    name = entry.get("name")
                    if entry.get("name") != space and isinstance(name, str) and name:
                        self._space_names[space] = name  # paths name a space by its name
                    return space_id
        names = sorted(e["name"] for e in self._spaces if isinstance(e.get("name"), str))
        # Only the spaces this provider supports for the token are listed: another space of
        # that name may still exist, so "not found" says where it was looked for.
        raise ValueError(
            f"your provider supports no space named {space!r} for this token "
            f"(the spaces it lists: {names})"
        )

    def space_name(self, space: str) -> str:
        """The name to put in a path for a space declared by name or by id."""
        return self._space_names.get(space, space)

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


def _misspelled(item: _Item) -> str:
    return (
        f"dataset '{item.dataset}' not found: give a space name (e.g. '{item.space}') "
        f"or an absolute directory path ('/{item.dataset}')."
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
        space_id = await run.space_id(item.space)
    except ValueError as exc:
        if item.misspelled:  # 'nosuch/sub': its first segment is not a space
            listing.problems.append(_misspelled(item))
        elif _looks_like_id(item.dataset):
            listing.problems.append(
                f"dataset '{item.dataset}' not found: it looks like an id, and this field "
                "takes a space name or an absolute directory path, such as '/my-dataset/sub'."
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
    # A space declared by its id is named by its name in paths (the lookup takes names).
    root = "/" + run.space_name(item.space) + item.root[len(item.space) + 1 :]
    listing.root = root
    try:
        root_id = await run.within(get_file_id(root))
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
    for path in dict.fromkeys(inaccessible):
        listing.problems.append(
            f"directory '{_absolute(root, path)}' in '{item.dataset}' is not listed "
            "(no access), so files under it cannot be checked. "
            "Declare files outside it, or get access."
        )
    return listing


def _select(item: _Item, files: list[FileInfo], unresolved: list[str]) -> list[FileInfo]:
    """The files an item declares; whatever it names that is not there goes to `unresolved`."""
    if not item.files and not item.ranges:
        if not files:
            unresolved.append(
                f"dataset '{item.dataset}' holds no files. "
                "Give a directory that holds the data, or name the files."
            )
        return files

    by_path = {f.path: f for f in files}
    by_name: dict[str, list[FileInfo]] = {}
    for f in files:
        by_name.setdefault(f.basename, []).append(f)

    chosen: dict[str, FileInfo] = {}
    for name in item.files:
        # Names are matched exactly as declared: no trimming, no normalising.
        if name.startswith("/"):
            found = by_path.get(name)
            matches = [found] if found else []
        elif "/" in name:
            found = by_path.get(_absolute(item.root, name))
            matches = [found] if found else []
        else:
            matches = by_name.get(name, [])
        if not matches and _looks_like_id(name):
            unresolved.append(
                f"file '{name}' in '{item.dataset}' not found: it looks like a file id, and "
                "files are given by their exact name or their absolute path."
            )
        elif not matches:
            unresolved.append(
                f"file '{name}' in '{item.dataset}' not found: "
                "give the exact file name, or its absolute path."
            )
        chosen.update((f.file_id, f) for f in matches)

    dated = [(f, name_date(f.basename)) for f in files] if item.ranges else []
    for start, end in item.ranges:
        matches = [f for f, day in dated if day is not None and start <= day <= end]
        if not matches:
            unresolved.append(
                f"date range {start.isoformat()}..{end.isoformat()} in '{item.dataset}' "
                "matched no file: dates are read from the YYYYMMDD in file names. "
                "Widen the range or name the files."
            )
        chosen.update((f.file_id, f) for f in matches)
    return list(chosen.values())


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
        for f in _select(named, listing.files, unresolved):
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


def _hold_failed_sentence(n: int, forbidden: bool) -> str:
    """Why a hold is missing, as far as the provider said. Only a refusal (403, `forbidden`,
    `eacces`, `eperm`) is called one; anything else (a timeout, another error, a rule that
    vanished) may pass, so the next call tries again."""
    text = f"A hold could not be placed for {_noun(n)}"
    if forbidden:
        return f"{text}: the provider refused this token the right to add QoS requirements."
    return f"{text}; the next call tries again."


def _advice(
    *,
    ready: bool,
    counts: Mapping[str, int],
    unresolved: int,
    unchecked: int,
    waited_s: int | None,
    hold_failed: int,
    hold_forbidden: bool = False,
) -> str:
    """One or two plain sentences: what the answer means. It names no tool (the other tool may
    not be offered alongside: design/10, gate 2).

    Never advice on what the agent does with the data.
    """
    ensure = waited_s is not None
    if ready:
        text = "Ready: all declared files are complete and readable at your provider now"
        if not ensure:
            return text + "."
        if hold_failed:
            return f"{text}. {_hold_failed_sentence(hold_failed, hold_forbidden)}"
        return text + ", and held there."

    sentences: list[str] = []
    dominant = max(counts, key=lambda r: (counts[r], -REASONS.index(r))) if counts else None
    if dominant is not None:
        sentences.append(_phrase(dominant, counts[dominant], ensure=ensure))
    if unresolved:
        what = "item was" if unresolved == 1 else "items were"
        sentences.append(f"{unresolved} declared {what} not resolved (see unresolved).")
    if unchecked:
        sentences.append(f"{_noun(unchecked)} could not be checked.")
    if hold_failed:
        sentences.append(_hold_failed_sentence(hold_failed, hold_forbidden))
    if ensure and dominant in ("being_written", "in_transfer", "at_other_site"):
        sentences.append("Call again to keep waiting.")
    lead = f"Not ready after {waited_s} s:" if ensure else "Not ready:"
    return f"{lead} {' '.join(sentences)}"


@dataclass
class _Hold:
    expression: str
    held_ids: set[str]  # files whose rule was confirmed to exist, or created
    failed: list[FileInfo]
    forbidden: bool = False  # the provider refused to create a rule (403, eacces, ...)

    @property
    def held(self) -> int:
        return len(self.held_ids)

    def ids(self, files: list[FileInfo]) -> list[str]:
        """The ids of the rules that hold the files, in the files' path order."""
        return [
            _rules[f.file_id]
            for f in sorted(files, key=lambda f: f.path)
            if f.file_id in self.held_ids and f.file_id in _rules
        ]


def _examples(files: Iterable[FileInfo]) -> list[str]:
    return sorted(f.path for f in files)[:MAX_EXAMPLES]


def _answer(
    files: list[FileInfo],
    unresolved: list[str],
    *,
    hold: _Hold | None = None,
    waited_s: int | None = None,
) -> dict[str, Any]:
    ready = bool(files) and not unresolved and all(f.state == "ready" for f in files)
    by_reason: dict[str, list[FileInfo]] = {}
    unchecked = 0
    for f in files:
        if f.state in REASONS:
            by_reason.setdefault(f.state, []).append(f)
        elif f.state != "ready":
            unchecked += 1

    answer: dict[str, Any] = {
        "ready": ready,
        "files": len(files),
        "bytes": sum(f.size for f in files),
    }
    if not ready:
        answer["bytes_local"] = sum(min(f.local_bytes, f.size) for f in files)
        if by_reason:
            answer["not_ready"] = {
                reason: {"files": len(by_reason[reason]), "examples": _examples(by_reason[reason])}
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
        hold_failed=len(hold.failed) if hold else 0,
        hold_forbidden=bool(hold and hold.forbidden),
    )
    if hold is not None:
        answer["waited_s"] = waited_s
        ids = hold.ids(files)
        # Each id is a QoS requirement that keeps its file here until it is removed: the
        # answer says which, so the holds can be released (design/18, decision 3).
        answer["held"] = {
            "files": hold.held,
            "rule": hold.expression,
            "qos_requirement_ids": ids[:MAX_HOLD_IDS_SHOWN],
        }
        if len(ids) > MAX_HOLD_IDS_SHOWN:
            answer["held"]["qos_requirement_ids_not_shown"] = len(ids) - MAX_HOLD_IDS_SHOWN
        if hold.failed:
            answer["hold_failed"] = {"files": len(hold.failed), "examples": _examples(hold.failed)}
    return answer


# ---------------------------------------------------------------------------
# check_readiness
# ---------------------------------------------------------------------------


def _config_unavailable(waited_s: int | None = None) -> dict[str, Any]:
    """The answer when the provider's own id could not be read in time: nothing was checked."""
    answer: dict[str, Any] = {
        "ready": False,
        "files": 0,
        "bytes": 0,
        "bytes_local": 0,
        "unresolved": [],
    }
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


async def check_readiness(extent: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Whether the declared data are ready at the local provider, or why not. Read-only."""
    items = _normalise_extent(extent)
    deadline = _monotonic() + CHECK_BUDGET_S
    try:
        local_id = await _local_id_within(deadline)
    except TimeoutError:
        return _config_unavailable()
    run = _Run(local_id, deadline)
    files, unresolved = await _resolve(run, items)
    await _assess(run, files)
    return _answer(files, unresolved)


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


_REFUSED = "refused"  # `_place_one`'s answer when the provider refused the token the right


async def _place_one(run: _Run, file: FileInfo) -> bool | str:
    """Add the placement rule for a file; falls back to the plain expression on a 400.

    True when the rule was created; `_REFUSED` when the provider refused the token (403,
    `forbidden`, `eacces`, `eperm`); False otherwise (a timeout, another error, no id)."""
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
            return False
        except OnedataApiError as exc:
            status = exc.response.get("status_code") if exc.response else None
            if _is_forbidden(exc):
                return _REFUSED
            if status == 400 and index + 1 < len(templates):
                continue  # Onedata rejected the expression form: try the plain one
            return False
        rule_id = created.get("qosRequirementId") if isinstance(created, dict) else None
        if not isinstance(rule_id, str) or not rule_id:
            return False
        _rules[file.file_id] = rule_id
        if index == 1:
            _hold_plain = True  # the primary form was rejected, the plain one accepted
        return True
    return False


async def _rule_confirmed(run: _Run, file: FileInfo) -> bool:
    """The remembered rule of a file exists: `GET /qos_requirements/{id}` answered 200 with
    a JSON object that names that rule, or that file. A redirect, a page of HTML, an empty
    answer or any other shape does not confirm it."""
    rule_id = _rules.get(file.file_id)
    if rule_id is None:
        return False
    try:
        # One encoded path segment; an id that cannot be one is not confirmed (ValueError).
        segment = id_segment(rule_id, "QoS requirement id")
        response = await run.call_in_time(
            request, get_oneprovider_config(), "GET", f"/qos_requirements/{segment}"
        )
    except (OnedataApiError, TimeoutError, ValueError):
        return False
    body = response.get("body") if isinstance(response, dict) else None
    return (
        response["status_code"] == 200
        and isinstance(body, dict)
        and (body.get("qosRequirementId") == rule_id or body.get("fileId") == file.file_id)
    )


async def _reconfirm_holds(run: _Run, files: list[FileInfo], hold: _Hold) -> None:
    """Read every rule counted as held once more, for the answer: `held` counts only the
    rules confirmed here. A rule that is gone, unreadable or not read in time is listed with
    the failed ones. Never raises."""
    held = [f for f in files if f.file_id in hold.held_ids]
    results = await _bounded(
        CHECK_CONCURRENCY, held, lambda f: _rule_confirmed(run, f), run.deadline
    )
    for f, ok in zip(held, results, strict=True):
        if ok is not True:
            hold.held_ids.discard(f.file_id)
            hold.failed.append(f)


async def _place(run: _Run, files: list[FileInfo]) -> _Hold:
    """Hold every file: a placement rule that is known to exist.

    A rule remembered from an earlier call is read back first (concurrently, within the
    limit): an agent with `remove_qos_requirement` may have deleted it. If it is gone, or
    cannot be read, the rule is created again. `held` counts only rules confirmed or
    created in this call; files not confirmed or created (failed, or out of time) are listed
    as failed.
    """
    held: set[str] = set()
    failed: list[FileInfo] = []
    refused = False
    cached = [f for f in files if f.file_id in _rules]

    confirmed = await _bounded(
        CHECK_CONCURRENCY, cached, lambda f: _rule_confirmed(run, f), run.deadline
    )
    todo = [f for f in files if f.file_id not in _rules]
    for f, result in zip(cached, confirmed, strict=True):
        if result is True:
            held.add(f.file_id)
        elif result is _SKIPPED:
            failed.append(f)
        else:
            todo.append(f)

    if todo:
        # The first rule settles which expression form Onedata takes; the rest follow it.
        first, rest = todo[0], todo[1:]
        outcome = await _place_one(run, first)
        results = await _bounded(
            PLACE_CONCURRENCY, rest, lambda f: _place_one(run, f), run.deadline
        )
        for f, ok in zip([first, *rest], [outcome, *results], strict=True):
            if ok is True:
                held.add(f.file_id)
            else:
                failed.append(f)
                refused = refused or ok == _REFUSED
    return _Hold(_hold_expression(run.local_id), held, failed, refused)


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


async def ensure_readiness(extent: Sequence[Mapping[str, Any]], deadline_s: int) -> dict[str, Any]:
    """Make the declared data ready at the local provider and hold them there.

    One call waits at most `min(deadline_s, max_wait_s())`, counted from the call's start
    (resolving and placing use up part of it), and the whole call ends within
    `max_wait_s() + ENSURE_GRACE_S` (170 s by default), with `max_wait_s()` never above
    240 s. On one clock:
    resolve, place, poll, re-confirm, classify, answer.
    """
    if (
        not isinstance(deadline_s, int)
        or isinstance(deadline_s, bool)
        or not 1 <= deadline_s <= MAX_DEADLINE_S
    ):
        raise ValueError(f"deadline_s must be an integer from 1 to {MAX_DEADLINE_S}")
    items = _normalise_extent(extent)
    start = _monotonic()
    max_wait = max_wait_s()
    hard_end = start + max_wait + ENSURE_GRACE_S
    work_end = hard_end - CLASSIFY_RESERVE_S  # no new work after this, only the final look
    wait_end = min(start + min(deadline_s, max_wait), work_end)

    try:
        local_id = await _local_id_within(work_end)
    except TimeoutError:
        return _config_unavailable(int(round(_monotonic() - start)))
    run = _Run(local_id, work_end)
    files, unresolved = await _resolve(run, items)
    hold = await _place(run, files)

    # Wait: poll every file not yet confirmed ready.
    run.deadline = wait_end
    pending = list(files)
    while pending:
        await _poll_round(run, pending)
        pending = [f for f in pending if f.state != "ready"]
        now = _monotonic()
        if not pending or now >= wait_end:
            break
        await _sleep(min(POLL_INTERVAL_S, wait_end - now))
    waited_s = int(round(_monotonic() - start))

    # A file confirmed ready during the wait is not ready for good: read every one of them
    # again in one final pass, and judge what failed it (and what was never confirmed) anew.
    # Every rule counted as held is read again too. This pass never raises: a read that
    # fails leaves its file unchecked.
    run.deadline = hard_end - CLASSIFY_RESERVE_S / 2
    await _reconfirm(run, [f for f in files if f.state == "ready"])
    await _reconfirm_holds(run, files, hold)
    run.deadline = hard_end - 1
    await _assess(run, [f for f in files if f.state is None], raise_errors=False)
    return _answer(files, unresolved, hold=hold, waited_s=waited_s)
