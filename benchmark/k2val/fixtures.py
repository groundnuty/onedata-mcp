"""Fixtures of the validation panel: synthetic instrument logs in a scratch space.

All files live under `/<space>/logs-<trial>/` (a fresh subdirectory per trial, so a delete
followed by a re-create under the same names cannot race on 25.0) and are `log_YYYYMMDD.dat`,
256 KiB of seeded random bytes.

Kinds of file (design/08, Fixtures):

- `s0`: written through the OWNER provider, whole, so its blocks are at the owner and none are
  local. `POST /data/{parent}/children?name=..&type=REG`, then `PUT /data/{id}/content` with no
  offset (which truncates to 0 first: fine for a new file). Confirmed on the owner's distribution.
- `s0_local`: an `s0` file, then replicated to the local provider with a transfer; local coverage
  is confirmed from the blocks.
- `s0_qos`: an `s0` file with a QoS rule `providerId=<local id>`, waited until it is fulfilled
  and the file is locally covered.
- `s2`: being written, mirroring the preallocated mode of the reference ingest tool (its
  `preallocate` and `ingestsparse` steps): create at the owner; extend with an EMPTY write at the
  final size (`PUT ...?offset=<size>`, which on 25.0 sets the size and writes no block); write a
  prefix at offset 0; a background writer then writes the rest in parts, at their offsets, on a
  fixed schedule from the trial's first model turn. NEVER a write without an offset.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass, field
from typing import Literal

from benchmark.k2val.clock import Clock
from benchmark.k2val.federation import FedApi, FixtureError

FILE_SIZE = 256 * 1024
CHUNK = 64 * 1024
REST_PARTS = (FILE_SIZE - CHUNK) // CHUNK  # parts the background writer writes
SWEEP_PREFIX = "logs"

Kind = Literal["s0", "s0_local", "s0_qos", "s2"]

CONFIRM_TIMEOUT_S = 60.0
LOCAL_SEES_TIMEOUT_S = 120.0
REPLICATION_TIMEOUT_S = 180.0


def file_name(day: str) -> str:
    return f"log_{day}.dat"


def same_mode(seen: object, wanted: str) -> bool:
    """POSIX permissions as Onedata reports them (an octal string, `"600"` or `"0600"`, per the
    swagger; unverified on 25.0, which the live test prints) against the octal set."""
    return isinstance(seen, str) and seen.lstrip("0") == wanted.lstrip("0")


def content_for(name: str, size: int = FILE_SIZE) -> bytes:
    """Seeded random bytes: the same name always gives the same content."""
    seed = int.from_bytes(hashlib.sha256(f"k2val:{name}".encode()).digest()[:8], "big")
    return random.Random(seed).randbytes(size)


@dataclass(frozen=True)
class FileSpec:
    day: str  # YYYYMMDD
    kind: Kind = "s0"

    @property
    def name(self) -> str:
        return file_name(self.day)


@dataclass(frozen=True)
class EntrySpec:
    """A small file or a directory of the file-tool scenarios (FA, LC and CD; design/13-15), at
    `path` relative to the trial directory (`calib_runs/log_20200103.dat`).

    Made through the OWNER provider, parents first, with `content` as the whole body. `mode`,
    when set, is the file's POSIX permissions in octal (`"600"`), set after the write. The trial
    starts only once the LOCAL provider shows every entry as built: see
    `FixtureBuilder._wait_local_tree`."""

    path: str
    kind: Literal["file", "dir"] = "file"
    content: bytes = b""
    mode: str | None = None

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def onedata_type(self) -> str:
        return "DIR" if self.kind == "dir" else "REG"


@dataclass(frozen=True)
class FixtureSpec:
    files: tuple[FileSpec, ...]
    # seconds after the first model turn at which the background writer finishes
    writer_finish_s: float | None = None
    # the file-tool scenarios' small files and directories (design/13-15)
    entries: tuple[EntrySpec, ...] = ()


@dataclass
class BuiltFile:
    spec: FileSpec
    file_id: str
    path: str

    @property
    def name(self) -> str:
        return self.spec.name


class ScheduledWriter:
    """Writes the rest of each `s2` file in parts, at their offsets, on a fixed schedule.

    Part i (1..REST_PARTS) of every file is written at `finish_s * i / REST_PARTS` seconds
    after `start()`, so the last part lands at `finish_s`. After the last part the writer
    confirms on the owner's distribution that the file is covered, and only then stamps
    `finished_confirmed_at`.
    """

    def __init__(
        self,
        fed: FedApi,
        files: list[BuiltFile],
        finish_s: float,
        clock: Clock,
        owner_id: str,
    ) -> None:
        self.fed = fed
        self.files = files
        self.finish_s = finish_s
        self.clock = clock
        self.owner_id = owner_id
        self.t0: float | None = None
        self.parts: list[dict] = []
        self.last_part_started_at: float | None = None
        self.last_write_done_at: float | None = None
        self.finished_confirmed_at: float | None = None
        self.error: str | None = None
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self.t0 = self.clock.time()
        self._tasks = [asyncio.create_task(self._run_file(f)) for f in self.files]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def wait(self) -> None:
        await asyncio.gather(*self._tasks, return_exceptions=True)

    @property
    def finished(self) -> bool:
        return self.finished_confirmed_at is not None

    async def _run_file(self, file: BuiltFile) -> None:
        assert self.t0 is not None
        data = content_for(file.name)
        try:
            for i in range(1, REST_PARTS + 1):
                due = self.t0 + self.finish_s * i / REST_PARTS
                await self.clock.sleep(max(0.0, due - self.clock.time()))
                offset = CHUNK * i
                started = self.clock.time()
                if i == REST_PARTS:
                    self.last_part_started_at = max(self.last_part_started_at or 0.0, started)
                # an explicit offset on every write: without one Onedata truncates the file
                await self.fed.put("owner", file.file_id, data[offset : offset + CHUNK], offset)
                done = self.clock.time()
                self.parts.append(
                    {"file": file.name, "offset": offset, "started_at": started, "done_at": done}
                )
                if i == REST_PARTS:
                    self.last_write_done_at = max(self.last_write_done_at or 0.0, done)

            async def covered() -> bool:
                dist = await self.fed.distribution("owner", file.file_id)
                return dist.covers(self.owner_id)

            await self.fed.wait_until(
                covered, timeout_s=CONFIRM_TIMEOUT_S, what=f"owner covers {file.name}"
            )
            now = self.clock.time()
            self.finished_confirmed_at = max(self.finished_confirmed_at or 0.0, now)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - recorded; the trial is a deployment artefact
            self.error = f"{type(e).__name__}: {e}"

    def record(self) -> dict:
        return {
            "t0": self.t0,
            "finish_s": self.finish_s,
            "parts": self.parts,
            "last_part_started_at": self.last_part_started_at,
            "last_write_done_at": self.last_write_done_at,
            "finished_confirmed_at": self.finished_confirmed_at,
            "error": self.error,
        }


@dataclass
class BuiltFixture:
    space: str
    dir_name: str
    dir_id: str
    files: dict[str, BuiltFile] = field(default_factory=dict)
    qos_rules: dict[str, str] = field(default_factory=dict)  # file name -> rule id
    writer: ScheduledWriter | None = None
    started_at: float = 0.0
    ready_at: float = 0.0
    # The file-tool scenarios (design/13-15): the entries made, by relative path -> file id, and
    # the trial directory's tree as the LOCAL provider showed it before the agent started (the
    # baseline the oracles compare the end state with; `FedApi.tree`'s form).
    entries: dict[str, str] = field(default_factory=dict)
    baseline_tree: dict[str, dict] | None = None

    @property
    def dir_path(self) -> str:
        return f"/{self.space}/{self.dir_name}"

    def ids(self) -> dict[str, str]:
        """File ids by relative path, for a brief that gives an id (`{ids[log_20200102.dat]}`)."""
        return dict(self.entries)


class FixtureBuilder:
    def __init__(self, fed: FedApi, space: str, clock: Clock) -> None:
        self.fed = fed
        self.space = space
        self.clock = clock

    # --- state checks ---------------------------------------------------

    async def _confirm_at_owner(self, file_id: str, name: str, ids: tuple[str, str]) -> None:
        owner_id, local_id = ids

        async def at_owner_only() -> bool:
            d = await self.fed.distribution("owner", file_id)
            return d.size == FILE_SIZE and d.covers(owner_id) and d.bytes_of(local_id) == 0

        await self.fed.wait_until(
            at_owner_only,
            timeout_s=CONFIRM_TIMEOUT_S,
            what=f"owner covers {name} and the local provider holds 0 bytes",
        )

    async def _confirm_preallocated(self, file_id: str, name: str, ids: tuple[str, str]) -> None:
        owner_id, _ = ids

        async def preallocated() -> bool:
            d = await self.fed.distribution("owner", file_id)
            return d.size == FILE_SIZE and d.bytes_of(owner_id) == 0

        await self.fed.wait_until(
            preallocated,
            timeout_s=CONFIRM_TIMEOUT_S,
            what=f"{name} at its final size with no block written (empty write at the final size)",
        )

    async def _confirm_prefix(self, file_id: str, name: str, ids: tuple[str, str]) -> None:
        owner_id, _ = ids

        async def prefix_only() -> bool:
            d = await self.fed.distribution("owner", file_id)
            return d.size == FILE_SIZE and d.bytes_of(owner_id) == CHUNK

        await self.fed.wait_until(
            prefix_only,
            timeout_s=CONFIRM_TIMEOUT_S,
            what=f"owner holds exactly the prefix of {name}",
        )

    async def _wait_local_sees(self, built: BuiltFile) -> None:
        async def sees() -> bool:
            if await self.fed.lookup("local", built.path) != built.file_id:
                return False
            attrs = await self.fed.attrs("local", built.file_id, ["size"])
            return bool(attrs) and attrs.get("size") == FILE_SIZE

        await self.fed.wait_until(
            sees,
            timeout_s=LOCAL_SEES_TIMEOUT_S,
            what=f"the local provider resolves {built.path} at its final size",
        )

    # --- build ----------------------------------------------------------

    async def build(self, spec: FixtureSpec, dir_name: str) -> BuiltFixture:
        owner_id = await self.fed.provider_id("owner")
        local_id = await self.fed.provider_id("local")
        ids = (owner_id, local_id)
        root_id = await self.fed.lookup("owner", f"/{self.space}")
        if root_id is None:
            raise FixtureError(f"space {self.space} not found at the owner provider")
        fixture = BuiltFixture(
            space=self.space,
            dir_name=dir_name,
            dir_id="",
            started_at=self.clock.time(),
        )
        fixture.dir_id = await self.fed.mkdir("owner", root_id, dir_name)

        async def dir_visible() -> bool:
            return await self.fed.lookup("local", fixture.dir_path) == fixture.dir_id

        await self.fed.wait_until(
            dir_visible,
            timeout_s=LOCAL_SEES_TIMEOUT_S,
            what=f"the local provider resolves {fixture.dir_path}",
        )

        for fs in spec.files:
            data = content_for(fs.name)
            file_id = await self.fed.create_reg("owner", fixture.dir_id, fs.name)
            bf = BuiltFile(fs, file_id, f"{fixture.dir_path}/{fs.name}")
            fixture.files[fs.name] = bf
            if fs.kind == "s2":
                # 1. extend with an empty write at the final size; 2. confirm; 3. the prefix at
                # offset 0 (explicit: no write here ever goes without an offset); 4. confirm.
                await self.fed.put("owner", file_id, b"", FILE_SIZE)
                await self._confirm_preallocated(file_id, fs.name, ids)
                await self.fed.put("owner", file_id, data[:CHUNK], 0)
                await self._confirm_prefix(file_id, fs.name, ids)
            else:
                await self.fed.put("owner", file_id, data, None)  # whole body, no offset
                await self._confirm_at_owner(file_id, fs.name, ids)

        # The agent, the transfer and the QoS rule all work through the LOCAL provider: it must
        # know each file (at its final size) before it is asked to replicate or hold one.
        for bf in fixture.files.values():
            await self._wait_local_sees(bf)

        for fs in spec.files:
            bf = fixture.files[fs.name]
            if fs.kind == "s0_local":
                await self.fed.replicate("local", bf.file_id, local_id)
                await self._wait_local_covered(bf, local_id)
            elif fs.kind == "s0_qos":
                rule = await self.fed.add_qos("local", bf.file_id, f"providerId={local_id}")
                fixture.qos_rules[fs.name] = rule
                await self._wait_rule_fulfilled(bf, rule, local_id)

        if spec.entries:  # the file-tool scenarios (design/13-15)
            await self._build_entries(fixture, spec.entries)

        s2 = [fixture.files[f.name] for f in spec.files if f.kind == "s2"]
        if s2 and spec.writer_finish_s is not None:
            fixture.writer = ScheduledWriter(
                self.fed, s2, spec.writer_finish_s, self.clock, owner_id
            )
        fixture.ready_at = self.clock.time()
        return fixture

    async def _wait_local_covered(self, bf: BuiltFile, local_id: str) -> None:
        async def covered() -> bool:
            return (await self.fed.distribution("local", bf.file_id)).covers(local_id)

        await self.fed.wait_until(
            covered,
            timeout_s=REPLICATION_TIMEOUT_S,
            what=f"the local provider covers {bf.name} (blocks, not transfer status)",
        )

    async def _wait_rule_fulfilled(self, bf: BuiltFile, rule: str, local_id: str) -> None:
        async def done() -> bool:
            if await self.fed.qos_status("local", rule) != "fulfilled":
                return False
            return (await self.fed.distribution("local", bf.file_id)).covers(local_id)

        await self.fed.wait_until(
            done,
            timeout_s=REPLICATION_TIMEOUT_S,
            what=f"the placement rule on {bf.name} is fulfilled and the file is locally covered",
        )

    # --- the file-tool scenarios' entries (design/13-15) -----------------

    async def _build_entries(self, fixture: BuiltFixture, entries: tuple[EntrySpec, ...]) -> None:
        """Make each entry through the owner, parents first; then wait until the local provider
        shows exactly the built tree, and record it as the baseline."""
        ids = {"": fixture.dir_id}
        for entry in sorted(entries, key=lambda e: e.path.count("/")):
            parent, _, name = entry.path.rpartition("/")
            if parent not in ids:
                raise FixtureError(f"entry {entry.path}: its parent {parent} is not an entry")
            if entry.kind == "dir":
                file_id = await self.fed.mkdir("owner", ids[parent], name)
            else:
                file_id = await self.fed.create_reg("owner", ids[parent], name)
                if entry.content:
                    await self.fed.put("owner", file_id, entry.content, None)
                if entry.mode is not None:
                    await self.fed.set_mode("owner", file_id, entry.mode)
            ids[entry.path] = file_id
            fixture.entries[entry.path] = file_id
        fixture.baseline_tree = await self._wait_local_tree(fixture, entries)

    async def _wait_local_tree(
        self, fixture: BuiltFixture, entries: tuple[EntrySpec, ...]
    ) -> dict[str, dict]:
        """The local provider's tree of the trial directory, once it is exactly the one built:
        the same paths and ids, each of the built type, size and mode. A listing that fails
        meanwhile is retried until the timeout (a FixtureError, never an empty baseline)."""
        expected = {e.path: e for e in entries}
        tree: dict[str, dict] = {}

        async def as_built() -> bool:
            nonlocal tree
            try:
                tree = await self.fed.tree("local", fixture.dir_id)
            except Exception:  # noqa: BLE001 - not visible yet; the wait's timeout decides
                return False
            if set(tree) != set(expected):
                return False
            for path, entry in expected.items():
                seen = tree[path]
                if seen["fileId"] != fixture.entries[path] or seen["type"] != entry.onedata_type:
                    return False
                if entry.kind == "file" and seen.get("size") != len(entry.content):
                    return False
                if entry.mode is not None and not same_mode(seen.get("mode"), entry.mode):
                    return False
            return True

        await self.fed.wait_until(
            as_built,
            timeout_s=LOCAL_SEES_TIMEOUT_S,
            what=f"the local provider shows the {len(expected)} entries built in "
            f"{fixture.dir_path} (types, sizes, modes)",
        )
        return tree

    # --- reset ----------------------------------------------------------

    async def purge_dir(self, dir_id: str) -> list[str]:
        """Delete the QoS rules on a directory's files, then the directory. Returns problems."""
        problems: list[str] = []
        ids = [dir_id]
        try:
            ids.extend(k["fileId"] for k in await self.fed.children("owner", dir_id))
        except Exception as e:  # noqa: BLE001
            problems.append(f"list {type(e).__name__}: {e}")
        for fid in ids:
            try:
                for qid in await self.fed.qos_ids("local", fid):
                    await self.fed.delete_qos("local", qid)
            except Exception as e:  # noqa: BLE001
                problems.append(f"qos {type(e).__name__}: {e}")
        for fid in ids[1:]:
            try:
                await self.fed.delete("owner", fid)
            except Exception as e:  # noqa: BLE001
                problems.append(f"delete {type(e).__name__}: {e}")
        try:
            await self.fed.delete("owner", dir_id)

            async def all_absent() -> bool:
                return all([await self.fed.attrs("owner", fid, ["name"]) is None for fid in ids])

            await self.fed.wait_until(
                all_absent, timeout_s=CONFIRM_TIMEOUT_S, what="the owner answers the old ids absent"
            )
        except Exception as e:  # noqa: BLE001
            problems.append(f"absent {type(e).__name__}: {e}")
        return problems

    async def sweep_space(self) -> list[str]:
        """Once per config at run start: delete every `logs` / `logs-*` directory left in the
        space by an earlier run (with the QoS rules on its files), and wait until the owner
        answers the old ids absent. Per-trial teardown touches only the trial's own directory,
        so two trials of one config never wipe each other's live fixture."""
        root_id = await self.fed.lookup("owner", f"/{self.space}")
        if root_id is None:
            raise FixtureError(f"space {self.space} not found at the owner provider")
        problems: list[str] = []
        for kid in await self.fed.children("owner", root_id):
            name = kid.get("name", "")
            if kid.get("type") == "DIR" and (
                name == SWEEP_PREFIX or name.startswith(SWEEP_PREFIX + "-")
            ):
                problems.extend(await self.purge_dir(kid["fileId"]))
        return problems

    async def teardown(self, fixture: BuiltFixture) -> list[str]:
        if fixture.writer is not None:
            await fixture.writer.stop()
        return await self.purge_dir(fixture.dir_id)
