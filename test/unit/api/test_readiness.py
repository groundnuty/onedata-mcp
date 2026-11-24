"""Unit tests for the readiness logic (check_readiness, ensure_readiness).

The pure rules are tested directly. The tools are tested against one stateful fake
Oneprovider + Onezone behind pytest-httpx, answering in the shapes of Onedata 25.0
(oneprovider-swagger: data_reg_distribution, recursive_file_list, qos_requirement,
transfer_status; onezone-swagger: providerdetails). Time runs on a fake clock, so a
150 s wait takes no real time.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote

import httpx
import pytest
from pytest_httpx import HTTPXMock

from onedata_mcp.api import readiness
from onedata_mcp.api.readiness import Distribution
from test.unit.object_ids import object_id

LOCAL = "pLocal"
OWNER = "pOwner"
OTHER = "pOther"
SPACE = "my-dataset"
SPACE_ID = "s1"
HOLD = f"providerId={LOCAL} & anyStorage"
HOLD_PLAIN = f"providerId={LOCAL}"
PROVIDER = "https://provider.example/api/v3/oneprovider"
ZONE = "https://onezone.example/api/v3/onezone"


# ---------------------------------------------------------------------------
# The fake federation
# ---------------------------------------------------------------------------


def _error(status: int, error_id: str, errno: str | None = None) -> httpx.Response:
    error: dict = {"id": error_id, "description": error_id}
    if errno:
        error["details"] = {"errno": errno}
    return httpx.Response(status, json={"error": error})


@dataclass
class FakeFile:
    fid: str
    rel: str  # path under the space root
    size: int
    origin: str
    blocks: dict[str, list[list[int]]]
    # a provider's own view of the size (None: it does not say)
    logical: dict[str, int | None] = field(default_factory=dict)
    virtual: dict[str, int] = field(default_factory=dict)  # a provider's `virtualSize`, if any

    @property
    def path(self) -> str:
        return f"/{SPACE}/{self.rel}"

    def local_full(self) -> bool:
        return sum(length for _, length in self.blocks.get(LOCAL, [])) >= self.size


class Clock:
    """A fake monotonic clock; `sleep` advances it and may run a hook (to flip the fake)."""

    def __init__(self) -> None:
        self.start = self.now = 1000.0
        self.sleeps = 0
        self.after_sleep: Callable[[int], None] | None = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.after_sleep:
            self.after_sleep(self.sleeps)

    @property
    def elapsed(self) -> float:
        return self.now - self.start


class Fed:
    """One Oneprovider (LOCAL) and its Onezone, with a space, files, transfers and QoS rules."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.files: dict[str, FakeFile] = {}
        self.online = {OWNER: True, OTHER: True}
        self.failed: set[str] = set()  # providers whose distribution entry fails
        self.zone_providers_fail = False
        self.zone_status: int | None = None  # every Onezone request answers this status
        # the provider's own `GET /spaces` (Onedata 25.0 shape: name, spaceId, providers, ...)
        self.spaces: list[dict] = [
            {
                "name": SPACE,
                "spaceId": SPACE_ID,
                "providers": [
                    {"providerId": LOCAL, "providerName": "local"},
                    {"providerId": OWNER, "providerName": "owner"},
                ],
                "fileId": "root-file-id",
                "dirId": "root-file-id",
                "trashDirId": "trash-file-id",
                "archivesDirId": "archives-file-id",
            }
        ]
        self.transfers: dict[str, dict] = {}
        self.rules: dict[str, dict] = {}
        self.qos_status: Callable[[str, str], int] = lambda fid, expression: 201
        self.latency = 0.0  # fake seconds each request takes
        self.requests: list[tuple[str, str]] = []
        self.unexpected: list[str] = []
        self.inflight: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.total = 0  # requests in flight, all routes together
        self.total_peak = 0
        self.listing_pages = 0
        # opaque directory id -> path; the space's own directory is listed by its `dirId`
        self.dirs: dict[str, str] = {"root-file-id": f"/{SPACE}"}
        self.qos_get_override: Callable[[str], httpx.Response] | None = None
        self.hang: set[str] = set()  # routes that never answer (they sleep 10 real seconds)

    # --- building -------------------------------------------------------

    def add(
        self,
        rel: str,
        size: int = 100,
        *,
        local: str | list | None = None,
        owner: str | list | None = None,
        other: str | list | None = None,
        origin: str = OWNER,
    ) -> FakeFile:
        def blocks(spec: str | list | None) -> list[list[int]]:
            if spec == "full":
                return [[0, size]]
            if spec == "half":
                return [[0, size // 2]]
            return spec or []  # type: ignore[return-value]

        fid = f"fid{len(self.files):06d}"
        f = FakeFile(
            fid,
            rel,
            size,
            origin,
            {LOCAL: blocks(local), OWNER: blocks(owner), OTHER: blocks(other)},
        )
        self.files[fid] = f
        return f

    def by_rel(self, rel: str) -> FakeFile:
        return next(f for f in self.files.values() if f.rel == rel)

    # --- routing --------------------------------------------------------

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        route, response_fn = self._route(request)
        self.requests.append((request.method, request.url.path))
        self.inflight[route] = self.inflight.get(route, 0) + 1
        self.peak[route] = max(self.peak.get(route, 0), self.inflight[route])
        self.total += 1
        self.total_peak = max(self.total_peak, self.total)
        await asyncio.sleep(10 if route in self.hang else 0)
        try:
            return response_fn()
        finally:
            self.inflight[route] -= 1
            self.total -= 1
            self.clock.now += self.latency

    def _route(self, request: httpx.Request) -> tuple[str, Callable[[], httpx.Response]]:
        url = str(request.url)
        method = request.method
        body = json.loads(request.content) if request.content else {}

        if url.startswith(ZONE + "/"):
            rest = url[len(ZONE) :]
            if self.zone_status is not None:
                return "zone_any", lambda: _error(self.zone_status, "unauthorized")
            if m := re.fullmatch(r"/providers/([^/?]+)", rest):
                return "zone_provider", lambda: self._zone_provider(m.group(1))
            # Onezone has nothing else to tell: spaces come from the provider's own list
        elif url.startswith(PROVIDER + "/"):
            rest = url[len(PROVIDER) :]
            path = request.url.path[len("/api/v3/oneprovider") :]
            if method == "GET" and rest == "/configuration":
                return "config", lambda: httpx.Response(
                    200, json={"providerId": LOCAL, "name": "local", "version": "25.0.0"}
                )
            if method == "GET" and path == "/spaces":
                return "spaces", lambda: httpx.Response(200, json=self.spaces)
            if method == "POST" and path.startswith("/lookup-file-id/"):
                return "lookup", lambda: self._lookup(unquote(path[len("/lookup-file-id/") :]))
            if m := re.fullmatch(r"/data/([^/]+)/files", path):
                return "listing", lambda: self._listing(m.group(1), body)
            if m := re.fullmatch(r"/data/([^/]+)/distribution", path):
                return "distribution", lambda: self._distribution(m.group(1))
            if m := re.fullmatch(r"/data/([^/]+)", path):
                return "attributes", lambda: self._attributes(m.group(1), body)
            if m := re.fullmatch(r"/spaces/([^/]+)/transfers", path):
                return "transfer_list", lambda: self._transfer_list()
            if m := re.fullmatch(r"/transfers/([^/]+)", path):
                return "transfer", lambda: self._transfer(m.group(1))
            if method == "POST" and path == "/qos_requirements":
                return "qos_post", lambda: self._qos_post(body)
            if m := re.fullmatch(r"/qos_requirements/([^/]+)", path):
                return "qos_get", lambda: self._qos_get(m.group(1))
        self.unexpected.append(f"{method} {url}")
        return "unexpected", lambda: _error(404, "notFound")

    # --- handlers (shapes: Onedata 25.0 swagger) --------------------------

    def _zone_provider(self, pid: str) -> httpx.Response:
        if self.zone_providers_fail:
            return _error(500, "internalServerError")
        return httpx.Response(
            200,
            json={
                "providerId": pid,
                "name": pid,
                "domain": f"{pid}.example",
                "latitude": 50.0,
                "longitude": 19.9,
                "clusterId": pid,
                "online": self.online.get(pid, True),
                "creationTime": 1576152793,
            },
        )

    def _lookup(self, path: str) -> httpx.Response:
        root = f"/{SPACE}"
        if path == root or (
            path.startswith(root + "/")
            and any(f.path.startswith(path + "/") for f in self.files.values())
        ):
            dir_id = f"dirid{len(self.dirs)}"
            self.dirs[dir_id] = path
            return httpx.Response(200, json={"fileId": dir_id})
        return _error(400, "posix", "enoent")

    def attrs(self, f: FakeFile) -> dict:
        local = sum(length for _, length in f.blocks[LOCAL])
        return {
            "fileId": f.fid,
            "path": f.path,
            "name": f.rel.rsplit("/", 1)[-1],
            "type": "REG",
            "size": f.size,
            "isFullyReplicatedLocally": f.local_full(),
            "localReplicationRate": (local / f.size) if f.size else 1.0,
            "originProviderId": f.origin,
        }

    def _listing(self, root_id: str, body: dict) -> httpx.Response:
        self.listing_pages += 1
        root = self.dirs[root_id]
        under = sorted(
            (f for f in self.files.values() if f.path.startswith(root + "/")),
            key=lambda f: f.path,
        )
        limit = body.get("limit", 1000)
        offset = int(body.get("token") or 0)
        page = under[offset : offset + limit]
        last = offset + limit >= len(under)
        entries = []
        for f in page:
            full = self.attrs(f)
            entry = {a: full[a] for a in body["attributes"] if a in full}
            if "path" in entry:  # the listing answers paths relative to the listed directory
                entry["path"] = f.path[len(root) + 1 :]
            entries.append(entry)
        return httpx.Response(
            200,
            json={
                "files": entries,
                "isLast": last,
                "nextPageToken": None if last else str(offset + limit),
            },
        )

    def _distribution(self, fid: str) -> httpx.Response:
        f = self.files.get(fid)
        if f is None:
            return _error(400, "posix", "enoent")
        per_provider: dict = {}
        for pid in (LOCAL, OWNER, OTHER):
            if pid in self.failed:
                per_provider[pid] = {
                    "success": False,
                    "error": {
                        "description": "Operation failed with POSIX error: enoent.",
                        "details": {"errno": "enoent"},
                    },
                }
                continue
            blocks = f.blocks[pid]
            logical = f.logical.get(pid, f.size)
            per_provider[pid] = {
                "success": True,
                **({"logicalSize": logical} if logical is not None else {}),
                **({"virtualSize": f.virtual[pid]} if pid in f.virtual else {}),
                "locationsPerStorageBackend": {
                    f"st-{pid}": {"success": True, "location": "/loc" if blocks else None}
                },
                "distributionPerStorageBackend": {
                    f"st-{pid}": {
                        "success": True,
                        "blocks": blocks,
                        "physicalSize": sum(length for _, length in blocks),
                    }
                },
            }
        return httpx.Response(200, json={"type": "REG", "distributionPerProvider": per_provider})

    def _attributes(self, fid: str, body: dict) -> httpx.Response:
        f = self.files.get(fid)
        if f is None:
            return _error(400, "posix", "enoent")
        full = self.attrs(f)
        return httpx.Response(200, json={a: full[a] for a in body["attributes"] if a in full})

    def _transfer_list(self) -> httpx.Response:
        return httpx.Response(200, json={"transfers": list(self.transfers), "nextPageToken": None})

    def _transfer(self, tid: str) -> httpx.Response:
        t = self.transfers[tid]
        return httpx.Response(
            200,
            json={
                "type": "replication",
                "spaceId": SPACE_ID,
                "dataSourceType": "file",
                "fileId": t.get("fileId"),
                "filePath": t.get("filePath"),
                **(
                    {"transferStatus": t.get("status", "replicating")} if t.get("status", 1) else {}
                ),
                "replicatingProviderId": t["replicatingProviderId"],
                "evictingProviderId": None,
            },
        )

    def _qos_post(self, body: dict) -> httpx.Response:
        status = self.qos_status(body["fileId"], body["expression"])
        if status >= 400:
            return _error(status, "qosRequestFailed")
        qid = f"q{len(self.rules):06d}"
        self.rules[qid] = {**body, "status": "pending"}
        return httpx.Response(
            201, json={"qosRequirementId": qid}, headers={"Location": f"/qos_requirements/{qid}"}
        )

    def _qos_get(self, qid: str) -> httpx.Response:
        if self.qos_get_override is not None:
            return self.qos_get_override(qid)
        rule = self.rules.get(qid)
        if rule is None:  # removed, for instance by an agent with remove_qos_requirement
            return _error(404, "notFound")
        return httpx.Response(
            200,
            json={
                "qosRequirementId": qid,
                "fileId": rule["fileId"],
                "expression": rule["expression"],
                "replicasNum": rule["replicasNum"],
                "status": rule["status"],
            },
        )

    def rule_bodies(self) -> list[dict]:
        return [{k: v for k, v in r.items() if k != "status"} for r in self.rules.values()]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", "https://provider.example")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "token-p")
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://onezone.example")
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", "token-z")
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    monkeypatch.delenv(readiness.MAX_WAIT_ENV, raising=False)
    monkeypatch.delenv("ONEDATA_MCP_NODE_NAMES", raising=False)
    readiness.reset_state()
    yield
    readiness.reset_state()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(readiness, "_monotonic", clock)
    monkeypatch.setattr(readiness, "_sleep", clock.sleep)
    return clock


@pytest.fixture
def fed(httpx_mock: HTTPXMock, clock: Clock) -> Fed:
    fake = Fed(clock)
    httpx_mock.add_callback(fake, is_reusable=True, is_optional=True)
    yield fake
    assert fake.unexpected == []


def listing_incomplete(dataset: str = SPACE) -> str:
    return (
        f"dataset '{dataset}': listing incomplete, not every page arrived. "
        "Call again, or give a smaller directory."
    )


def out_of_time(dataset: str = SPACE) -> str:
    return (
        f"dataset '{dataset}': listing did not finish within the time budget. "
        "Call again, or give a smaller directory."
    )


def no_such_directory(dataset: str) -> str:
    return (
        f"dataset '{dataset}' not found at your provider: no such directory, or one this "
        "token cannot see. Give a space name or an existing absolute directory path."
    )


def no_files(dataset: str = SPACE) -> str:
    return (
        f"dataset '{dataset}' holds no files. "
        "Give a directory that holds the data, or name the files."
    )


def file_not_found(name: str, dataset: str = SPACE) -> str:
    return (
        f"file '{name}' in '{dataset}' not found: give the exact file name, or its absolute path."
    )


def range_matched_nothing(start: str, end: str, dataset: str = SPACE) -> str:
    return (
        f"date range {start}..{end} in '{dataset}' matched no file: "
        "dates are read from the YYYYMMDD in file names. Widen the range or name the files."
    )


def no_access(path: str, dataset: str = SPACE) -> str:
    return (
        f"directory '{path}' in '{dataset}' is not listed (no access), "
        "so files under it cannot be checked. Declare files outside it, or get access."
    )


def misspelled(dataset: str, space: str) -> str:
    """A dataset whose first segment is no space: the hint names the spaces there are (Gemini
    10: it used to suggest the segment that was not found)."""
    return (
        f"dataset '{dataset}' not found: your provider supports no space named '{space}' for "
        f"this token (the spaces it lists: ['{SPACE}']). Give one of those names, or an absolute "
        "directory path inside one."
    )


def patch_listing(fed: Fed, change: Callable[[dict], dict]) -> None:
    """Make the fake's listing answer `change(page)` instead of the page it built."""
    original = fed._listing
    fed._listing = lambda root_id, body: httpx.Response(  # type: ignore[method-assign]
        200, json=change(json.loads(original(root_id, body).content))
    )


def held_summary(result: dict) -> dict:
    """`held` without its rule ids (tested on their own below)."""
    return {k: v for k, v in result["held"].items() if k in ("files", "rule")}


def extent(**item) -> list[dict]:
    return [{"dataset": SPACE, **item}]


async def check(**item) -> dict:
    return await readiness.check_readiness(extent(**item))


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("data_20180801.nc", date(2018, 8, 1)),
        ("20180801_example_product_P1D-m.nc", date(2018, 8, 1)),
        ("a_20180801_b_20190102.nc", date(2018, 8, 1)),  # the first date wins
        ("a_20181301_b_20190102.nc", date(2019, 1, 2)),  # an invalid run is skipped
        ("data_2018080.nc", None),  # 7 digits
        ("data_201808011.nc", None),  # 9 digits: not exactly 8
        ("data_20180230.nc", None),  # not a calendar date
        ("data.nc", None),
        ("", None),
    ],
)
def test_name_date(name: str, expected: date | None) -> None:
    assert readiness.name_date(name) == expected


def test_covers_union_gaps_and_size_zero() -> None:
    assert readiness.covers([[0, 4], [4, 6]], 10)
    assert readiness.covers([[0, 6], [4, 6]], 10)  # overlap
    assert readiness.covers([[5, 5], [0, 5]], 10)  # unsorted
    assert readiness.covers([[0, 20]], 10)  # a block past the end
    assert not readiness.covers([[0, 4], [6, 4]], 10)  # a gap
    assert not readiness.covers([[0, 9]], 10)
    assert not readiness.covers([], 10)
    assert readiness.covers([], 0)  # a size-0 file is covered by any provider that lists it
    assert not readiness.covers([[0, 0], ["x"], None, [1]], 10)  # malformed entries are ignored
    assert readiness.covered_bytes([[0, 4], [2, 4], [8, 1]], 10) == 7


def _body(entries: dict) -> dict:
    return {"type": "REG", "distributionPerProvider": entries}


def _entry(size: int, *storages: list[list[int]]) -> dict:
    return {
        "success": True,
        "logicalSize": size,
        "distributionPerStorageBackend": {
            f"s{i}": {"success": True, "blocks": blocks, "physicalSize": 0}
            for i, blocks in enumerate(storages)
        },
    }


def test_parse_distribution_unions_storages_and_keeps_failures_apart() -> None:
    dist = readiness.parse_distribution(
        _body(
            {
                LOCAL: _entry(10, [[0, 5]], [[5, 5]]),  # two storages together cover it
                OWNER: _entry(10, [[0, 3]]),
                OTHER: {"success": False, "error": {"description": "down"}},
            }
        )
    )
    assert dist.size == 10
    assert dist.covering == {LOCAL}
    assert dist.holders == {LOCAL, OWNER}
    assert dist.failed == {OTHER}
    assert dist.covered == {LOCAL: 10, OWNER: 3}


def test_parse_distribution_takes_the_largest_logical_size() -> None:
    """A provider whose size has not synced must not make a half-written file look complete."""
    dist = readiness.parse_distribution(_body({LOCAL: _entry(0), OWNER: _entry(100, [[0, 50]])}))
    assert dist.size == 100
    assert dist.covering == frozenset()


def test_parse_distribution_failed_storage_marks_the_provider_failed() -> None:
    entry = _entry(10, [[0, 5]])
    entry["distributionPerStorageBackend"]["s9"] = {"success": False, "error": {}}
    dist = readiness.parse_distribution(_body({OWNER: entry}))
    assert dist.failed == {OWNER}
    assert dist.covering == frozenset()


def test_parse_distribution_survives_junk() -> None:
    assert readiness.parse_distribution(None).covering == frozenset()
    assert readiness.parse_distribution({"distributionPerProvider": []}).size == 0


def _dist(size: int = 10, **provider_blocks: list[list[int]] | None) -> Distribution:
    entries: dict = {}
    for pid, blocks in provider_blocks.items():
        entries[pid] = {"success": False} if blocks is None else _entry(size, blocks)
    return readiness.parse_distribution(_body(entries))


@pytest.mark.parametrize(
    ("dist", "origin", "writes_complete"),
    [
        (_dist(pLocal=[[0, 10]], pOwner=[[0, 10]]), OWNER, True),
        (_dist(pLocal=[[0, 10]], pOwner=[[0, 5]]), OWNER, False),  # local full, no other full
        (_dist(pLocal=[[0, 10]]), OWNER, False),
        (_dist(pLocal=[[0, 10]]), LOCAL, True),  # written here: the exception
        (_dist(pLocal=[[0, 5]]), LOCAL, False),
        (_dist(pLocal=[], pOwner=[[0, 10]]), OWNER, True),
    ],
)
def test_writes_complete(dist: Distribution, origin: str, writes_complete: bool) -> None:
    assert readiness.writes_complete(dist, LOCAL, origin) is writes_complete


def test_ready_needs_the_flag_not_to_be_false() -> None:
    dist = _dist(pLocal=[[0, 10]], pOwner=[[0, 10]])
    assert readiness.is_ready(dist, LOCAL, OWNER, True)
    assert readiness.is_ready(dist, LOCAL, OWNER, None)
    assert not readiness.is_ready(dist, LOCAL, OWNER, False)


@pytest.mark.parametrize(
    ("dist", "kwargs", "reason"),
    [
        # S0: complete at the owner, nothing local
        (_dist(pLocal=[], pOwner=[[0, 10]]), {}, "at_other_site"),
        # S2: owner partial, preallocated at the final size
        (_dist(pLocal=[], pOwner=[[0, 5]]), {}, "being_written"),
        # local full but no other provider full: not ready
        (_dist(pLocal=[[0, 10]], pOwner=[[0, 5]]), {}, "being_written"),
        # local partial with something moving it here
        (_dist(pLocal=[[0, 5]], pOwner=[[0, 10]]), {"moving": True}, "in_transfer"),
        (_dist(pLocal=[[0, 5]], pOwner=[[0, 5]]), {"moving": True}, "being_written"),
        # owner down: entry failed
        (_dist(pLocal=[], pOwner=None), {}, "unreachable"),
        # owner offline per Onezone, complete there
        (_dist(pLocal=[], pOwner=[[0, 10]]), {"offline": [OWNER]}, "unreachable"),
        # owner offline, partial blocks there
        (_dist(pLocal=[], pOwner=[[0, 5]]), {"offline": [OWNER]}, "unreachable"),
        # offline but holds nothing: it is not why the file is missing
        (_dist(pLocal=[], pOwner=[], pOther=[[0, 10]]), {"offline": [OWNER]}, "at_other_site"),
        # another online provider has it all
        (
            _dist(pLocal=[], pOwner=[[0, 10]], pOther=[[0, 10]]),
            {"offline": [OWNER]},
            "at_other_site",
        ),
        # an unreadable provider does not hide that the owner is still writing
        (_dist(pLocal=[], pOwner=[[0, 5]], pOther=None), {}, "being_written"),
        # ...but with nobody known to hold blocks, an unreadable provider may hold them all
        (_dist(pLocal=[], pOwner=[], pOther=None), {}, "unreachable"),
        # a flag of false rules readiness out
        (_dist(pLocal=[[0, 10]], pOwner=[[0, 10]]), {"flag": False}, "at_other_site"),
    ],
)
def test_classify_first_matching_reason(dist: Distribution, kwargs: dict, reason: str) -> None:
    assert readiness.classify(dist, LOCAL, OWNER, **kwargs) == reason


def test_classify_ready() -> None:
    assert readiness.classify(_dist(pLocal=[[0, 10]], pOwner=[[0, 10]]), LOCAL, OWNER) is None
    # written here and complete here
    assert readiness.classify(_dist(pLocal=[[0, 10]]), LOCAL, LOCAL) is None


# ---------------------------------------------------------------------------
# check_readiness against the fake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_ready(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("b.nc", 50, local="full", owner="full")

    result = await check()

    assert result == {
        "ready": True,
        "resolved_files": 2,
        "ready_files": 2,
        "bytes": 150,
        "advice": "Ready: all declared files are complete and readable at your provider now.",
    }
    # check is read-only: no rule is created
    assert not any(p.endswith("/qos_requirements") for _, p in fed.requests)
    assert fed.rules == {}


@pytest.mark.asyncio
async def test_check_s0_complete_at_the_owner_nothing_local(fed: Fed) -> None:
    fed.add("a.nc", 100, owner="full")

    result = await check()

    assert result["ready"] is False
    assert result["not_ready"] == {"at_other_site": {"files": 1, "examples": [f"/{SPACE}/a.nc"]}}
    assert result["unresolved"] == []
    assert result["bytes_local"] == 0
    assert result["advice"] == (
        "Not ready: 1 file is complete at another provider and not yet at yours."
    )


@pytest.mark.asyncio
async def test_check_s2_owner_partial_preallocated_is_being_written(fed: Fed) -> None:
    for i in range(14):
        fed.add(f"data_201808{i + 10:02d}.nc", 100, owner="half")
    fed.add("ok.nc", 100, local="full", owner="full")

    result = await check()

    assert result["ready"] is False
    assert result["resolved_files"] == 15
    assert result["not_ready"] == {
        "being_written": {
            "files": 14,
            "examples": [
                f"/{SPACE}/data_20180810.nc",
                f"/{SPACE}/data_20180811.nc",
                f"/{SPACE}/data_20180812.nc",
            ],  # at most 3, in path order
            "examples_not_shown": 11,  # truncation said, not left to be guessed (Codex 13)
        }
    }
    assert result["bytes"] == 1500
    assert result["bytes_local"] == 100
    assert result["advice"] == (
        "Not ready: 14 files are still being written at their owner, so their bytes are incomplete."
    )


@pytest.mark.asyncio
async def test_check_local_full_but_no_other_provider_full_is_not_ready(fed: Fed) -> None:
    """Local blocks are not trusted alone: a read of an unwritten range may be zero-filled."""
    fed.add("a.nc", 100, local="full", owner="half")

    result = await check()

    assert result["ready"] is False
    assert list(result["not_ready"]) == ["being_written"]
    assert result["bytes_local"] == 100


@pytest.mark.asyncio
async def test_check_local_full_and_written_here_is_ready(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", origin=LOCAL)

    assert (await check())["ready"] is True


@pytest.mark.asyncio
async def test_check_local_partial_with_a_pending_rule_is_in_transfer(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local="half", owner="full")
    fed.rules["q-mine"] = {
        "fileId": f.fid,
        "expression": HOLD,
        "replicasNum": 1,
        "status": "pending",
    }
    readiness._rules[f.fid] = "q-mine"

    result = await check()

    assert result["not_ready"] == {"in_transfer": {"files": 1, "examples": [f.path]}}
    assert result["bytes_local"] == 50
    assert result["advice"].startswith("Not ready: 1 file is on the way to your provider.")


@pytest.mark.parametrize("status", ["fulfilled", "impossible"])
@pytest.mark.asyncio
async def test_check_a_satisfied_or_impossible_rule_is_not_moving(fed: Fed, status: str) -> None:
    f = fed.add("a.nc", 100, local="half", owner="full")
    fed.rules["q-mine"] = {"fileId": f.fid, "expression": HOLD, "replicasNum": 1, "status": status}
    readiness._rules[f.fid] = "q-mine"

    assert list((await check())["not_ready"]) == ["at_other_site"]


@pytest.mark.asyncio
async def test_check_a_rule_this_process_did_not_create_does_not_count(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local="half", owner="full")
    fed.rules["q-other"] = {
        "fileId": f.fid,
        "expression": "geo=EU",
        "replicasNum": 1,
        "status": "pending",
    }

    assert list((await check())["not_ready"]) == ["at_other_site"]


@pytest.mark.asyncio
async def test_check_an_ongoing_transfer_to_here_of_an_ancestor_directory_is_in_transfer(
    fed: Fed,
) -> None:
    fed.add("sub/x.nc", 100, owner="full")
    fed.add("other/y.nc", 100, owner="full")
    fed.transfers["t1"] = {
        "fileId": "dirsub",
        "filePath": f"/{SPACE}/sub",
        "replicatingProviderId": LOCAL,
    }
    # a transfer to some other provider moves nothing here
    fed.transfers["t2"] = {
        "fileId": "dirother",
        "filePath": f"/{SPACE}/other",
        "replicatingProviderId": OTHER,
    }

    result = await check()

    assert result["not_ready"]["in_transfer"]["examples"] == [f"/{SPACE}/sub/x.nc"]
    assert result["not_ready"]["at_other_site"]["examples"] == [f"/{SPACE}/other/y.nc"]


@pytest.mark.asyncio
async def test_check_an_ongoing_transfer_of_the_file_itself_is_in_transfer(fed: Fed) -> None:
    f = fed.add("a.nc", 100, owner="full")
    fed.transfers["t1"] = {"fileId": f.fid, "filePath": f.path, "replicatingProviderId": LOCAL}

    assert list((await check())["not_ready"]) == ["in_transfer"]


@pytest.mark.asyncio
async def test_check_reads_transfers_once_per_space_and_at_most_50(fed: Fed) -> None:
    for i in range(30):
        fed.add(f"f{i}.nc", 100, owner="full")
    for i in range(60):
        fed.transfers[f"t{i:02d}"] = {
            "fileId": f"x{i}",
            "filePath": f"/{SPACE}/nothing{i}",
            "replicatingProviderId": LOCAL,
        }

    await check()

    paths = [p for _, p in fed.requests]
    assert sum(p.endswith(f"/spaces/{SPACE_ID}/transfers") for p in paths) == 1
    assert sum("/transfers/t" in p for p in paths) == readiness.MAX_TRANSFERS == 50


@pytest.mark.asyncio
async def test_check_without_the_privilege_to_list_transfers_still_answers(fed: Fed) -> None:
    fed.add("a.nc", 100, owner="full")
    fed._transfer_list = lambda: _error(403, "forbidden")  # type: ignore[method-assign]

    assert list((await check())["not_ready"]) == ["at_other_site"]


@pytest.mark.asyncio
async def test_check_owner_down_is_unreachable(fed: Fed) -> None:
    fed.add("a.nc", 100, owner="full")
    fed.failed.add(OWNER)
    fed.online[OWNER] = False

    result = await check()

    assert result["not_ready"] == {"unreachable": {"files": 1, "examples": [f"/{SPACE}/a.nc"]}}
    assert result["advice"] == (
        "Not ready: the only copies of 1 file are at a provider that is down."
    )


@pytest.mark.asyncio
async def test_check_owner_offline_per_onezone_but_answering_is_unreachable(fed: Fed) -> None:
    fed.add("a.nc", 100, owner="full")
    fed.online[OWNER] = False

    assert list((await check())["not_ready"]) == ["unreachable"]


@pytest.mark.asyncio
async def test_check_a_failing_onezone_lookup_counts_as_online(fed: Fed) -> None:
    fed.add("a.nc", 100, owner="full")
    fed.online[OWNER] = False
    fed.zone_providers_fail = True

    # BEST EFFORT: the tool still answers, and takes the owner for online
    assert list((await check())["not_ready"]) == ["at_other_site"]


def onezone_requests(fed: Fed) -> list[str]:
    return [p for _, p in fed.requests if p.startswith("/api/v3/onezone/")]


@pytest.mark.asyncio
async def test_check_resolving_a_dataset_name_makes_no_onezone_request(fed: Fed) -> None:
    """The space comes from the provider's own list: a token valid only there is enough."""
    fed.add("a.nc", 100, local="full", owner="full")

    result = await check()

    assert result["ready"] is True
    assert onezone_requests(fed) == []
    assert fed.unexpected == []  # nothing asked Onezone for a space's details


@pytest.mark.asyncio
async def test_ensure_resolving_a_dataset_name_makes_no_onezone_request(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await ensure()

    assert result["ready"] is True
    assert onezone_requests(fed) == []


@pytest.mark.asyncio
async def test_check_reads_the_provider_list_of_spaces_once_per_call(fed: Fed) -> None:
    fed.add("sub/a.nc", 100, local="full", owner="full")

    result = await readiness.check_readiness(
        [{"dataset": SPACE}, {"dataset": f"/{SPACE}/sub"}, {"dataset": f"{SPACE}/sub"}]
    )

    assert result["ready"] is True
    assert sum(p == "/api/v3/oneprovider/spaces" for _, p in fed.requests) == 1


@pytest.mark.asyncio
async def test_a_space_is_matched_by_name_or_by_id(fed: Fed) -> None:
    fed.spaces.append({"name": "archive", "spaceId": "sp-archive-id", "providers": []})
    run = readiness._Run(LOCAL, readiness._monotonic() + 60)

    assert await run.space_id("archive") == "sp-archive-id"
    assert await run.space_id("sp-archive-id") == "sp-archive-id"
    assert await run.space_id(SPACE) == SPACE_ID
    with pytest.raises(
        ValueError, match=r"no space named 'nope'.*the spaces it lists: \['archive'"
    ):
        await run.space_id("nope")


@pytest.mark.asyncio
async def test_a_provider_list_of_spaces_that_is_not_a_list_is_the_providers_failure(
    fed: Fed,
) -> None:
    """Gemini 10: it was reported as a dataset "not found", with a hint to give one of the
    names listed (none were). It is the provider's failure: an error, never a missing space."""
    from onedata_mcp.utils import OnedataApiError

    fed.spaces = {"error": {"id": "forbidden"}}  # type: ignore[assignment]

    with pytest.raises(OnedataApiError, match=r"list of spaces \(GET /spaces\) is not a list"):
        await check()


@pytest.mark.asyncio
@pytest.mark.parametrize("onezone", ["answers-401", "not-configured"])
async def test_check_and_ensure_are_not_failed_by_a_onezone_that_refuses_the_token(
    fed: Fed, monkeypatch: pytest.MonkeyPatch, onezone: str
) -> None:
    """A token valid only at the local provider: Onezone answers 401, or is not even set up.
    The owner's online state is the only thing asked of it, and it is best effort."""
    if onezone == "answers-401":
        fed.zone_status = 401
    else:
        monkeypatch.delenv("ONEDATA_ONEZONE_HOST")
        monkeypatch.delenv("ONEDATA_ONEZONE_TOKEN")
    fed.add("a.nc", 100, owner="full")  # not here yet: classifying it asks Onezone about the owner
    fed.add("b.nc", 100, local="full", owner="full")

    checked = await check()
    ensured = await ensure(deadline_s=10)

    assert list(checked["not_ready"]) == [
        "at_other_site"
    ]  # no exception; the owner taken as online
    assert checked["resolved_files"] == 2
    assert ensured["ready"] is False
    assert ensured["held"]["files"] == 2
    assert "unchecked" not in ensured


@pytest.mark.asyncio
async def test_check_looks_each_provider_up_once(fed: Fed) -> None:
    for i in range(20):
        fed.add(f"f{i}.nc", 100, owner="full")

    await check()

    assert sum(p.endswith(f"/providers/{OWNER}") for _, p in fed.requests) == 1


@pytest.mark.asyncio
async def test_check_counts_each_reason_and_names_the_dominant_one(fed: Fed) -> None:
    fed.add("a.nc", 100, owner="full")
    fed.add("b.nc", 100, owner="half")
    fed.add("c.nc", 100, owner="half")
    fed.add("d.nc", 100, local="full", owner="full")

    result = await check()

    assert {r: v["files"] for r, v in result["not_ready"].items()} == {
        "being_written": 2,
        "at_other_site": 1,
    }
    assert "2 files are still being written" in result["advice"]


# --- resolving the extent ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_unresolved_dataset(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await readiness.check_readiness([{"dataset": "no-such-space"}])

    assert result["ready"] is False
    assert result["resolved_files"] == 0
    assert len(result["unresolved"]) == 1
    assert result["unresolved"] == [
        "dataset 'no-such-space' not found: your provider supports no space named "
        f"'no-such-space' for this token (the spaces it lists: ['{SPACE}']). "
        "Give one of those names, or an absolute directory path inside one."
    ]
    assert "1 declared item was not resolved (see unresolved)." in result["advice"]


@pytest.mark.asyncio
async def test_check_unresolved_directory_inside_a_space(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await readiness.check_readiness([{"dataset": f"/{SPACE}/nodir"}])

    assert result["unresolved"] == [no_such_directory(f"/{SPACE}/nodir")]


@pytest.mark.asyncio
async def test_check_unresolved_file_and_date_range_make_the_extent_not_ready(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("data_20180801.nc", 100, local="full", owner="full")

    result = await check(
        files=["a.nc", "missing.nc"],
        date_ranges=[{"start": "2018-09-01", "end": "2018-09-30"}],
    )

    assert result["ready"] is False
    assert result["resolved_files"] == 1  # a.nc resolved and is ready
    assert "not_ready" not in result
    assert result["unresolved"] == [
        file_not_found("missing.nc"),
        range_matched_nothing("2018-09-01", "2018-09-30"),
    ]
    assert result["advice"].startswith("Not ready: 2 declared items were not resolved")


@pytest.mark.asyncio
async def test_check_an_empty_dataset_is_unresolved_not_vacuously_ready(fed: Fed) -> None:
    result = await readiness.check_readiness([{"dataset": SPACE}])

    assert result["ready"] is False
    assert result["resolved_files"] == 0
    assert result["unresolved"] == [no_files()]


@pytest.mark.asyncio
async def test_check_overlapping_datasets_count_each_file_once(fed: Fed) -> None:
    fed.add("sub/a.nc", 100, local="full", owner="full")

    result = await readiness.check_readiness([{"dataset": f"/{SPACE}/sub"}, {"dataset": SPACE}])

    assert result["ready"] is True
    assert result["resolved_files"] == 1


@pytest.mark.asyncio
async def test_check_selects_by_date_for_both_name_forms(fed: Fed) -> None:
    fed.add("data_20180801.nc", 100, local="full", owner="full")
    fed.add("20180802_example_product_P1D-m.nc", 100, local="full", owner="full")
    fed.add("data_20180901.nc", 100, owner="half")  # outside the range: must not count
    fed.add("data_undated.nc", 100, owner="half")  # no date never matches a range

    result = await check(date_ranges=[{"start": "2018-08-01", "end": "2018-08-31"}])

    assert result["ready"] is True
    assert result["resolved_files"] == 2


@pytest.mark.asyncio
async def test_check_date_range_bounds_are_inclusive(fed: Fed) -> None:
    for day in ("20180731", "20180801", "20180831", "20180901"):
        fed.add(f"data_{day}.nc", 10, local="full", owner="full")

    result = await check(date_ranges=[{"start": "2018-08-01", "end": "2018-08-31"}])

    assert result["resolved_files"] == 2


@pytest.mark.asyncio
async def test_check_files_and_date_ranges_are_a_union(fed: Fed) -> None:
    fed.add("data_20180801.nc", 10, local="full", owner="full")
    fed.add("other.nc", 10, local="full", owner="full")
    fed.add("skipped.nc", 10, local="full", owner="full")

    result = await check(
        files=["other.nc"], date_ranges=[{"start": "2018-08-01", "end": "2018-08-01"}]
    )

    assert result["resolved_files"] == 2


@pytest.mark.asyncio
async def test_check_files_by_basename_absolute_path_and_relative_path(fed: Fed) -> None:
    fed.add("top.nc", 10, local="full", owner="full")
    fed.add("sub/deep.nc", 10, local="full", owner="full")
    fed.add("sub/deeper/x.nc", 10, local="full", owner="full")
    fed.add("skipped.nc", 10, local="full", owner="full")

    result = await check(files=["top.nc", f"/{SPACE}/sub/deep.nc", "sub/deeper/x.nc"])

    assert result["ready"] is True
    assert result["resolved_files"] == 3


@pytest.mark.asyncio
async def test_check_a_dataset_given_as_a_directory_path_scopes_the_listing(fed: Fed) -> None:
    fed.add("sub/a.nc", 10, local="full", owner="full")
    fed.add("outside.nc", 10, owner="half")

    result = await readiness.check_readiness([{"dataset": f"/{SPACE}/sub"}])

    assert result["ready"] is True
    assert result["resolved_files"] == 1


@pytest.mark.asyncio
async def test_check_the_same_file_named_twice_counts_once(fed: Fed) -> None:
    fed.add("a.nc", 10, local="full", owner="full")

    result = await readiness.check_readiness(
        [{"dataset": SPACE, "files": ["a.nc"]}, {"dataset": SPACE}]
    )

    assert result["resolved_files"] == 1


@pytest.mark.asyncio
async def test_check_pages_the_listing_by_directory_id(fed: Fed) -> None:
    for i in range(2500):
        fed.add(f"f{i:05d}.nc", 1, local="full", owner="full")

    result = await check(files=["f00000.nc", "f02499.nc"])

    assert result["resolved_files"] == 2
    assert fed.listing_pages == 3  # 1000 + 1000 + 500
    # one path lookup for the root, not one per page
    assert sum(m == "POST" and "lookup-file-id" in p for m, p in fed.requests) == 1


@pytest.mark.asyncio
async def test_check_refuses_an_extent_of_more_than_20000_files(fed: Fed) -> None:
    for i in range(20_001):
        fed.add(f"f{i:05d}.nc", 1, local="full", owner="full")

    with pytest.raises(ValueError, match="more than 20000 files"):
        await check()
    # refused before any file was looked at
    assert not any("/distribution" in p for _, p in fed.requests)


@pytest.mark.asyncio
async def test_check_the_file_limit_is_inclusive(fed: Fed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "MAX_FILES", 50)
    for i in range(50):
        fed.add(f"f{i:05d}.nc", 1, local="full", owner="full")
    assert (await check())["resolved_files"] == 50

    fed.add("one-too-many.nc", 1, local="full", owner="full")
    with pytest.raises(ValueError, match="more than 50 files"):
        await check()


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [{"dataset": "x"}] * 21,
        [{"dataset": ""}],
        [{"dataset": "/"}],
        [{}],
        [{"dataset": "x", "files": "a.nc"}],
        [{"dataset": "x", "files": [""]}],
        [{"dataset": "x", "date_ranges": [{"start": "2018-13-01", "end": "2018-12-01"}]}],
        [{"dataset": "x", "date_ranges": [{"start": "2018-09-01", "end": "2018-08-01"}]}],
        [{"dataset": "x", "date_ranges": [{"start": "2018-08-01"}]}],
        ["x"],
    ],
)
@pytest.mark.asyncio
async def test_check_rejects_a_malformed_extent(bad: list) -> None:
    with pytest.raises(ValueError):
        await readiness.check_readiness(bad)


@pytest.mark.asyncio
async def test_check_budget_runs_out_and_the_rest_is_unchecked(fed: Fed, clock: Clock) -> None:
    for i in range(30):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")
    listing = fed._listing

    def slow_listing(root_id: str, body: dict) -> httpx.Response:
        response = listing(root_id, body)
        clock.now += readiness.CHECK_BUDGET_S + 1  # the listing eats the whole budget
        return response

    fed._listing = slow_listing  # type: ignore[method-assign]

    result = await check()

    assert result["ready"] is False
    assert result["resolved_files"] == 30
    assert result["unchecked"] == 30
    assert result["unresolved"] == []
    assert result["advice"] == "Not ready: 30 files could not be checked."
    assert not any("/distribution" in p for _, p in fed.requests)


@pytest.mark.asyncio
async def test_check_does_not_start_follow_up_reads_after_the_budget(
    fed: Fed, clock: Clock
) -> None:
    """A file that is not ready needs a provider lookup, transfer reads and a rule read to be
    classified. Once the budget is gone it is left unchecked instead."""
    for i in range(20):
        fed.add(f"f{i}.nc", 100, owner="full")  # not ready: classifying it needs the follow-ups
    distribution = fed._distribution

    def slow_distribution(fid: str) -> httpx.Response:
        response = distribution(fid)
        clock.now += readiness.CHECK_BUDGET_S + 1
        return response

    fed._distribution = slow_distribution  # type: ignore[method-assign]

    result = await check()

    assert result["unchecked"] == 20
    assert "not_ready" not in result
    assert not any("/providers/" in p or "/transfers" in p or "qos" in p for _, p in fed.requests)


@pytest.mark.asyncio
async def test_check_a_listing_cut_short_by_the_budget_is_reported(
    fed: Fed, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "CHECK_BUDGET_S", 30.0)
    for i in range(2500):
        fed.add(f"f{i:05d}.nc", 1, local="full", owner="full")
    listing = fed._listing

    def slow_listing(root_id: str, body: dict) -> httpx.Response:
        response = listing(root_id, body)
        clock.now += 40
        return response

    fed._listing = slow_listing  # type: ignore[method-assign]

    result = await check()

    assert result["unresolved"] == [out_of_time()]
    assert result["resolved_files"] == 1000  # what the first page held
    assert result["ready"] is False


@pytest.mark.asyncio
async def test_check_one_unreadable_file_is_unchecked_not_a_failure(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    gone = fed.add("b.nc", 100, local="full", owner="full")
    original = fed._distribution
    fed._distribution = lambda fid: (  # type: ignore[method-assign]
        _error(400, "posix", "enoent") if fid == gone.fid else original(fid)
    )

    result = await check()

    assert result["ready"] is False
    assert result["unchecked"] == 1


@pytest.mark.asyncio
async def test_check_every_distribution_failing_is_an_error_not_an_answer(fed: Fed) -> None:
    from onedata_mcp.utils import OnedataApiError

    fed.add("a.nc", 100, local="full", owner="full")
    fed._distribution = lambda fid: _error(500, "internalServerError")  # type: ignore[method-assign]

    with pytest.raises(OnedataApiError):
        await check()


@pytest.mark.asyncio
async def test_check_caps_requests_in_flight_at_16(fed: Fed) -> None:
    for i in range(60):
        fed.add(f"f{i}.nc", 100, owner="full")

    await check()

    assert readiness.CHECK_CONCURRENCY == 16
    assert 1 < fed.peak["distribution"] <= 16


@pytest.mark.asyncio
async def test_check_has_at_most_16_calls_in_flight_in_total(fed: Fed) -> None:
    """Distribution reads, provider lookups, transfer reads and rule reads share one limit."""
    for i in range(80):
        fed.add(f"f{i}.nc", 100, owner="full")
    for i in range(60):
        fed.transfers[f"t{i:02d}"] = {
            "fileId": f"x{i}",
            "filePath": f"/{SPACE}/nothing{i}",
            "replicatingProviderId": LOCAL,
        }

    await check()

    assert 1 < fed.total_peak <= readiness.CHECK_CONCURRENCY == 16


@pytest.mark.asyncio
async def test_ensure_has_at_most_16_calls_in_flight_in_total(fed: Fed) -> None:
    for i in range(40):
        fed.add(f"f{i}.nc", 100, owner="half" if i % 2 else "full")

    await ensure(deadline_s=10)

    assert 1 < fed.total_peak <= 16


@pytest.mark.asyncio
async def test_check_fast_path_a_false_flag_cannot_be_ready(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local="full", owner="full")
    original = fed.attrs
    fed.attrs = lambda file: {**original(file), "isFullyReplicatedLocally": False}  # type: ignore[method-assign]

    result = await check()

    assert f.local_full()
    assert result["ready"] is False


@pytest.mark.asyncio
async def test_check_the_provider_id_is_read_once_per_process(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    await check()
    await check()

    assert sum(p.endswith("/configuration") for _, p in fed.requests) == 1


# ---------------------------------------------------------------------------
# ensure_readiness against the fake
# ---------------------------------------------------------------------------


async def ensure(deadline_s: int = 3600, **item) -> dict:
    return await readiness.ensure_readiness(extent(**item), deadline_s)


@pytest.mark.asyncio
async def test_ensure_places_one_rule_per_file_waits_and_turns_ready(
    fed: Fed, clock: Clock
) -> None:
    a = fed.add("a.nc", 100, local="full", owner="full")  # ready already: still held
    b = fed.add("b.nc", 100, owner="full")  # at another site
    c = fed.add("c.nc", 100, owner="half")  # still being written

    def flip(n: int) -> None:
        if n == 2:  # the writer finishes and the copy lands
            c.blocks[OWNER] = [[0, 100]]
            b.blocks[LOCAL] = [[0, 100]]
            c.blocks[LOCAL] = [[0, 100]]

    clock.after_sleep = flip

    result = await ensure()

    assert result == {
        "ready": True,
        "data_ready": True,
        "resolved_files": 3,
        "ready_files": 3,
        "bytes": 300,
        "advice": (
            "Ready: all declared files are complete and readable at your provider now, "
            "and each is held there."
        ),
        "waited_s": 10,
        "held": {
            "files": 3,
            "rule": HOLD,
            "qos_requirement_ids": [  # in the files' path order
                next(q for q, r in fed.rules.items() if r["fileId"] == f.fid) for f in (a, b, c)
            ],
        },
    }
    assert sorted(r["fileId"] for r in fed.rule_bodies()) == sorted([a.fid, b.fid, c.fid])
    assert {json.dumps(r, sort_keys=True) for r in fed.rule_bodies()} == {
        json.dumps({"fileId": f.fid, "expression": HOLD, "replicasNum": 1}, sort_keys=True)
        for f in (a, b, c)
    }
    assert clock.sleeps == 2


@pytest.mark.asyncio
async def test_ensure_ready_at_once_does_not_wait(fed: Fed, clock: Clock) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await ensure()

    assert result["ready"] is True
    assert result["waited_s"] == 0
    assert clock.sleeps == 0


@pytest.mark.asyncio
async def test_ensure_does_not_place_a_second_rule_for_a_file_it_already_holds(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("b.nc", 100, local="full", owner="full")

    await ensure()
    assert len(fed.rules) == 2
    fed.add("c.nc", 100, local="full", owner="full")
    result = await ensure()

    assert len(fed.rules) == 3
    assert held_summary(result) == {"files": 3, "rule": HOLD}


@pytest.mark.asyncio
async def test_ensure_returns_not_ready_at_the_wait_cap_with_the_reason(
    fed: Fed, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "POLL_INTERVAL_S", 40.0)  # fewer rounds: this test is slow
    for i in range(14):
        fed.add(f"data_201808{i + 10:02d}.nc", 100, owner="half")
    fed.add("ok1.nc", 100, local="full", owner="full")
    fed.add("ok2.nc", 100, local="full", owner="full")

    result = await ensure(deadline_s=3600)

    assert result["ready"] is False
    assert result["waited_s"] == 150  # the cap, not the 3600 s the agent offered
    assert result["not_ready"]["being_written"]["files"] == 14
    assert held_summary(result) == {"files": 16, "rule": HOLD}
    assert result["advice"] == (
        "Not ready after 150 s: 14 files are still being written at their owner. "
        "Call again to keep waiting."
    )
    assert clock.elapsed <= readiness.MAX_WAIT_DEFAULT_S + readiness.ENSURE_GRACE_S


@pytest.mark.asyncio
async def test_ensure_waits_only_as_long_as_the_agent_offered(fed: Fed, clock: Clock) -> None:
    fed.add("a.nc", 100, owner="half")

    result = await ensure(deadline_s=12)

    assert result["waited_s"] == 12
    assert result["advice"] == (
        "Not ready after 12 s: 1 file is still being written at its owner. "
        "Call again to keep waiting."
    )


@pytest.mark.asyncio
async def test_ensure_the_wait_cap_comes_from_the_environment(
    fed: Fed, monkeypatch: pytest.MonkeyPatch
) -> None:
    fed.add("a.nc", 100, owner="half")
    monkeypatch.setenv(readiness.MAX_WAIT_ENV, "30")

    assert (await ensure(deadline_s=3600))["waited_s"] == 30


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
def test_wait_cap_falls_back_to_150_on_a_bad_value(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(readiness.MAX_WAIT_ENV, raw)
    assert readiness.max_wait_s() == 150


@pytest.mark.parametrize("bad", [0, -1, 86_401, True, 1.5, "60", None])
@pytest.mark.asyncio
async def test_ensure_rejects_a_bad_deadline(fed: Fed, bad: object) -> None:
    with pytest.raises(ValueError, match="deadline_s"):
        await readiness.ensure_readiness(extent(), bad)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ensure_falls_back_to_the_plain_expression_on_a_400(fed: Fed) -> None:
    for i in range(5):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")
    fed.qos_status = lambda fid, expression: 400 if "anyStorage" in expression else 201

    result = await ensure()

    assert result["ready"] is True
    assert held_summary(result) == {"files": 5, "rule": HOLD_PLAIN}
    assert [r["expression"] for r in fed.rule_bodies()] == [HOLD_PLAIN] * 5
    posts = [e for e in fed.requests if e == ("POST", "/api/v3/oneprovider/qos_requirements")]
    assert (
        len(posts) == 6
    )  # the first file tried both forms, the other four went straight to the plain one
    assert readiness._hold_plain is True

    # and it stays in use for later calls
    fed.add("g.nc", 100, local="full", owner="full")
    fed.requests.clear()
    await ensure()
    assert [e for e in fed.requests if e[0] == "POST" and e[1].endswith("qos_requirements")] == [
        ("POST", "/api/v3/oneprovider/qos_requirements")
    ]


@pytest.mark.asyncio
async def test_ensure_a_non_400_failure_does_not_trigger_the_fallback(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.qos_status = lambda fid, expression: 403

    result = await ensure()

    assert result["hold_failed"] == {
        "files": 1,
        "examples": [f"/{SPACE}/a.nc"],
        "causes": {"refused": 1},
    }
    assert readiness._hold_plain is False
    assert [e for e in fed.requests if e == ("POST", "/api/v3/oneprovider/qos_requirements")] == [
        ("POST", "/api/v3/oneprovider/qos_requirements")
    ]  # one attempt, no second form


@pytest.mark.asyncio
async def test_ensure_reports_hold_failed_and_holds_the_rest(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    bad = fed.add("b.nc", 100, local="full", owner="full")
    fed.add("c.nc", 100, local="full", owner="full")
    fed.qos_status = lambda fid, expression: 403 if fid == bad.fid else 201

    result = await ensure()

    # the data are there, and one file is not held: not ready (Codex 7, Gemini 9), and each
    # half is said on its own
    assert (result["ready"], result["data_ready"]) == (False, True)
    assert held_summary(result) == {"files": 2, "rule": HOLD}
    assert result["hold_failed"] == {
        "files": 1,
        "examples": [bad.path],
        "causes": {"refused": 1},
    }
    assert result["advice"] == (
        "Not ready after 0 s: the data are complete and readable at your provider now "
        "(data_ready), but 1 file is not held there. A hold could not be placed for 1 file: the "
        "provider refused this token the right to add QoS requirements."
    )


@pytest.mark.asyncio
async def test_ensure_when_both_expression_forms_are_rejected_the_file_is_hold_failed(
    fed: Fed,
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.qos_status = lambda fid, expression: 400

    result = await ensure()

    assert result["hold_failed"]["files"] == 1
    assert result["held"]["files"] == 0
    assert readiness._hold_plain is False


@pytest.mark.asyncio
async def test_ensure_a_failed_hold_is_retried_by_the_next_call(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local="full", owner="full")
    fed.qos_status = lambda fid, expression: 403
    assert (await ensure())["hold_failed"]["files"] == 1

    fed.qos_status = lambda fid, expression: 201
    result = await ensure()

    assert "hold_failed" not in result
    assert held_summary(result) == {"files": 1, "rule": HOLD}
    assert fed.rule_bodies()[0]["fileId"] == f.fid


@pytest.mark.asyncio
async def test_ensure_caps_rule_creation_at_8_in_flight(fed: Fed) -> None:
    for i in range(40):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")

    await ensure()

    assert readiness.PLACE_CONCURRENCY == 8
    assert 1 < fed.peak["qos_post"] <= 8
    assert len(fed.rules) == 40


@pytest.mark.asyncio
async def test_ensure_only_re_checks_files_not_yet_confirmed(fed: Fed, clock: Clock) -> None:
    fed.add("ready.nc", 100, local="full", owner="full")
    slow = fed.add("slow.nc", 100, owner="half")

    def flip(n: int) -> None:
        if n == 3:
            slow.blocks[OWNER] = [[0, 100]]
            slow.blocks[LOCAL] = [[0, 100]]

    clock.after_sleep = flip
    await ensure()

    attribute_calls = [
        p for _, p in fed.requests if re.fullmatch(r"/api/v3/oneprovider/data/[^/]+", p)
    ]
    ready_id = fed.by_rel("ready.nc").fid
    # polled once and confirmed, then read once more in the final pass: no poll round after it
    assert sum(p.endswith(ready_id) for p in attribute_calls) == 2


@pytest.mark.asyncio
async def test_ensure_unreachable_names_the_provider_that_is_down(fed: Fed) -> None:
    fed.add("a.nc", 100, owner="full")
    fed.failed.add(OWNER)
    fed.online[OWNER] = False

    result = await ensure(deadline_s=10)

    assert result["not_ready"] == {"unreachable": {"files": 1, "examples": [f"/{SPACE}/a.nc"]}}
    assert result["advice"] == (
        "Not ready after 10 s: the only copies of 1 file are at a provider that is down."
    )
    assert result["held"]["files"] == 1  # the rule is in place for when it comes back


@pytest.mark.asyncio
async def test_ensure_unresolved_items_are_reported_and_the_rest_is_still_held(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await ensure(files=["a.nc", "missing.nc"])

    assert result["ready"] is False
    assert result["unresolved"] == [file_not_found("missing.nc")]
    assert held_summary(result) == {"files": 1, "rule": HOLD}
    assert result["advice"].startswith("Not ready after 0 s: 1 declared item was not resolved")


@pytest.mark.asyncio
async def test_ensure_nothing_resolved_places_nothing(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await readiness.ensure_readiness([{"dataset": "nope"}], 60)

    assert result["ready"] is False
    assert held_summary(result) == {"files": 0, "rule": HOLD}
    assert fed.rules == {}


@pytest.mark.asyncio
async def test_ensure_calls_the_nudge_hook_for_a_file_with_a_fulfilled_rule_that_is_not_local(
    fed: Fed, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = fed.add("a.nc", 100, owner="full")  # writes complete, not local
    g = fed.add("b.nc", 100, owner="half")  # still being written: no nudge
    nudged: list[str] = []

    async def nudge(file: readiness.FileInfo) -> bool:
        nudged.append(file.file_id)
        return False

    monkeypatch.setattr(readiness, "_nudge", nudge)

    def fulfil(n: int) -> None:
        for rule in fed.rules.values():
            rule["status"] = "fulfilled"

    clock.after_sleep = fulfil
    await ensure(deadline_s=15)

    assert nudged
    assert set(nudged) == {f.fid}
    assert g.fid not in nudged


@pytest.mark.asyncio
async def test_ensure_does_not_nudge_while_the_rule_is_pending(
    fed: Fed, monkeypatch: pytest.MonkeyPatch
) -> None:
    fed.add("a.nc", 100, owner="full")
    nudged: list[str] = []

    async def nudge(file: readiness.FileInfo) -> bool:
        nudged.append(file.file_id)
        return False

    monkeypatch.setattr(readiness, "_nudge", nudge)
    await ensure(deadline_s=15)

    assert nudged == []


@pytest.mark.asyncio
async def test_the_default_nudge_is_a_no_op_returning_false() -> None:
    assert await readiness._nudge(readiness.FileInfo("f", "/s/f", "s")) is False


@pytest.mark.asyncio
async def test_ensure_ends_within_the_hard_cap_on_a_slow_server(fed: Fed, clock: Clock) -> None:
    """Each request costs three fake seconds. Unplaced files are reported, nothing runs on."""
    for i in range(120):
        fed.add(f"f{i}.nc", 100, owner="half")
    fed.latency = 3.0

    result = await ensure(deadline_s=3600)

    assert result["ready"] is False
    # Requests in flight at the cut-off each add their latency to the one fake clock.
    cap = readiness.MAX_WAIT_DEFAULT_S + readiness.ENSURE_GRACE_S
    cap += readiness.CHECK_CONCURRENCY * fed.latency
    assert clock.elapsed <= cap
    assert result["hold_failed"]["files"] > 0  # not placed in time
    assert result["unchecked"] > 0  # not looked at in time
    assert "Not ready after" in result["advice"]


@pytest.mark.asyncio
async def test_ensure_waits_with_the_poll_interval(fed: Fed, clock: Clock) -> None:
    fed.add("a.nc", 100, owner="half")

    await ensure(deadline_s=23)

    assert clock.sleeps == 5  # 5 + 5 + 5 + 5 + 3


# ---------------------------------------------------------------------------
# Review fixes: each test below fails without its fix
# ---------------------------------------------------------------------------

# --- 1. size ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected_covering"),
    [
        (100, frozenset()),  # the listing says 100: both providers' 50 bytes are not all of it
        (50, frozenset({LOCAL, OWNER})),
        (0, frozenset({LOCAL, OWNER})),
    ],
)
def test_parse_distribution_judges_against_the_listing_size_too(
    size: int, expected_covering: frozenset[str]
) -> None:
    body = _body({LOCAL: _entry(50, [[0, 50]]), OWNER: _entry(50, [[0, 50]])})

    assert readiness.parse_distribution(body, size).covering == expected_covering


def test_parse_distribution_with_no_size_from_any_source_covers_nothing() -> None:
    body = _body(
        {
            LOCAL: {
                "success": True,
                "distributionPerStorageBackend": {"s": {"success": True, "blocks": []}},
            },
            OWNER: {"success": True, "distributionPerStorageBackend": {}},
        }
    )

    dist = readiness.parse_distribution(body)  # the listing gave no size either

    assert dist.size == 0
    assert not dist.known
    assert dist.covering == frozenset()
    assert dist.holders == frozenset()


def test_parse_distribution_logical_sizes_of_0_do_not_make_a_known_empty_file() -> None:
    """A missing listing size is not a size of 0: the providers' 0 alone does not settle it."""
    body = _body({LOCAL: _entry(0), OWNER: _entry(0)})

    dist = readiness.parse_distribution(body, None)

    assert not dist.known
    assert dist.covering == frozenset()


def test_parse_distribution_a_listing_size_of_0_is_an_empty_file_every_lister_covers() -> None:
    dist = readiness.parse_distribution(_body({LOCAL: _entry(0), OWNER: _entry(0)}), 0)

    assert dist.empty
    assert dist.known
    assert dist.size == 0
    assert dist.covering == {LOCAL, OWNER}


def test_parse_distribution_a_listing_size_of_0_does_not_hide_a_positive_provider_size() -> None:
    dist = readiness.parse_distribution(_body({LOCAL: _entry(0), OWNER: _entry(100, [[0, 40]])}), 0)

    assert not dist.empty
    assert dist.size == 100
    assert dist.covering == frozenset()


def test_parse_distribution_a_listing_size_of_0_does_not_hide_blocks() -> None:
    dist = readiness.parse_distribution(_body({OWNER: _entry(0, [[0, 5]])}), 0)

    assert not dist.empty
    assert dist.size == 5  # it holds bytes: it is not empty


@pytest.mark.asyncio
async def test_check_a_stale_smaller_logical_size_cannot_certify_a_bigger_file(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local=[[0, 50]], owner=[[0, 50]])  # the listing says 100 bytes
    f.logical = {LOCAL: 50, OWNER: 50, OTHER: 50}  # but every provider's own, stale size says 50

    result = await check()

    assert result["ready"] is False
    assert list(result["not_ready"]) == ["being_written"]


@pytest.mark.asyncio
async def test_check_a_bigger_provider_size_than_the_listing_wins(fed: Fed) -> None:
    f = fed.add("a.nc", 50, local="full", owner="full")  # the listing says 50 bytes
    f.logical = {LOCAL: 100, OWNER: 100}  # the providers say 100

    assert (await check())["ready"] is False


def _without_listing_size(fed: Fed) -> None:
    original = fed.attrs
    fed.attrs = lambda file: {k: v for k, v in original(file).items() if k != "size"}  # type: ignore[method-assign]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("logical", "blocks"),
    [(None, "full"), (0, [])],
    ids=["no-logical-size", "logical-size-0-and-no-blocks"],
)
async def test_check_a_size_missing_from_every_source_is_unchecked(
    fed: Fed, logical: int | None, blocks: str | list
) -> None:
    """A missing size is not an empty file: it cannot be judged, so it is never ready."""
    f = fed.add("a.nc", 100, local=blocks, owner=blocks)
    f.logical = {LOCAL: logical, OWNER: logical, OTHER: logical}
    _without_listing_size(fed)

    result = await check()

    assert result["ready"] is False
    assert result["unchecked"] == 1
    assert "not_ready" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("logical", [0, None], ids=["logical-size-0", "no-logical-size"])
async def test_check_an_empty_file_is_ready(fed: Fed, logical: int | None) -> None:
    """A listing size of exactly 0 and no positive size elsewhere: nothing to read."""
    f = fed.add("empty.nc", 0, local=[], owner=[])
    f.logical = {LOCAL: logical, OWNER: logical, OTHER: logical}

    result = await check()

    assert result == {
        "ready": True,
        "resolved_files": 1,
        "ready_files": 1,
        "bytes": 0,
        "advice": "Ready: all declared files are complete and readable at your provider now.",
    }


@pytest.mark.asyncio
async def test_check_an_empty_file_the_local_provider_cannot_be_read_for_is_not_ready(
    fed: Fed,
) -> None:
    fed.add("empty.nc", 0, local=[], owner=[])
    fed.failed.add(LOCAL)

    assert (await check())["ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [OWNER, LOCAL])
async def test_check_an_empty_file_is_not_confirmed_when_any_provider_is_unreadable(
    fed: Fed, origin: str
) -> None:
    fed.add("empty.nc", 0, local=[], owner=[], origin=origin)
    fed.failed.add(OTHER)  # one provider that cannot be read: it might hold bytes

    result = await check()

    assert result["ready"] is False
    assert result["unchecked"] == 1


@pytest.mark.asyncio
async def test_check_a_listing_size_of_0_does_not_hide_a_positive_provider_size(fed: Fed) -> None:
    f = fed.add("a.nc", 0, local=[], owner=[])
    f.logical = {LOCAL: 0, OWNER: 100, OTHER: 0}  # the owner says there are 100 bytes

    result = await check()

    assert result["ready"] is False
    assert list(result["not_ready"]) == ["being_written"]


@pytest.mark.asyncio
async def test_ensure_an_empty_file_is_confirmed_and_stays_ready_through_the_final_pass(
    fed: Fed,
) -> None:
    fed.add("empty.nc", 0, local=[], owner=[])
    fed.add("a.nc", 100, local="full", owner="full")

    result = await ensure()

    assert result["ready"] is True
    assert held_summary(result) == {"files": 2, "rule": HOLD}


@pytest.mark.asyncio
async def test_check_a_dataset_without_its_leading_slash_resolves_when_its_first_segment_is_a_space(
    fed: Fed,
) -> None:
    fed.add("sub/a.nc", 100, local="full", owner="full")
    fed.add("outside.nc", 100, owner="half")  # not in sub: must not count

    result = await readiness.check_readiness([{"dataset": f"{SPACE}/sub"}])

    assert result == {
        "ready": True,
        "resolved_files": 1,
        "ready_files": 1,
        "bytes": 100,
        "advice": "Ready: all declared files are complete and readable at your provider now.",
    }
    looked_up = [
        unquote(path) for m, path in fed.requests if m == "POST" and "lookup-file-id" in path
    ]
    assert looked_up == ["/api/v3/oneprovider/lookup-file-id//my-dataset/sub"]  # the directory


@pytest.mark.asyncio
async def test_check_a_plain_space_name_still_works(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    assert (await readiness.check_readiness([{"dataset": SPACE}]))["resolved_files"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dataset", "space"), [("nosuch/sub", "nosuch"), ("other-space/a/b", "other-space")]
)
async def test_check_a_dataset_without_its_leading_slash_and_no_such_space_says_how_to_fix_it(
    fed: Fed, dataset: str, space: str
) -> None:
    fed.add("sub/a.nc", 100, local="full", owner="full")

    result = await readiness.check_readiness([{"dataset": dataset}])

    assert result["ready"] is False
    assert result["resolved_files"] == 0
    assert result["unresolved"] == [misspelled(dataset, space)]
    assert not any("lookup-file-id" in p for _, p in fed.requests)


@pytest.mark.asyncio
async def test_check_a_dataset_without_its_leading_slash_and_no_such_directory_is_unresolved(
    fed: Fed,
) -> None:
    fed.add("sub/a.nc", 100, local="full", owner="full")

    result = await readiness.check_readiness([{"dataset": f"{SPACE}/nodir"}])

    assert result["resolved_files"] == 0
    assert result["unresolved"] == [no_such_directory(f"{SPACE}/nodir")]


def test_the_example_text_for_a_dataset_without_its_leading_slash() -> None:
    assert misspelled("nosuch/sub", "nosuch") == (
        "dataset 'nosuch/sub' not found: your provider supports no space named 'nosuch' for "
        "this token (the spaces it lists: ['my-dataset']). Give one of those names, or an "
        "absolute directory path inside one."
    )


@pytest.mark.asyncio
async def test_ensure_a_dataset_without_its_leading_slash_is_unresolved_too(fed: Fed) -> None:
    result = await readiness.ensure_readiness([{"dataset": "nosuch/sub"}], 60)

    assert result["ready"] is False
    assert result["unresolved"] == [misspelled("nosuch/sub", "nosuch")]
    assert fed.rules == {}


@pytest.mark.asyncio
async def test_every_unresolved_text_is_one_plain_line_that_says_what_to_give(fed: Fed) -> None:
    """Each says what to change in the extent; none advises on what to do with the data."""
    fed.add("sub/a.nc", 100, local="full", owner="full")
    texts: list[str] = []
    for item in (
        {"dataset": "nope"},
        {"dataset": "nosuch/sub"},
        {"dataset": f"/{SPACE}/nodir"},
        {"dataset": SPACE, "files": ["missing.nc"]},
        {"dataset": SPACE, "date_ranges": [{"start": "2018-09-01", "end": "2018-09-30"}]},
    ):
        texts += (await readiness.check_readiness([item]))["unresolved"]
    fed.files.clear()
    texts += (await readiness.check_readiness([{"dataset": SPACE}]))["unresolved"]
    texts += [listing_incomplete(), out_of_time(), no_access(f"/{SPACE}/d")]

    assert len(texts) >= 8
    for text in texts:
        assert "\n" not in text
        assert not re.search(r"comput|analy|should|must|do not|don't", text, re.IGNORECASE), text
        assert re.search(r"[Gg]ive|[Cc]all again|[Dd]eclare|[Ww]iden", text), text


# --- 2. inaccessible directories --------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_inaccessible_paths_are_unresolved_and_the_extent_is_not_ready(
    fed: Fed,
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    patch_listing(fed, lambda page: {**page, "inaccessiblePaths": ["Dir1/Dir3", "private"]})

    result = await check()

    assert result["ready"] is False
    assert result["resolved_files"] == 1  # what could be listed is still checked
    assert result["unresolved"] == [
        no_access(f"/{SPACE}/Dir1/Dir3"),
        no_access(f"/{SPACE}/private"),
    ]


@pytest.mark.asyncio
async def test_check_a_malformed_inaccessible_paths_field_is_an_incomplete_listing(
    fed: Fed,
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    patch_listing(fed, lambda page: {**page, "inaccessiblePaths": "Dir1"})

    result = await check()

    assert result["ready"] is False
    assert result["unresolved"] == [listing_incomplete()]


# --- 3. no sticky ready at the end ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_a_file_that_lost_its_local_replica_before_the_answer_is_not_ready(
    fed: Fed, clock: Clock
) -> None:
    a = fed.add("a.nc", 100, local="full", owner="full")
    b = fed.add("b.nc", 100, owner="full")

    def flip(n: int) -> None:
        if n == 1:
            b.blocks[LOCAL] = [[0, 100]]
            a.blocks[LOCAL] = []  # a was confirmed ready, then lost its replica

    clock.after_sleep = flip

    result = await ensure()

    assert result["ready"] is False
    # its placement rule is still pending, so it is on its way again
    assert result["not_ready"] == {"in_transfer": {"files": 1, "examples": [a.path]}}


@pytest.mark.asyncio
async def test_ensure_a_file_that_grew_after_it_was_confirmed_is_not_ready(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    attributes = fed._attributes
    reads: list[str] = []

    def grown_on_the_final_pass(fid: str, body: dict) -> httpx.Response:
        reads.append(fid)
        response = attributes(fid, body)
        if len(reads) == 1:  # the poll
            return response
        return httpx.Response(200, json={**json.loads(response.content), "size": 200})

    fed._attributes = grown_on_the_final_pass  # type: ignore[method-assign]

    result = await ensure()

    assert len(reads) == 2
    assert result["ready"] is False  # judged at 200 bytes, of which 100 are held anywhere
    assert list(result["not_ready"]) == ["being_written"]


@pytest.mark.asyncio
async def test_ensure_a_failed_final_read_does_not_certify_the_file(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    attributes = fed._attributes
    reads: list[str] = []

    def fails_on_the_final_pass(fid: str, body: dict) -> httpx.Response:
        reads.append(fid)
        if len(reads) == 1:
            return attributes(fid, body)
        return _error(500, "internalServerError")

    fed._attributes = fails_on_the_final_pass  # type: ignore[method-assign]

    result = await ensure()

    assert result["ready"] is False
    assert result["unchecked"] == 1
    assert "not_ready" not in result


@pytest.mark.asyncio
async def test_ensure_the_final_pass_reads_every_confirmed_file_once_more(fed: Fed) -> None:
    for i in range(5):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")

    result = await ensure()

    assert result["ready"] is True
    for f in fed.files.values():
        reads = [p for m, p in fed.requests if p.endswith(f"/data/{f.fid}")]
        assert len(reads) == 2  # the poll that confirmed it, and the final pass


# --- 4. names are matched exactly -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [" a.nc", "a.nc ", "A.NC"])
async def test_check_file_names_are_matched_exactly_as_declared(fed: Fed, name: str) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("sub/a.nc", 100, local="full", owner="full")

    result = await check(files=[name])

    assert result["resolved_files"] == 0
    assert result["unresolved"] == [file_not_found(name)]


@pytest.mark.asyncio
@pytest.mark.parametrize("dataset", [f" {SPACE}", f"{SPACE} "])
async def test_check_dataset_names_are_matched_exactly_as_declared(fed: Fed, dataset: str) -> None:
    fed.add("sub/a.nc", 100, local="full", owner="full")

    result = await readiness.check_readiness([{"dataset": dataset}])

    assert result["ready"] is False
    assert result["resolved_files"] == 0
    assert result["unresolved"] and result["unresolved"][0].startswith(
        f"dataset '{dataset}' not found"
    )


# --- 5. an incomplete listing is never completed silently -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        lambda page: {**page, "isLast": False, "nextPageToken": None},
        lambda page: {**page, "isLast": False, "nextPageToken": ""},
        lambda page: {k: v for k, v in page.items() if k not in ("isLast", "nextPageToken")},
        lambda page: {k: v for k, v in page.items() if k != "files"},
    ],
    ids=["not-last-no-token", "not-last-empty-token", "no-isLast-no-token", "no-files"],
)
async def test_check_a_listing_that_cannot_be_continued_is_incomplete(fed: Fed, change) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    patch_listing(fed, change)

    result = await check()

    assert result["ready"] is False
    assert listing_incomplete() in result["unresolved"]


@pytest.mark.asyncio
async def test_check_the_onedata_25_last_page_shape_is_complete(fed: Fed) -> None:
    """Onedata 25.0 (measured live) omits `isLast` and ends with `nextPageToken: null`."""
    fed.add("a.nc", 100, local="full", owner="full")
    patch_listing(
        fed,
        lambda page: {
            **{k: v for k, v in page.items() if k != "isLast"},
            "nextPageToken": None,
        },
    )

    result = await check()

    assert result["ready"] is True
    assert not result.get("unresolved")


@pytest.mark.asyncio
async def test_check_a_regular_entry_without_a_file_id_makes_the_listing_incomplete(
    fed: Fed,
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("b.nc", 100, local="full", owner="full")

    def without_an_id(page: dict) -> dict:
        page["files"][1].pop("fileId")
        return page

    patch_listing(fed, without_an_id)

    result = await check()

    assert result["ready"] is False
    assert result["resolved_files"] == 1
    assert result["unresolved"] == [listing_incomplete()]


@pytest.mark.asyncio
async def test_check_an_entry_without_a_type_makes_the_listing_incomplete(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    def untyped(page: dict) -> dict:
        page["files"][0].pop("type")
        return page

    patch_listing(fed, untyped)

    assert (await check())["unresolved"] == [listing_incomplete()]


@pytest.mark.asyncio
async def test_check_a_continuation_token_that_does_not_advance_is_incomplete(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    pages = {"n": 0}
    listing = fed._listing

    def stuck(root_id: str, body: dict) -> httpx.Response:
        pages["n"] += 1
        data = json.loads(listing(root_id, {**body, "token": None}).content)
        return httpx.Response(200, json={**data, "isLast": False, "nextPageToken": "same"})

    fed._listing = stuck  # type: ignore[method-assign]

    result = await check()

    assert pages["n"] == 2  # the second page brought the same token: stop
    assert result["unresolved"] == [listing_incomplete()]


# --- 6. blocks --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [[0], [0, 1, 2], [-1, 5], [0, -5], [0, 0], ["0", 5], [0.5, 4], [True, 1], "ab", None, 7, {}],
)
def test_parse_distribution_a_malformed_block_makes_the_provider_unreadable(bad: object) -> None:
    body = _body({LOCAL: _entry(10, [[0, 10]]), OWNER: _entry(10, [[0, 5], bad])})  # type: ignore[list-item]

    dist = readiness.parse_distribution(body)

    assert dist.failed == {OWNER}
    assert dist.covering == {LOCAL}
    assert OWNER not in dist.covered


@pytest.mark.parametrize("raw", [None, "0-5", {"a": 1}])
def test_parse_distribution_blocks_that_are_not_a_list_make_the_provider_unreadable(
    raw: object,
) -> None:
    entry = _entry(10)
    entry["distributionPerStorageBackend"]["s0"] = {"success": True, "blocks": raw}

    assert readiness.parse_distribution(_body({OWNER: entry})).failed == {OWNER}


def test_parse_distribution_a_provider_without_a_storage_map_is_unreadable() -> None:
    entry = {"success": True, "logicalSize": 10}

    assert readiness.parse_distribution(_body({OWNER: entry})).failed == {OWNER}


def test_parse_distribution_accepts_tuples_as_blocks() -> None:
    dist = readiness.parse_distribution(_body({LOCAL: _entry(10, [(0, 10)])}))  # type: ignore[list-item]

    assert dist.covering == {LOCAL}


@pytest.mark.asyncio
async def test_check_a_malformed_local_block_cannot_make_a_file_ready(fed: Fed) -> None:
    # Two blocks that overlap at -1: the lengths add up to 101, and a lenient reading would
    # clip the first one and call the file covered.
    fed.add("a.nc", 100, local=[[-1, 50], [49, 51]], owner="full")

    result = await check()

    assert result["ready"] is False
    assert list(result["not_ready"]) == ["at_other_site"]


# --- 7. time: provider lookups ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_looks_the_providers_of_one_file_up_concurrently(fed: Fed) -> None:
    fed.add("a.nc", 100, owner="full", other="full")  # two other providers hold it

    await check()

    assert fed.peak["zone_provider"] == 2


@pytest.mark.asyncio
async def test_check_a_provider_lookup_is_cut_off_by_the_remaining_budget(
    fed: Fed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    monkeypatch.setattr(readiness, "_monotonic", time.monotonic)  # this test runs on real time
    monkeypatch.setattr(readiness, "CHECK_BUDGET_S", 1.5)
    fed.add("a.nc", 100, owner="full")
    fed.hang.add("zone_provider")  # Onezone never answers

    started = time.monotonic()
    result = await check()

    assert time.monotonic() - started < 5  # the lookup alone would take 10 s
    # the lookup used up the budget, so the rest of the classification could not be read
    assert result["ready"] is False
    assert result["unchecked"] == 1


@pytest.mark.asyncio
async def test_check_no_provider_lookup_starts_once_the_budget_is_gone(
    fed: Fed, clock: Clock
) -> None:
    fed.add("a.nc", 100, owner="full")
    distribution = fed._distribution

    def slow(fid: str) -> httpx.Response:
        response = distribution(fid)
        clock.now += readiness.CHECK_BUDGET_S + 1
        return response

    fed._distribution = slow  # type: ignore[method-assign]

    await check()

    assert not any("/providers/" in p for _, p in fed.requests)


# --- 8. deadlines -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_the_wait_budget_counts_from_the_start_of_the_call(
    fed: Fed, clock: Clock
) -> None:
    fed.add("a.nc", 100, owner="half")
    post = fed._qos_post

    def slow_placement(body: dict) -> httpx.Response:
        clock.now += 12  # placing the rule takes 12 of the 15 s the agent offered
        return post(body)

    fed._qos_post = slow_placement  # type: ignore[method-assign]

    result = await ensure(deadline_s=15)

    assert result["waited_s"] == 15
    assert clock.elapsed == 15  # not 12 + 15
    assert clock.sleeps == 1  # one sleep of the 3 s that were left


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1000", 150),
        ("240", 150),
        ("150.5", 150),
        ("150", 150),
        ("149", 149),
        ("30", 30),
        ("0.5", 0.5),
    ],
)
def test_the_wait_cap_never_exceeds_150_s_whatever_the_environment_says(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv(readiness.MAX_WAIT_ENV, raw)

    assert readiness.max_wait_s() == expected


@pytest.mark.asyncio
async def test_ensure_with_a_huge_wait_cap_in_the_environment_still_ends_within_the_harness_bound(
    fed: Fed, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(readiness.MAX_WAIT_ENV, "100000")
    monkeypatch.setattr(readiness, "POLL_INTERVAL_S", 40.0)
    fed.add("a.nc", 100, owner="half")

    result = await ensure(deadline_s=86_400)

    assert result["waited_s"] == 150  # the env may lower the wait, never raise it
    assert clock.elapsed <= 170


# --- 9. caps ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_stops_paging_as_soon_as_more_than_the_limit_of_files_is_seen(
    fed: Fed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "MAX_FILES", 50)
    monkeypatch.setattr(readiness, "LIST_PAGE_LIMIT", 10)
    for i in range(500):
        fed.add(f"f{i:04d}.nc", 1, local="full", owner="full")

    with pytest.raises(ValueError, match="more than 50 files"):
        await check(files=["f0000.nc"])  # a small selection does not help: the dataset is refused

    assert fed.listing_pages == 6  # 5 pages hold 50 files; the 6th shows the 51st
    assert not any("/distribution" in p for _, p in fed.requests)


@pytest.mark.parametrize(
    "item",
    [
        {"dataset": "x", "files": ["a.nc"] * 20_001},
        {"dataset": "x", "date_ranges": [{"start": "2018-01-01", "end": "2018-01-02"}] * 401},
        {"dataset": "x", "date_ranges": {"start": "2018-01-01", "end": "2018-01-02"}},
    ],
    ids=["too-many-files", "too-many-date-ranges", "date-ranges-not-a-list"],
)
@pytest.mark.asyncio
async def test_a_too_large_item_is_refused_before_anything_is_read(item: dict) -> None:
    with pytest.raises(ValueError):
        await readiness.check_readiness([item])


@pytest.mark.asyncio
async def test_the_caps_are_exactly_20000_names_and_400_ranges(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await check(
        files=["a.nc"] + [f"nothing{i}.nc" for i in range(19_999)],
        date_ranges=[{"start": "2018-01-01", "end": "2018-01-02"}] * 400,
    )

    assert result["resolved_files"] == 1  # accepted, and resolved: the rest is unresolved
    assert result["unresolved"][-1].startswith("... and ")


# --- 10. errors -------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [_error(400, "posix", "enoent"), _error(404, "notFound")],
    ids=["enoent", "404"],
)
async def test_check_an_enoent_or_404_from_the_listing_marks_the_dataset_unresolved(
    fed: Fed, response: httpx.Response
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed._listing = lambda root_id, body: response  # type: ignore[method-assign]

    result = await check()

    assert result["ready"] is False
    assert result["resolved_files"] == 0
    assert result["unresolved"] == [no_such_directory(SPACE)]


@pytest.mark.asyncio
async def test_check_another_listing_error_still_escapes(fed: Fed) -> None:
    from onedata_mcp.utils import OnedataApiError

    fed.add("a.nc", 100, local="full", owner="full")
    fed._listing = lambda root_id, body: _error(500, "internalServerError")  # type: ignore[method-assign]

    with pytest.raises(OnedataApiError):
        await check()


@pytest.mark.asyncio
async def test_check_reads_that_all_failed_with_the_rest_skipped_at_the_deadline_are_unchecked(
    fed: Fed, clock: Clock
) -> None:
    for i in range(40):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")

    def failing_and_slow(fid: str) -> httpx.Response:
        clock.now += readiness.CHECK_BUDGET_S + 1  # the first reads use up the budget
        return _error(500, "internalServerError")

    fed._distribution = failing_and_slow  # type: ignore[method-assign]

    result = await check()  # no exception: the failures are not the whole story

    assert result["ready"] is False
    assert result["unchecked"] == 40
    assert result["advice"] == "Not ready: 40 files could not be checked."


# --- 11. the hold claim -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_creates_again_a_rule_that_was_removed_since_the_last_call(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local="full", owner="full")
    await ensure()
    assert len(fed.rules) == 1
    fed.rules.clear()  # an agent removed it with remove_qos_requirement

    result = await ensure()

    assert held_summary(result) == {"files": 1, "rule": HOLD}
    assert [r["fileId"] for r in fed.rules.values()] == [f.fid]  # created again, and really there


@pytest.mark.asyncio
async def test_ensure_does_not_claim_a_hold_it_could_neither_confirm_nor_recreate(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local="full", owner="full")
    await ensure()
    fed.rules.clear()
    fed.qos_status = lambda fid, expression: 403

    result = await ensure()

    assert held_summary(result) == {"files": 0, "rule": HOLD}
    assert result["hold_failed"] == {
        "files": 1,
        "examples": [f.path],
        "causes": {"refused": 1},
    }
    assert result["ready"] is False


@pytest.mark.asyncio
async def test_ensure_confirms_each_remembered_rule_by_reading_it_and_creates_nothing(
    fed: Fed,
) -> None:
    for i in range(3):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")
    await ensure()
    fed.requests.clear()

    result = await ensure()

    reads = [p for m, p in fed.requests if m == "GET" and "/qos_requirements/" in p]
    posts = [p for m, p in fed.requests if m == "POST" and p.endswith("/qos_requirements")]
    assert len(reads) == 6  # each rule read back when placing, and again in the final pass
    assert posts == []
    assert held_summary(result) == {"files": 3, "rule": HOLD}


@pytest.mark.asyncio
async def test_ensure_confirms_remembered_rules_concurrently_and_within_the_limit(fed: Fed) -> None:
    for i in range(60):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")
    await ensure()
    fed.peak.clear()

    await ensure()

    assert 1 < fed.peak["qos_get"] <= readiness.CHECK_CONCURRENCY


# --- 12. transfers ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("scheduled", "in_transfer"),
        ("enqueued", "in_transfer"),
        ("active", "in_transfer"),
        ("replicating", "in_transfer"),  # the swagger's transferStatus for a running replication
        ("failed", "at_other_site"),
        ("cancelled", "at_other_site"),
        ("completed", "at_other_site"),
        ("skipped", "at_other_site"),
        ("aborting", "at_other_site"),
        ("evicting", "at_other_site"),
        (None, "at_other_site"),  # no state at all: not shown to be moving
    ],
)
async def test_check_only_a_transfer_that_is_still_going_counts_as_moving(
    fed: Fed, status: str | None, reason: str
) -> None:
    f = fed.add("a.nc", 100, owner="full")
    fed.transfers["t1"] = {
        "fileId": f.fid,
        "filePath": f.path,
        "replicatingProviderId": LOCAL,
        "status": status,
    }

    assert list((await check())["not_ready"]) == [reason]


# ---------------------------------------------------------------------------
# Last review batch
# ---------------------------------------------------------------------------

# --- A. size evidence -------------------------------------------------------------------------


def test_parse_distribution_reads_the_virtual_size_and_the_block_end() -> None:
    body = _body({OWNER: {**_entry(0, [[0, 40]]), "virtualSize": 60}})

    assert readiness.parse_distribution(body, 10).size == 60
    assert readiness.parse_distribution(_body({OWNER: _entry(0, [[0, 70]])}), 10).size == 70


def test_parse_distribution_a_block_end_raises_a_known_size_but_makes_none_up() -> None:
    body = _body(
        {OWNER: {"success": True, "distributionPerStorageBackend": {"s": {"blocks": [[0, 40]]}}}}
    )

    nothing = readiness.parse_distribution(body)  # no source gave a size
    some = readiness.parse_distribution(body, 10)  # the listing gave one

    assert nothing.size == 0
    assert not nothing.known
    assert some.size == 40


@pytest.mark.parametrize(
    "unreadable",
    [
        {"success": False, "error": {}},
        _entry(0, [[0, 0]]),  # a malformed block
        {**_entry(0), "distributionPerStorageBackend": {"s": {"success": False, "error": {}}}},
        {"success": True, "logicalSize": 0},  # no storage map
    ],
    ids=["failed", "malformed-block", "storage-failed", "no-storage-map"],
)
def test_parse_distribution_an_empty_file_needs_every_provider_entry_readable(
    unreadable: dict,
) -> None:
    dist = readiness.parse_distribution(_body({LOCAL: _entry(0), OWNER: unreadable}), 0)

    assert not dist.empty
    assert not dist.known
    assert dist.covering == frozenset()


@pytest.mark.asyncio
async def test_check_the_largest_block_end_at_any_provider_is_size_evidence(fed: Fed) -> None:
    f = fed.add("a.nc", 50, local="full", owner="full", other=[[0, 100]])
    f.logical = {LOCAL: 50, OWNER: 50, OTHER: 50}  # every stated size is stale

    result = await check()

    assert result["ready"] is False  # judged at 100: the local provider holds 50
    assert list(result["not_ready"]) == ["at_other_site"]  # the third provider holds all 100


@pytest.mark.asyncio
async def test_check_a_virtual_size_is_size_evidence(fed: Fed) -> None:
    f = fed.add("a.nc", 50, local="full", owner="full")
    f.logical = {LOCAL: 50, OWNER: 50, OTHER: 50}
    f.virtual = {OWNER: 100}

    result = await check()

    assert result["ready"] is False
    assert list(result["not_ready"]) == ["being_written"]


@pytest.mark.asyncio
async def test_check_a_virtual_size_alone_is_a_size(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local="full", owner="full")
    f.logical = {LOCAL: None, OWNER: None, OTHER: None}
    f.virtual = {OWNER: 100}
    _without_listing_size(fed)

    assert (await check())["ready"] is True


@pytest.mark.asyncio
async def test_check_overlapping_listings_keep_the_largest_size(fed: Fed) -> None:
    fed.add("sub/a.nc", 100, local="full", owner="full")
    listing = fed._listing

    def bigger_in_sub(root_id: str, body: dict) -> httpx.Response:
        response = listing(root_id, body)
        if not fed.dirs[root_id].endswith("/sub"):
            return response
        page = json.loads(response.content)
        for entry in page["files"]:
            entry["size"] = 200  # this listing, taken later, says the file is bigger
        return httpx.Response(200, json=page)

    fed._listing = bigger_in_sub  # type: ignore[method-assign]

    result = await readiness.check_readiness([{"dataset": SPACE}, {"dataset": f"/{SPACE}/sub"}])

    assert result["bytes"] == 200
    assert result["ready"] is False


@pytest.mark.asyncio
async def test_ensure_keeps_the_largest_size_seen_across_polls(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    attributes = fed._attributes
    reads: list[str] = []

    def high_first(fid: str, body: dict) -> httpx.Response:
        reads.append(fid)
        response = attributes(fid, body)
        if len(reads) > 1:
            return response
        return httpx.Response(200, json={**json.loads(response.content), "size": 200})

    fed._attributes = high_first  # type: ignore[method-assign]

    result = await ensure(deadline_s=15)

    assert result["ready"] is False  # a later, smaller size never lowers what was seen


# --- B. the final pass ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_a_false_flag_in_the_final_read_is_final(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    attributes = fed._attributes
    reads: list[str] = []

    def false_on_the_final_pass(fid: str, body: dict) -> httpx.Response:
        reads.append(fid)
        response = attributes(fid, body)
        if len(reads) == 1:
            return response
        return httpx.Response(
            200, json={**json.loads(response.content), "isFullyReplicatedLocally": False}
        )

    fed._attributes = false_on_the_final_pass  # type: ignore[method-assign]

    result = await ensure()

    # the distribution still shows every block here, and still cannot make the file ready
    assert result["ready"] is False
    assert "unchecked" not in result
    assert list(result["not_ready"]) == ["in_transfer"]  # its rule is pending


@pytest.mark.asyncio
async def test_ensure_the_final_pass_never_raises(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    attributes, distribution = fed._attributes, fed._distribution
    attribute_reads: list[str] = []
    distribution_reads: list[str] = []

    def lost_replica(fid: str, body: dict) -> httpx.Response:
        attribute_reads.append(fid)
        response = attributes(fid, body)
        if len(attribute_reads) == 1:
            return response
        return httpx.Response(
            200, json={**json.loads(response.content), "isFullyReplicatedLocally": False}
        )

    def distribution_fails_after_the_poll(fid: str) -> httpx.Response:
        distribution_reads.append(fid)
        if len(distribution_reads) == 1:
            return distribution(fid)
        return _error(500, "internalServerError")

    fed._attributes = lost_replica  # type: ignore[method-assign]
    fed._distribution = distribution_fails_after_the_poll  # type: ignore[method-assign]

    result = await ensure()  # every read of the final assessment fails: still an answer

    assert result["ready"] is False
    assert result["unchecked"] == 1


@pytest.mark.asyncio
async def test_ensure_the_final_pass_confirms_every_held_rule_and_counts_only_those(
    fed: Fed,
) -> None:
    for i in range(3):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")

    result = await ensure()

    reads = [p for m, p in fed.requests if m == "GET" and "/qos_requirements/" in p]
    assert len(reads) == 3  # created in this call, then read back in the final pass
    assert held_summary(result) == {"files": 3, "rule": HOLD}


@pytest.mark.asyncio
async def test_ensure_a_rule_that_vanishes_before_the_answer_is_not_counted_as_held(
    fed: Fed,
) -> None:
    a = fed.add("a.nc", 100, local="full", owner="full")
    attributes = fed._attributes
    reads: list[str] = []

    def removed_during_the_final_pass(fid: str, body: dict) -> httpx.Response:
        reads.append(fid)
        if len(reads) == 2:
            fed.rules.clear()  # an agent removes the rule while the answer is being made
        return attributes(fid, body)

    fed._attributes = removed_during_the_final_pass  # type: ignore[method-assign]

    result = await ensure()

    # the data are there, and no longer protected: not ready, the data ready on their own
    assert (result["ready"], result["data_ready"]) == (False, True)
    assert held_summary(result) == {"files": 0, "rule": HOLD}
    assert result["hold_failed"]["files"] == 1
    assert result["hold_failed"]["examples"] == [a.path]


# --- C. what confirms a rule ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "confirms"),
    [
        (lambda qid, fid: httpx.Response(200, json={"qosRequirementId": qid}), True),
        (lambda qid, fid: httpx.Response(200, json={"fileId": fid}), True),
        (
            lambda qid, fid: httpx.Response(200, json={"qosRequirementId": "x", "fileId": "y"}),
            False,
        ),
        (lambda qid, fid: httpx.Response(200, json={"status": "fulfilled"}), False),
        (lambda qid, fid: httpx.Response(200, json=[{"qosRequirementId": qid}]), False),
        (
            lambda qid, fid: httpx.Response(
                200, text="<html>sign in</html>", headers={"Content-Type": "text/html"}
            ),
            False,
        ),
        (lambda qid, fid: httpx.Response(302, headers={"Location": "/login"}), False),
        (lambda qid, fid: httpx.Response(204), False),
        (lambda qid, fid: httpx.Response(201, json={"qosRequirementId": qid}), False),
    ],
    ids=[
        "names-the-rule",
        "names-the-file",
        "names-something-else",
        "no-ids",
        "a-list",
        "html",
        "redirect",
        "no-content",
        "created-not-ok",
    ],
)
async def test_ensure_only_a_200_json_answer_naming_the_rule_or_the_file_confirms_it(
    fed: Fed, answer, confirms: bool
) -> None:
    """Anything but that is unreadable, not "gone": the rule may well be there, so no second
    one is placed beside it (Codex 6); the hold is not confirmed, and its id is listed."""
    f = fed.add("a.nc", 100, local="full", owner="full")
    readiness._rules[f.fid] = "q-remembered"
    fed.qos_get_override = lambda qid: answer(qid, f.fid)

    result = await ensure()

    posts = [e for e in fed.requests if e == ("POST", "/api/v3/oneprovider/qos_requirements")]
    assert posts == []  # confirmed or not, never placed again beside a rule that may be there
    assert result["held"]["files"] == (1 if confirms else 0)
    if not confirms:
        assert result["hold_failed"]["causes"] == {"not_confirmed": 1}
        assert result["hold_failed"]["unconfirmed_qos_requirement_ids"] == ["q-remembered"]
        assert result["ready"] is False and result["data_ready"] is True


@pytest.mark.asyncio
async def test_ensure_places_a_hold_again_only_when_the_provider_says_it_is_gone(fed: Fed) -> None:
    f = fed.add("a.nc", 100, local="full", owner="full")
    readiness._rules[f.fid] = "q-removed-meanwhile"

    result = await ensure()

    (new,) = fed.rules
    assert result["held"]["qos_requirement_ids"] == [new] and new != "q-removed-meanwhile"
    assert readiness._rules[f.fid] == new


# --- D. listing robustness --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_a_regular_entry_without_a_path_makes_the_listing_incomplete(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("b.nc", 100, local="full", owner="full")

    def without_a_path(page: dict) -> dict:
        page["files"][1].pop("path")
        return page

    patch_listing(fed, without_a_path)

    result = await check()

    assert result["resolved_files"] == 1
    assert result["unresolved"] == [listing_incomplete()]


@pytest.mark.asyncio
async def test_check_a_directory_entry_without_a_path_makes_the_listing_incomplete(
    fed: Fed,
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    patch_listing(
        fed, lambda page: {**page, "files": [*page["files"], {"type": "DIR", "fileId": "d"}]}
    )

    assert (await check())["unresolved"] == [listing_incomplete()]


@pytest.mark.asyncio
async def test_check_directory_and_symlink_entries_do_not_trouble_the_listing(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    extra = [
        {"type": "DIR", "fileId": "d", "path": "sub"},
        {"type": "SYMLNK", "fileId": "l", "path": "link"},
    ]
    patch_listing(fed, lambda page: {**page, "files": [*page["files"], *extra]})

    result = await check()

    assert result["ready"] is True
    assert result["resolved_files"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["FIFO", "", 7, None, ["REG"]])
async def test_check_an_unknown_file_type_makes_the_listing_incomplete(
    fed: Fed, kind: object
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    odd = {"type": kind, "fileId": "x", "path": "x"}
    patch_listing(fed, lambda page: {**page, "files": [*page["files"], odd]})

    result = await check()

    assert result["resolved_files"] == 1
    assert result["unresolved"] == [listing_incomplete()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        httpx.Response(200, json=None),
        httpx.Response(200, json=["a.nc"]),
        httpx.Response(200, text="<html>gateway</html>", headers={"Content-Type": "text/html"}),
        httpx.Response(200, json=7),
    ],
    ids=["null", "list", "html", "number"],
)
async def test_check_a_listing_body_that_is_not_an_object_is_incomplete(
    fed: Fed, body: httpx.Response
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed._listing = lambda root_id, request_body: body  # type: ignore[method-assign]

    result = await check()  # no exception

    assert result["ready"] is False
    assert result["unresolved"] == [listing_incomplete()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [_error(404, "notFound"), _error(400, "notFound")],
    ids=["404", "id-notFound"],
)
async def test_check_a_plain_404_from_the_root_lookup_is_no_such_directory(
    fed: Fed, response: httpx.Response
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed._lookup = lambda path: response  # type: ignore[method-assign]

    result = await check()

    assert result["resolved_files"] == 0
    assert result["unresolved"] == [no_such_directory(SPACE)]


@pytest.mark.asyncio
async def test_check_another_error_from_the_root_lookup_still_escapes(fed: Fed) -> None:
    from onedata_mcp.utils import OnedataApiError

    fed._lookup = lambda path: _error(500, "internalServerError")  # type: ignore[method-assign]

    with pytest.raises(OnedataApiError):
        await check()


# --- E. time and repeated declarations --------------------------------------------------------


@pytest.mark.asyncio
async def test_check_a_hanging_provider_configuration_read_answers_not_ready(
    fed: Fed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    monkeypatch.setattr(readiness, "_monotonic", time.monotonic)  # this test runs on real time
    monkeypatch.setattr(readiness, "CHECK_BUDGET_S", 0.5)
    fed.add("a.nc", 100, local="full", owner="full")
    fed.hang.add("config")

    started = time.monotonic()
    result = await check()

    assert time.monotonic() - started < 5  # the read alone would take 10 s
    assert result == {
        "ready": False,
        "resolved_files": 0,
        "ready_files": 0,
        "bytes": 0,
        "bytes_local": 0,
        "unresolved": [],
        "advice": (
            "Not ready: provider configuration unavailable, so nothing could be checked. "
            "Call again."
        ),
    }


@pytest.mark.asyncio
async def test_ensure_a_hanging_provider_configuration_read_answers_not_ready_and_places_nothing(
    fed: Fed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    monkeypatch.setattr(readiness, "_monotonic", time.monotonic)
    monkeypatch.setattr(readiness, "ENSURE_GRACE_S", 0.5)
    monkeypatch.setenv(readiness.MAX_WAIT_ENV, "0.5")
    fed.add("a.nc", 100, local="full", owner="full")
    fed.hang.add("config")

    started = time.monotonic()
    result = await ensure()

    assert time.monotonic() - started < 5
    assert result["ready"] is False
    assert result["advice"].startswith("Not ready after 1 s: provider configuration unavailable")
    assert held_summary(result) == {"files": 0, "rule": "providerId=<local provider> & anyStorage"}
    assert fed.rules == {}


@pytest.mark.asyncio
async def test_a_name_or_range_declared_twice_is_looked_for_once(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await check(
        files=["missing.nc"] * 5 + ["a.nc"] * 3,
        date_ranges=[{"start": "2018-09-01", "end": "2018-09-30"}] * 4,
    )

    assert result["resolved_files"] == 1
    assert result["unresolved"] == [
        file_not_found("missing.nc"),
        range_matched_nothing("2018-09-01", "2018-09-30"),
    ]


@pytest.mark.asyncio
async def test_twenty_thousand_repeats_of_one_name_cost_one_lookup(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")

    result = await check(files=["a.nc"] * 20_000)

    assert result["ready"] is True
    assert result["resolved_files"] == 1


# ---------------------------------------------------------------------------
# What the model reads
# ---------------------------------------------------------------------------


def test_no_answer_text_advises_the_agent_on_its_analysis() -> None:
    texts = []
    for ready in (True, False):
        for waited in (None, 0, 150):
            for data_ready in (False, True):
                for causes in ({}, dict.fromkeys(readiness.HOLD_CAUSES, 2)):
                    for reason in readiness.REASONS:
                        for n in (1, 5):
                            texts.append(
                                readiness._advice(
                                    ready=ready,
                                    counts={reason: n},
                                    unresolved=n,
                                    unchecked=n,
                                    waited_s=waited,
                                    data_ready=data_ready,
                                    hold_causes=causes,
                                )
                            )
    assert texts
    for text in texts:
        assert not re.search(r"comput|analy|do not|don't|should|must", text, re.IGNORECASE), text


# ---------------------------------------------------------------------------
# Gate 1 (design/10): every error a model can trigger says how to fix the call, and claims no
# more than the provider's answer shows. design/17 and design/18 list these paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        # a guessed shape (an external CLI leg guessed `paths` and `date_from`): refused, never
        # dropped, since an item left with neither files nor date_ranges is the whole dataset
        (
            [{"dataset": "x", "paths": ["a.nc"], "date_from": "2018-06-01"}],
            r"extent\[0\] has no field 'date_from', 'paths': its fields are dataset, files, "
            r"date_ranges",
        ),
        ([{"dataset": "x", "file": ["a.nc"]}], r"has no field 'file'"),
        (
            [{"dataset": "x", "date_ranges": [{"from": "2018-06-01", "to": "2018-06-02"}]}],
            r"extent\[0\]\.date_ranges\[0\] has no field 'from', 'to': its fields are start, end",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_unknown_field_is_refused_and_named(bad: list, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        await readiness.check_readiness(bad)


@pytest.mark.parametrize(
    "dataset",
    [
        f"/{SPACE}/..",
        f"/{SPACE}/a/../b",
        f"{SPACE}/./sub",
        f"/{SPACE}//sub",
        "/..",
        "/./x",
        f"//{SPACE}/sub",  # a second leading slash (Gemini 10)
        f"/{SPACE}/sub//",  # a second trailing slash
    ],
)
@pytest.mark.asyncio
async def test_a_dataset_with_a_dot_or_empty_component_is_refused_before_any_request(
    fed: Fed, dataset: str
) -> None:
    """`/my-dataset/..` would be resolved by the provider to the directory above every space."""
    with pytest.raises(ValueError, match=r"has an empty, '\.' or '\.\.' component: give a space"):
        await readiness.check_readiness([{"dataset": dataset}])
    with pytest.raises(ValueError, match=r"component"):
        await readiness.ensure_readiness([{"dataset": dataset}], 60)
    assert fed.requests == []


@pytest.mark.parametrize("dataset", [f"/{SPACE}/sub/", f"{SPACE}/sub", f"/{SPACE}"])
def test_a_trailing_slash_or_a_missing_leading_one_is_not_refused(dataset: str) -> None:
    (item,) = readiness._normalise_extent([{"dataset": dataset}])
    assert item.dataset == dataset


@pytest.mark.parametrize("dataset", [f"/{SPACE}/sub/", f"{SPACE}/sub/", f"{SPACE}/sub"])
@pytest.mark.asyncio
async def test_one_trailing_or_a_missing_leading_slash_names_the_same_directory(
    fed: Fed, dataset: str
) -> None:
    """Gemini 10: `/my-dataset/sub/` was kept as declared and its lookup failed, though the
    record said a trailing slash is accepted."""
    fed.add("sub/a.nc", 100, local="full", owner="full")

    result = await readiness.check_readiness([{"dataset": dataset}])

    assert (result["ready"], result["resolved_files"]) == (True, 1), result


@pytest.mark.parametrize("dataset", ["/..", "/.", "../x", "/./x"])
def test_a_dataset_with_no_space_segment_is_shown_a_real_example(dataset: str) -> None:
    """Gemini 10: `/..` was answered "give a space name such as '..'"."""
    with pytest.raises(ValueError) as caught:
        readiness._normalise_extent([{"dataset": dataset}])
    assert "such as 'my-dataset'" in str(caught.value)
    assert "such as '.'" not in str(caught.value) and "such as '..'" not in str(caught.value)


@pytest.mark.parametrize("name", ["./a.nc", "sub//a.nc", "sub/../a.nc", "sub/a.nc/", "/x//a.nc"])
@pytest.mark.asyncio
async def test_a_file_path_with_a_dot_or_empty_component_is_refused_before_any_request(
    fed: Fed, name: str
) -> None:
    """Codex 5: such a path selected nothing in the tool (names are matched exactly) and the
    file in the scorer's looser reading. Refused, with how to give it."""
    fed.add("a.nc", 100, local="full", owner="full")

    with pytest.raises(ValueError, match=r"files\[0\] .* has an empty, '\.' or '\.\.' component"):
        await check(files=[name])
    assert fed.requests == []


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        (
            [{"dataset": "x", "date_ranges": [{"start": "2020-02-30", "end": "2020-03-01"}]}],
            r"start '2020-02-30' is not a calendar date: give a real day, as YYYY-MM-DD",
        ),
        (
            [{"dataset": "x", "date_ranges": [{"start": "June 1", "end": "2020-03-01"}]}],
            r"must be a date in YYYY-MM-DD form, got 'June 1'",
        ),
        (
            [{"dataset": "x", "date_ranges": [{"start": "2020-03-02", "end": "2020-03-01"}]}],
            r"start 2020-03-02 is after end 2020-03-01; give the earlier day as start",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_date_range_says_how_to_fix_it(bad: list, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        await readiness.check_readiness(bad)


@pytest.mark.asyncio
async def test_the_names_of_the_whole_extent_are_capped_not_only_each_item(fed: Fed) -> None:
    half = [f"f{i}.nc" for i in range(readiness.MAX_FILES // 2 + 1)]
    with pytest.raises(ValueError, match=r"names more than 20000 files in all"):
        await readiness.check_readiness([{"dataset": SPACE, "files": half}] * 2)
    assert fed.requests == []


@pytest.mark.parametrize(
    "response",
    [_error(403, "forbidden"), _error(400, "posix", "eacces"), _error(400, "posix", "eperm")],
    ids=["403", "eacces", "eperm"],
)
@pytest.mark.asyncio
async def test_a_dataset_the_token_may_not_read_is_unresolved_with_the_reason(
    fed: Fed, response: httpx.Response
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    for which in ("lookup", "listing"):
        readiness.reset_state()
        original = fed._lookup, fed._listing
        if which == "lookup":
            fed._lookup = lambda path: response  # type: ignore[method-assign]
        else:
            fed._listing = lambda root_id, body: response  # type: ignore[method-assign]

        result = await check()

        fed._lookup, fed._listing = original  # type: ignore[method-assign]
        assert result["ready"] is False, which
        assert result["unresolved"] == [
            f"dataset '{SPACE}' cannot be listed: the provider refused this token access to "
            "it. Declare data the token can read."
        ], which


@pytest.mark.asyncio
async def test_a_5xx_carrying_enoent_is_not_read_as_a_missing_directory(fed: Fed) -> None:
    """A structured not-found counts only on a 4xx: a failing provider is not "not found"."""
    from onedata_mcp.utils import OnedataApiError

    fed.add("a.nc", 100, local="full", owner="full")
    fed._listing = lambda root_id, body: _error(500, "posix", "enoent")  # type: ignore[method-assign]

    with pytest.raises(OnedataApiError):
        await check()


@pytest.mark.asyncio
async def test_an_id_given_where_a_name_or_path_is_asked_for_is_named_as_an_id(fed: Fed) -> None:
    file_id = "0000000000526A3A67756964233136" + "a" * 40
    space_id = "c2a1b7d34f2e4b0f9a1c3e5d7f9b1d3e"
    fed.add("a.nc", 100, local="full", owner="full")

    by_id = await readiness.check_readiness([{"dataset": file_id}])
    as_file = await check(files=[file_id])

    assert by_id["unresolved"] == [
        f"dataset '{file_id}' not found: it looks like an id, and no space your provider lists "
        "for this token has that id (a file's or a directory's id is not taken here). Give a "
        "space's name or id, or an absolute directory path such as '/my-dataset/sub'."
    ]
    assert as_file["unresolved"] == [
        f"file '{file_id}' in '{SPACE}' not found: it looks like a file id, and files are given "
        "by their exact name or their absolute path."
    ]
    # a space's id is a legitimate dataset: the provider's list matches it by id
    fed.spaces[0]["spaceId"] = space_id
    readiness.reset_state()
    assert (await readiness.check_readiness([{"dataset": space_id}]))["ready"] is True


@pytest.mark.parametrize(
    ("status", "sentence"),
    [
        (
            403,
            "A hold could not be placed for 1 file: the provider refused this token the right "
            "to add QoS requirements.",
        ),
        (
            500,
            "A hold could not be placed for 1 file: the provider rejected the request; the "
            "next call tries again.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_hold_that_could_not_be_placed_says_why_as_far_as_the_provider_said(
    fed: Fed, status: int, sentence: str
) -> None:
    fed.add("a.nc", 100, owner="full")
    fed.qos_status = lambda fid, expression: status

    result = await ensure(deadline_s=5)

    assert result["hold_failed"]["files"] == 1
    assert sentence in result["advice"]


def test_no_answer_text_names_a_tool() -> None:
    """Either tool may be offered without the other (a read-only token prunes ensure_readiness;
    a scenario may offer one alone), so no answer names a tool (design/10, gate 2)."""
    texts = [
        readiness._advice(
            ready=ready,
            counts={reason: n},
            unresolved=n,
            unchecked=n,
            waited_s=waited,
            data_ready=data_ready,
            hold_causes={cause: 2} if cause else {},
        )
        for ready in (True, False)
        for waited in (None, 0, 150)
        for data_ready in (False, True)
        for cause in (None, *readiness.HOLD_CAUSES)
        for reason in readiness.REASONS
        for n in (1, 5)
    ]
    texts += [readiness._config_unavailable()["advice"], readiness._config_unavailable(3)["advice"]]
    for text in texts:
        assert not re.search(r"\b\w+_readiness\b|\b[a-z]+_[a-z_]+\(", text), text


# ---------------------------------------------------------------------------
# The operator's decisions (design/18): a call that ends under a common client timeout, and
# holds that can be released by their ids.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_default_ensure_call_ends_under_a_180_s_client_timeout(
    fed: Fed, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One wait of at most 150 s, and the whole call within 170 s. (On the fake, requests in
    flight at a cut-off add their latency to one clock: the slow-server test above allows for
    that; this one takes none.)"""
    monkeypatch.setattr(readiness, "POLL_INTERVAL_S", 30.0)
    for i in range(20):
        fed.add(f"data_201808{i + 10:02d}.nc", 100, owner="half")

    result = await ensure(deadline_s=86_400)

    assert readiness.MAX_WAIT_DEFAULT_S + readiness.ENSURE_GRACE_S == 170
    assert result["waited_s"] <= 150
    assert clock.elapsed <= 170 < 180


@pytest.mark.asyncio
async def test_the_answer_lists_the_ids_of_the_holds_in_path_order(fed: Fed) -> None:
    for rel in ("c.nc", "a.nc", "b.nc"):
        fed.add(rel, 100, local="full", owner="full")

    first = await ensure()
    again = await ensure()  # nothing is created: the remembered rules are read back

    by_file = {rule["fileId"]: qid for qid, rule in fed.rules.items()}
    want = [by_file[fed.by_rel(rel).fid] for rel in ("a.nc", "b.nc", "c.nc")]
    assert first["held"]["qos_requirement_ids"] == want
    assert again["held"]["qos_requirement_ids"] == want
    assert len(fed.rules) == 3
    assert "qos_requirement_ids_not_shown" not in first["held"]


@pytest.mark.asyncio
async def test_a_file_whose_hold_failed_has_no_id_in_the_answer(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    bad = fed.add("b.nc", 100, local="full", owner="full")
    fed.qos_status = lambda fid, expression: 403 if fid == bad.fid else 201

    result = await ensure()

    (only,) = fed.rules
    assert result["held"]["qos_requirement_ids"] == [only]
    assert result["hold_failed"]["examples"] == [bad.path]


@pytest.mark.asyncio
async def test_the_ids_shown_are_capped_and_the_rest_counted(
    fed: Fed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "MAX_HOLD_IDS_SHOWN", 2)
    for i in range(5):
        fed.add(f"f{i}.nc", 100, local="full", owner="full")

    result = await ensure()

    assert result["held"]["files"] == 5
    assert len(result["held"]["qos_requirement_ids"]) == 2
    assert result["held"]["qos_requirement_ids_not_shown"] == 3
    # where the others are found, naming no tool
    # how to get the others, naming no tool, and not "the QoS summary", which lists other
    # people's and inherited requirements too (Codex 6)
    assert result["held"]["note"] == (
        "Not every id is listed here (the first ones, in the files' path order, are). To list "
        "the others, call again with fewer files: a hold this server placed is read back and "
        "listed, not placed again."
    )
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", result["held"]["note"])


@pytest.mark.asyncio
async def test_a_hold_that_vanished_before_the_answer_is_not_listed(fed: Fed) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("b.nc", 100, local="full", owner="full")
    await ensure()
    gone = next(iter(fed.rules))
    real = fed._qos_get
    fed._qos_get = lambda qid: _error(404, "notFound") if qid == gone else real(qid)  # type: ignore[method-assign]

    result = await ensure()

    assert gone not in result["held"]["qos_requirement_ids"]
    assert held_summary(result)["files"] == len(result["held"]["qos_requirement_ids"])


# ---------------------------------------------------------------------------
# Ids in request paths go through `id_segment` (one encoded segment; empty, `.`, `..` and `/`
# refused before any request), as in every other tool since M-14's fix.
# ---------------------------------------------------------------------------


def _raw_paths(httpx_mock: HTTPXMock) -> list[str]:
    return [r.url.raw_path.decode() for r in httpx_mock.get_requests()]


@pytest.mark.asyncio
async def test_a_provider_id_is_one_encoded_segment_of_the_onezone_lookup(
    fed: Fed, httpx_mock: HTTPXMock
) -> None:
    run = readiness._Run(LOCAL, readiness._monotonic() + 60)

    assert await run.provider_online("p?x#y z") is True

    assert "/api/v3/onezone/providers/p%3Fx%23y%20z" in _raw_paths(httpx_mock)


@pytest.mark.parametrize("provider_id", ["", ".", "..", "p/../../user", "a/b"])
@pytest.mark.asyncio
async def test_a_provider_id_that_cannot_be_a_segment_is_not_looked_up(
    fed: Fed, httpx_mock: HTTPXMock, provider_id: str
) -> None:
    run = readiness._Run(LOCAL, readiness._monotonic() + 60)

    assert await run.provider_online(provider_id) is True  # best effort: taken as online
    assert not any("/providers/" in p for p in _raw_paths(httpx_mock))


@pytest.mark.asyncio
async def test_a_rule_id_is_one_encoded_segment_when_a_hold_is_confirmed(
    fed: Fed, httpx_mock: HTTPXMock
) -> None:
    f = fed.add("a.nc", 100, local="full", owner="full")
    readiness._rules[f.fid] = "q?1#2"
    run = readiness._Run(LOCAL, readiness._monotonic() + 60)
    info = readiness.FileInfo(f.fid, f.path, SPACE_ID)

    await readiness._rule_state(run, info, "q?1#2")

    assert "/api/v3/oneprovider/qos_requirements/q%3F1%232" in _raw_paths(httpx_mock)


@pytest.mark.parametrize("rule_id", [".", "..", "q/../../configuration"])
@pytest.mark.asyncio
async def test_a_rule_id_that_cannot_be_a_segment_is_not_confirmed_and_not_requested(
    fed: Fed, httpx_mock: HTTPXMock, rule_id: str
) -> None:
    f = fed.add("a.nc", 100, local="full", owner="full")
    readiness._rules[f.fid] = rule_id
    run = readiness._Run(LOCAL, readiness._monotonic() + 60)

    # an id that cannot be a segment names no rule: absent (placed again), never requested
    info = readiness.FileInfo(f.fid, f.path, SPACE_ID)
    assert await readiness._rule_state(run, info, rule_id) == readiness.ABSENT
    assert not any("/qos_requirements/" in p for p in _raw_paths(httpx_mock))


# ---------------------------------------------------------------------------
# The operator's decisions of 2026-11-23, second round (design/17, design/18): how many files are
# ready, and the pointer to ensure_readiness only where it is offered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_says_how_many_files_are_ready_and_keeps_the_examples(fed: Fed) -> None:
    for i in range(3):
        fed.add(f"ok{i}.nc", 100, local="full", owner="full")
    for i in range(4):
        fed.add(f"far{i}.nc", 100, owner="full")

    result = await check()

    assert result["ready"] is False and result["resolved_files"] == 7
    assert result["ready_files"] == 3
    assert result["not_ready"]["at_other_site"]["files"] == 4
    assert len(result["not_ready"]["at_other_site"]["examples"]) == 3  # still capped at three


@pytest.mark.asyncio
async def test_a_ready_check_and_a_ready_ensure_both_count_the_ready_files(
    fed: Fed,
) -> None:
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("b.nc", 100, local="full", owner="full")

    assert (await check())["ready_files"] == 2
    # ensure says it too (the operator, after the external review: a weak model read "held:
    # 5" as five ready files)
    assert (await ensure())["ready_files"] == 2


@pytest.mark.parametrize(
    ("offered", "rel", "blocks", "pointer"),
    [
        (True, "a.nc", {"owner": "full"}, True),  # at another site: it can place it here
        (True, "b.nc", {"owner": "half"}, True),  # being written: it can wait for it
        (False, "a.nc", {"owner": "full"}, False),  # not offered: never named
    ],
)
@pytest.mark.asyncio
async def test_check_names_ensure_readiness_only_where_it_is_offered(
    fed: Fed, offered: bool, rel: str, blocks: dict, pointer: bool
) -> None:
    fed.add(rel, 100, **blocks)

    result = await readiness.check_readiness(extent(), ensure_offered=offered)

    sentence = "ensure_readiness can wait for it and place it at your provider."
    assert (sentence in result["advice"]) is pointer
    assert ("ensure_readiness" in result["advice"]) is pointer


@pytest.mark.asyncio
async def test_no_pointer_where_ensure_readiness_cannot_help(fed: Fed) -> None:
    """Unreachable data: placing a rule would not bring them; a ready answer needs none."""
    fed.add("a.nc", 100, owner="full")
    fed.online[OWNER] = False
    down = await readiness.check_readiness(extent(), ensure_offered=True)
    assert "unreachable" in down["not_ready"]
    assert "ensure_readiness" not in down["advice"]

    fed.files.clear()
    fed.add("b.nc", 100, local="full", owner="full")
    readiness.reset_state()
    ready = await readiness.check_readiness(extent(), ensure_offered=True)
    assert ready["ready"] is True and "ensure_readiness" not in ready["advice"]


def test_only_the_offered_tool_is_ever_named() -> None:
    texts = [
        readiness._advice(
            ready=False,
            counts={reason: 2},
            unresolved=1,
            unchecked=1,
            waited_s=waited,
            hold_causes={"rejected": 1},
            ensure_offered=True,
        )
        for waited in (None, 0, 150)
        for reason in readiness.REASONS
    ]
    named = {m for text in texts for m in re.findall(r"\b\w+_readiness\b", text)}
    assert named == {"ensure_readiness"}
    # ensure's own answer never points to itself
    assert all("ensure_readiness" not in t for t in texts[len(readiness.REASONS) :])


# ---------------------------------------------------------------------------
# External review (design/17, design/18, "External review"): each BLOCKING finding, reproduced
# here first, then fixed. Each test names its finding.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [{"files": []}, {"date_ranges": []}, {"files": None}, {"date_ranges": None}],
    ids=["files-empty", "date_ranges-empty", "files-null", "date_ranges-null"],
)
@pytest.mark.asyncio
async def test_review_an_explicitly_empty_selector_is_refused_not_read_as_the_whole_dataset(
    fed: Fed, item: dict
) -> None:
    """Codex 1, Gemini 4: `files: []` (an empty search result passed on) held every file."""
    fed.add("a.nc", 100, owner="full")
    fed.add("b.nc", 100, owner="full")

    with pytest.raises(ValueError, match=r"is empty|is null"):
        await readiness.ensure_readiness(extent(**item), 60)
    assert fed.rules == {}
    assert fed.requests == []


@pytest.mark.asyncio
async def test_review_a_basename_matching_two_files_is_refused_and_neither_is_held(
    fed: Fed,
) -> None:
    """Codex 1, Gemini 5: a name with no "/" matched every file of that name, at any depth."""
    current = fed.add("current/log.dat", 100, owner="full")
    archive = fed.add("archive/log.dat", 100, owner="full")

    result = await ensure(deadline_s=5, files=["log.dat"])

    assert fed.rules == {}
    assert result["ready"] is False and result["resolved_files"] == 0
    (text,) = result["unresolved"]
    assert archive.path in text and current.path in text


@pytest.mark.asyncio
async def test_review_a_space_name_equal_to_another_space_id_is_refused_as_ambiguous(
    fed: Fed,
) -> None:
    """Codex 2: the first space whose name OR id matched won; a space named like another's id
    was never reached, and its caller held the other space's files."""
    fed.add("a.nc", 100, owner="full")
    fed.spaces.append({"name": SPACE_ID, "spaceId": "s2", "providers": []})

    result = await readiness.ensure_readiness([{"dataset": SPACE_ID}], 5)

    assert fed.rules == {}
    (text,) = result["unresolved"]
    assert SPACE in text and "s2" in text


@pytest.mark.asyncio
async def test_review_a_space_given_by_id_is_not_looked_up_by_a_name_two_spaces_share(
    fed: Fed,
) -> None:
    """Codex 2: a space given by its id was looked up again by its name, which another space
    may share: the lookup then reaches whichever space the provider takes for that name."""
    fed.add("a.nc", 100, local="full", owner="full")
    fed.spaces.append({"name": SPACE, "spaceId": "s2", "providers": []})

    await readiness.check_readiness([{"dataset": SPACE_ID}])

    by_name = f"/api/v3/oneprovider/lookup-file-id/%2F{SPACE}"
    assert not [p for m, p in fed.requests if m == "POST" and unquote(p) == unquote(by_name)]


@pytest.mark.asyncio
async def test_review_a_rule_read_back_while_the_provider_fails_is_not_placed_twice(
    fed: Fed,
) -> None:
    """Codex 6, Gemini 12: a read-back that failed (a 500) was taken for a rule that is gone,
    and a second rule was placed beside the first."""
    fed.add("a.nc", 100, local="full", owner="full")
    await ensure()
    (first,) = fed.rules
    fed.qos_get_override = lambda qid: _error(500, "internalServerError")

    await ensure(deadline_s=5)

    assert list(fed.rules) == [first]


@pytest.mark.asyncio
async def test_review_a_read_back_that_names_another_file_does_not_confirm_the_hold(
    fed: Fed,
) -> None:
    """Codex 6: a matching rule id confirmed the hold even when the answer named another file."""
    f = fed.add("a.nc", 100, local="full", owner="full")
    readiness._rules[f.fid] = "q-remembered"
    fed.qos_get_override = lambda qid: httpx.Response(
        200, json={"qosRequirementId": qid, "fileId": "another-file", "status": "fulfilled"}
    )

    result = await ensure(deadline_s=5)

    assert result["held"]["files"] == 0
    assert "q-remembered" not in result["held"]["qos_requirement_ids"]


@pytest.mark.asyncio
async def test_review_two_concurrent_ensures_place_one_hold_and_report_the_same_id(
    fed: Fed,
) -> None:
    """Codex 6: two calls at once both missed the remembered rule, both placed one, and both
    reported the second id while the first rule stayed."""
    fed.add("a.nc", 100, local="full", owner="full")

    first, second = await asyncio.gather(ensure(deadline_s=5), ensure(deadline_s=5))

    assert len(fed.rules) == 1
    (only,) = fed.rules
    assert first["held"]["qos_requirement_ids"] == second["held"]["qos_requirement_ids"] == [only]


@pytest.mark.asyncio
async def test_review_ensure_is_not_ready_when_no_hold_was_placed(fed: Fed) -> None:
    """Codex 7, Gemini 9: complete and readable here, every hold refused, and the answer
    began "Ready: ... and held there" with `ready: true`."""
    fed.add("a.nc", 100, local="full", owner="full")
    fed.qos_status = lambda fid, expression: 403

    result = await ensure(deadline_s=5)

    assert result["held"]["files"] == 0
    assert result["ready"] is False
    assert not result["advice"].startswith("Ready")


@pytest.mark.asyncio
async def test_review_the_advice_states_every_reason_not_only_the_largest(fed: Fed) -> None:
    """Gemini 3: three files at another site and two being written: the advice named the
    three only, and a model reading it reported the wrong reason."""
    for d in (1, 3, 5):
        fed.add(f"log_2020010{d}.dat", 100, owner="full")
    for d in (2, 4):
        fed.add(f"log_2020010{d}.dat", 100, owner="half")

    result = await check()

    assert "3 files are complete at another provider" in result["advice"]
    assert "2 files are still being written" in result["advice"]


# --- The space, chosen once and bound to its id (Codex 2) --------------------------------------


def _whole_id(uuid: str, space_id: str) -> str:
    """A 25.0-shaped file id (`api.files._file_id_parts` reads its space id)."""
    payload = f"guid#{uuid}#{space_id}".encode()
    return object_id(payload)


@pytest.mark.asyncio
async def test_a_directory_inside_a_space_whose_name_is_shared_is_not_looked_up_by_it(
    fed: Fed,
) -> None:
    fed.add("sub/a.nc", 100, local="full", owner="full")
    fed.spaces.append({"name": SPACE, "spaceId": "s2", "providers": []})

    result = await readiness.ensure_readiness([{"dataset": f"/{SPACE_ID}/sub"}], 5)

    assert fed.rules == {} and result["resolved_files"] == 0
    assert result["unresolved"] == [
        f"dataset '/{SPACE_ID}/sub': the name of its space, '{SPACE}', is also another space's "
        "name or id at your provider, so no path inside it is looked up by that name. Declare "
        f"the space itself by its id ('{SPACE_ID}') and name the files by their path inside it."
    ]
    assert not [p for m, p in fed.requests if m == "POST" and "lookup-file-id" in p]


@pytest.mark.asyncio
async def test_a_space_whose_name_is_shared_is_listed_by_its_own_directory_id(fed: Fed) -> None:
    """Given by its id, the space itself is listed by its directory's id, never by the name."""
    fed.add("sub/a.nc", 100, local="full", owner="full")
    fed.spaces.append({"name": SPACE, "spaceId": "s2", "providers": []})

    result = await readiness.check_readiness([{"dataset": SPACE_ID, "files": ["sub/a.nc"]}])

    assert (result["ready"], result["resolved_files"]) == (True, 1), result
    assert ("GET", "/api/v3/oneprovider/data/root-file-id/files") in fed.requests


@pytest.mark.asyncio
async def test_a_directory_found_in_another_space_is_refused(fed: Fed) -> None:
    """The lookup goes by the space's name: what it finds is checked against the space's id
    (a 25.0 id carries it), and a directory of another space is not used."""
    fed.add("sub/a.nc", 100, local="full", owner="full")
    elsewhere = _whole_id("u1", "another-space")
    fed._lookup = lambda path: httpx.Response(200, json={"fileId": elsewhere})  # type: ignore[method-assign]

    result = await ensure(deadline_s=5)

    assert fed.rules == {} and result["resolved_files"] == 0
    assert result["unresolved"] == [
        f"dataset '{SPACE}' resolved to a directory of another space (id 'another-space'), not "
        f"of the space '{SPACE}' (id '{SPACE_ID}'), so nothing there is checked. Declare the "
        "space by its id, or an absolute directory path inside it."
    ]


@pytest.mark.asyncio
async def test_the_ambiguous_space_text_names_both_and_how_to_tell_them_apart(fed: Fed) -> None:
    fed.spaces.append({"name": SPACE_ID, "spaceId": "s2", "providers": []})

    result = await readiness.check_readiness([{"dataset": SPACE_ID}])

    assert result["unresolved"] == [
        f"dataset '{SPACE_ID}': '{SPACE_ID}' names more than one space at your provider: the "
        f"space '{SPACE}' (id '{SPACE_ID}') and the space '{SPACE_ID}' (id 's2'). Give "
        f"'{SPACE}' or 's2' instead: each names one space only."
    ]


# --- Partial evidence is labelled, never completed (Codex 13) ----------------------------------


@pytest.mark.asyncio
async def test_a_file_not_on_a_listing_that_stopped_early_is_unknown_not_missing(
    fed: Fed, clock: Clock
) -> None:
    """A listing cut short after one page: a declared file on the unread page was called "not
    found", and `files: 1, ready_files: 1` looked complete beside `ready: false`."""
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("b.nc", 100, local="full", owner="full")
    original = fed._listing

    def first_page_then_stuck(root_id: str, body: dict) -> httpx.Response:
        response = original(root_id, {**body, "limit": 1})
        clock.now += readiness.CHECK_BUDGET_S  # the next page cannot come in time
        return response

    fed._listing = first_page_then_stuck  # type: ignore[method-assign]

    result = await check(files=["/my-dataset/a.nc", "b.nc", "c.nc"])

    assert result["ready"] is False
    # the one file found was found too late to be checked: said so, not counted ready
    assert (result["resolved_files"], result["ready_files"], result["unchecked"]) == (1, 0, 1)
    assert out_of_time() in result["unresolved"]
    for name in ("b.nc", "c.nc"):
        assert (
            f"file '{name}' in '{SPACE}' is unknown: it is not among the files listed before the "
            "listing stopped, so whether it is there is not known."
        ) in result["unresolved"]
    assert not any("not found" in text for text in result["unresolved"])


@pytest.mark.asyncio
async def test_a_name_seen_once_on_a_listing_that_stopped_early_is_not_taken(
    fed: Fed, clock: Clock
) -> None:
    """Another file of that name may be on the unread pages: it is asked for by its path."""
    fed.add("a.nc", 100, local="full", owner="full")
    fed.add("sub/a.nc", 100, local="full", owner="full")
    original = fed._listing

    def first_page_then_stuck(root_id: str, body: dict) -> httpx.Response:
        response = original(root_id, {**body, "limit": 1})
        clock.now += readiness.CHECK_BUDGET_S + readiness.MAX_WAIT_CAP_S + 60
        return response

    fed._listing = first_page_then_stuck  # type: ignore[method-assign]

    result = await readiness.ensure_readiness(extent(files=["a.nc"]), 60)

    assert fed.rules == {} and result["resolved_files"] == 0
    assert (
        f"file 'a.nc' in '{SPACE}' is not taken: the listing stopped early, so another file of "
        "that name may be in the rest of it. Give it by its path inside the dataset or its "
        "absolute path."
    ) in result["unresolved"]


@pytest.mark.asyncio
async def test_hold_failures_are_counted_by_the_cause_the_provider_showed(fed: Fed) -> None:
    """One refused hold made the advice say that every failure was a refusal."""
    refused = fed.add("a.nc", 100, local="full", owner="full")
    rejected = fed.add("b.nc", 100, local="full", owner="full")
    fed.add("c.nc", 100, local="full", owner="full")
    fed.qos_status = lambda fid, expression: {refused.fid: 403, rejected.fid: 500}.get(fid, 201)

    result = await ensure(deadline_s=5)

    assert result["hold_failed"]["causes"] == {"refused": 1, "rejected": 1}
    assert result["advice"] == (
        "Not ready after 0 s: the data are complete and readable at your provider now "
        "(data_ready), but 2 files are not held there. A hold could not be placed for 1 file: "
        "the provider refused this token the right to add QoS requirements. A hold could not "
        "be placed for 1 file: the provider rejected the request; the next call tries again."
    )


# --- Every phase within the caller's budget; the time measured at return (Codex 8, Gemini 8) ---


@pytest.mark.asyncio
async def test_a_short_deadline_bounds_placement_too(fed: Fed, clock: Clock) -> None:
    """With `deadline_s` 5, placing holds ran on for up to 150 s: only the wait was bounded."""
    for i in range(30):
        fed.add(f"f{i:02d}.nc", 100, local="full", owner="full")

    def slow_create(fid: str, expression: str) -> int:
        clock.now += 10  # each create takes 10 s
        return 201

    fed.qos_status = slow_create

    result = await ensure(deadline_s=5)

    # the first create overran the 5 s; nothing else was started after the budget
    assert len(fed.rules) == 1
    assert result["hold_failed"]["causes"] == {"out_of_time": 29}
    # the call's time is the one at return, and the whole call is within the budget, the
    # grace and the one request in flight at the cut-off
    assert result["waited_s"] == round(clock.elapsed)
    assert clock.elapsed <= 5 + readiness.ENSURE_GRACE_S + 10


@pytest.mark.asyncio
async def test_the_time_reported_includes_the_final_reads(fed: Fed, clock: Clock) -> None:
    """`waited_s` was taken before the final pass, which could add 20 s the answer did not
    say."""
    fed.add("a.nc", 100, local="full", owner="full")
    attributes = fed._attributes

    def slow(fid: str, body: dict) -> httpx.Response:
        clock.now += 7
        return attributes(fid, body)

    fed._attributes = slow  # type: ignore[method-assign]

    result = await ensure(deadline_s=5)

    assert result["waited_s"] == round(clock.elapsed) > 5


@pytest.mark.parametrize(
    ("deadline_s", "cap", "timing"),
    [
        (3600, None, (150.0, 20.0)),  # the default: within 170 s in all
        (60, None, (60.0, 20.0)),  # the caller's deadline bounds the work
        (3600, "30", (30.0, 4.0)),  # a lowered cap scales the grace down with it
        (10, "30", (10.0, 4.0)),
    ],
)
def test_the_budget_is_the_callers_and_the_grace_scales_with_the_cap(
    monkeypatch: pytest.MonkeyPatch, deadline_s: int, cap: str | None, timing: tuple
) -> None:
    if cap is not None:
        monkeypatch.setenv(readiness.MAX_WAIT_ENV, cap)
    assert readiness.ensure_timing(deadline_s) == timing


# ---------------------------------------------------------------------------
# The external review's verification round (design/17, design/18): each item reproduced first.
# ---------------------------------------------------------------------------


def _listing_of(n: int) -> readiness._Listing:
    from datetime import timedelta

    d0 = date(2000, 1, 1)
    files = [
        readiness.FileInfo(
            f"f{i}",
            f"/{SPACE}/d/f_{(d0 + timedelta(days=i % 9000)).strftime('%Y%m%d')}_{i}.nc",
            "s1",
        )
        for i in range(n)
    ]
    return readiness._Listing(files=files, root=f"/{SPACE}/d")


def _ranged(items: int, ranges: int) -> list[dict]:
    from datetime import timedelta

    d0 = date(2000, 1, 1)
    rs = [
        {
            "start": (d0 + timedelta(days=k)).isoformat(),
            "end": (d0 + timedelta(days=k + 30)).isoformat(),
        }
        for k in range(ranges)
    ]
    return [{"dataset": f"/{SPACE}/d", "date_ranges": rs} for _ in range(items)]


@pytest.mark.asyncio
async def test_verify_the_ranges_of_a_whole_extent_are_capped_before_any_request(fed: Fed) -> None:
    """Codex 8, Gemini 8: 20 items x 400 ranges was a valid extent; its selection over a
    20,000-file listing ran for minutes on a 1 s budget."""
    with pytest.raises(ValueError, match=r"date ranges in all"):
        await readiness.check_readiness(_ranged(20, 400))
    assert fed.requests == []


@pytest.mark.asyncio
async def test_verify_selection_stays_within_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex 8: the selection itself (no request) was not bounded by the deadline: 20 items x
    20 ranges over 20,000 files took seconds on a 1 s budget."""
    import time

    listing = _listing_of(20_000)

    async def listed(run, item):
        return listing

    monkeypatch.setattr(readiness, "_list_dataset", listed)
    items = readiness._normalise_extent(_ranged(20, 20))
    run = readiness._Run(LOCAL, time.monotonic() + 1.0)

    started = time.monotonic()
    await readiness._resolve(run, items)

    assert time.monotonic() - started < 1.5


@pytest.mark.asyncio
async def test_verify_a_hold_that_could_not_be_read_back_is_not_called_not_held(fed: Fed) -> None:
    """Codex 13: a read-back that timed out was reported as "1 file is not held there"; the
    observed fact is that the hold could not be confirmed."""
    f = fed.add("a.nc", 100, local="full", owner="full")
    readiness._rules[f.fid] = "q-remembered"
    fed.qos_get_override = lambda qid: _error(504, "timeout")

    result = await ensure(deadline_s=5)

    assert result["hold_failed"]["causes"] == {"not_confirmed": 1}
    assert "not held" not in result["advice"]
    assert "could not be confirmed" in result["advice"]


@pytest.mark.asyncio
async def test_a_selection_stopped_by_the_deadline_is_reported_and_holds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the deadline a large selection stops within one batch of files, and the item says
    why; a heavy extent within the caps ends within its budget."""
    import time

    listing = _listing_of(20_000)

    async def listed(run, item):
        return listing

    monkeypatch.setattr(readiness, "_list_dataset", listed)
    items = readiness._normalise_extent(_ranged(1, 400))
    run = readiness._Run(LOCAL, time.monotonic() - 1)  # the budget is gone already

    files, unresolved = await readiness._resolve(run, items)

    assert files == []
    assert unresolved == [
        f"dataset '/{SPACE}/d': its files were not selected within the time budget. Call "
        "again, or declare fewer files or date ranges."
    ]


# --- The review's second verification round: reproduced first ---------------------------------


@pytest.mark.asyncio
async def test_verify2_cached_items_after_a_late_listing_stop_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex 8, Gemini 8: a second listing returning just before the 1 s deadline, then 18 items
    selecting from the cached 20,000-file listing: no check between them, and the last was
    credited after the deadline."""
    import time

    big = _listing_of(19_990)  # with the second listing's file, under the 20,000-file cap
    small = readiness._Listing(files=[readiness.FileInfo("x", f"/{SPACE}/e/x.nc", "s1")])
    run = readiness._Run(LOCAL, time.monotonic() + 1.0)

    async def listed(run_, item):
        if item.root.endswith("/e"):
            await asyncio.sleep(max(run.deadline - time.monotonic() - 0.02, 0))
            return small
        return big

    monkeypatch.setattr(readiness, "_list_dataset", listed)
    every_day = [{"start": "2000-01-01", "end": "2030-12-31"}]
    extent = [
        {"dataset": f"/{SPACE}/d", "date_ranges": every_day},
        {"dataset": f"/{SPACE}/e"},
        *[{"dataset": f"/{SPACE}/d", "date_ranges": every_day}] * 18,
    ]
    items = readiness._normalise_extent(extent)

    started = time.monotonic()
    files, unresolved = await readiness._resolve(run, items)

    assert time.monotonic() - started < 1.13  # the call's whole allowance at a 1 s cap
    assert any("not selected within the time budget" in u for u in unresolved)


def test_verify2_the_range_cap_hint_never_widens_a_selection() -> None:
    """New MAJOR: "fewer, wider ranges" could make ensure hold the files in the gaps."""
    with pytest.raises(ValueError) as caught:
        readiness._normalise_extent(_ranged(2, 201))
    text = str(caught.value)
    assert "wider" not in text
    assert "overlap" in text and "split" in text
