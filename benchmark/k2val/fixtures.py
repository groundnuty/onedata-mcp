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
- `s0_qos_owner`: an `s0` file with a QoS rule `providerId=<owner id>` (one replica), waited
  until it is fulfilled; the local provider still holds none of it (the stable tools' smoke
  scenarios, design/16).
- `s0_qos_both`: an `s0` file with a QoS rule `providerId=<owner id>|providerId=<local id>` and
  two replicas, waited until it is fulfilled and the file is locally covered (design/16).
- The transfer that replicates an `s0_local` file is recorded (`BuiltFixture.transfers`) and
  kept in the file's baseline (`transfer_id`), so an oracle knows which transfer made the copy.
- Any kind but `s2` may be larger than the standard 256 KiB (`FileSpec.size`): such a file is
  written in `PART`-sized writes at explicit offsets, so memory stays bounded. The copy scenarios
  of `schedule_file_replication` (SR1, design/12) use `LARGE_FILE_SIZE`, which takes a transfer
  a moment (about 2 s on an unloaded 25.0 federation); SR1's oracle therefore asks that a "whole"
  claim be read off the block map after scheduling, not that the transfer be seen running.
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
# list_children's largest `limit` (onedata_mcp/modules/files.py): LCp's names must lie beyond it.
PAGE_LIMIT_MAX = 100
CHUNK = 64 * 1024
# SR1's file (design/12, gate 4). Calibrated by the live test (test/live/test_transfer_live.py) on
# 2026-11-21, unloaded: whole 2.06 s after scheduling, about 62 MiB/s. Kept at 128 MiB by decision:
# "in transfer" is rarely observable, so SR1 and SR1i fail a "whole" claim not read off the block
# map after scheduling instead (design/12, "A whole claim is read from the blocks").
LARGE_FILE_SIZE = 128 * 1024 * 1024
# A large file's writes. Small enough to finish within the REST client's default 5 s timeout on
# a CPU-capped provider; if a live write still times out, lower it.
PART = 4 * 1024 * 1024
REST_PARTS = (FILE_SIZE - CHUNK) // CHUNK  # parts the background writer writes
SWEEP_PREFIX = "logs"

Kind = Literal["s0", "s0_local", "s0_qos", "s0_qos_owner", "s0_qos_both", "s2"]
# The kinds whose start state holds no local block (the others start locally covered).
NOT_LOCAL_KINDS = ("s0", "s2", "s0_qos_owner")
# The kinds with a QoS rule the fixture makes: kind -> (the providers its expression names,
# replicas). The expression is `providerId=<id>` per provider, joined by `|`.
QOS_KINDS: dict[str, tuple[tuple[str, ...], int]] = {
    "s0_qos": (("local",), 1),
    "s0_qos_owner": (("owner",), 1),
    "s0_qos_both": (("owner", "local"), 2),
}

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
    size: int = FILE_SIZE

    def __post_init__(self) -> None:
        if self.kind == "s2" and self.size != FILE_SIZE:
            raise ValueError("an s2 file has the standard size: its writer's parts assume it")

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
    # Names that must NOT be on the trial directory's first page at the listing tool's largest
    # limit (`PAGE_LIMIT_MAX`), as the local provider lists it: checked before the agent starts,
    # so that an answer needs a second page whatever order the provider lists in (LCp).
    beyond_first_page: tuple[str, ...] = ()
    # The fixture's transfers (of its `s0_local` files) must all be among the first this many
    # transfers the LOCAL provider lists as ended in the space, before the agent starts (ST4,
    # design/16): an answer must not depend on reading the space's whole transfer history. That
    # 25.0 lists ended transfers newest first is unverified; if it does not, the build fails
    # (RESET_FAIL), never a model.
    transfers_listed_within: int | None = None


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
    # each file as the local provider reported it before the agent started (`_wait_files_baseline`)
    files_baseline: dict[str, dict] = field(default_factory=dict)
    writer: ScheduledWriter | None = None
    started_at: float = 0.0
    ready_at: float = 0.0
    # The file-tool scenarios (design/13-15): the entries made, by relative path -> file id, and
    # the trial directory's tree as the LOCAL provider showed it before the agent started (the
    # baseline the oracles compare the end state with; `FedApi.tree`'s form).
    entries: dict[str, str] = field(default_factory=dict)
    baseline_tree: dict[str, dict] | None = None
    # The transfer that replicated each `s0_local` file to the local provider: file name -> id.
    transfers: dict[str, str] = field(default_factory=dict)

    @property
    def dir_path(self) -> str:
        return f"/{self.space}/{self.dir_name}"

    def ids(self) -> dict[str, str]:
        """File ids by relative path, for a brief that gives an id (`{ids[log_20200102.dat]}`):
        the entries, the log files, and the trial directory itself (`.`); and the id of the
        transfer that copied a log file, as `{ids[transfer:log_20200116.dat]}`."""
        return {
            ".": self.dir_id,
            **{n: bf.file_id for n, bf in self.files.items()},
            **self.entries,
            **{f"transfer:{n}": tid for n, tid in self.transfers.items()},
        }

    def baseline(self) -> dict:
        """The start state the agent got, as the LOCAL provider reported it before the first
        turn: each log file (`files`, `_wait_files_baseline`) and the trial directory's tree
        (`tree`, the file-tool scenarios, else None). The artefact's `fixture_baseline`."""
        return {"files": dict(self.files_baseline), "tree": self.baseline_tree}


def _baseline_holds(kind: str, reading: dict, want_qos: tuple[str, ...], size: int) -> bool:
    """The state a kind of file starts a trial in (see the module docstring), as the local
    provider's own entry reads it: its size, its blocks and its effective QoS summary. An `s2`
    file is at its final (preallocated) size with no block here and no rule, like an `s0`."""
    if not reading["size_known"] or reading["size"] != size:
        return False
    if tuple(reading["qos_ids"]) != want_qos:
        return False
    if kind in NOT_LOCAL_KINDS:
        return reading["local_bytes"] == 0
    return reading["local_covered"]


class FixtureBuilder:
    def __init__(self, fed: FedApi, space: str, clock: Clock) -> None:
        self.fed = fed
        self.space = space
        self.clock = clock

    # --- state checks ---------------------------------------------------

    async def _confirm_at_owner(
        self, file_id: str, name: str, ids: tuple[str, str], size: int = FILE_SIZE
    ) -> None:
        owner_id, local_id = ids

        async def at_owner_only() -> bool:
            d = await self.fed.distribution("owner", file_id)
            return d.size_of(owner_id) == size and d.covers(owner_id) and d.bytes_of(local_id) == 0

        await self.fed.wait_until(
            at_owner_only,
            timeout_s=CONFIRM_TIMEOUT_S,
            what=f"owner covers {name} and the local provider holds 0 bytes",
        )

    async def _confirm_preallocated(self, file_id: str, name: str, ids: tuple[str, str]) -> None:
        owner_id, _ = ids

        async def preallocated() -> bool:
            d = await self.fed.distribution("owner", file_id)
            return (
                d.readable(owner_id)
                and d.size_of(owner_id) == FILE_SIZE
                and d.bytes_of(owner_id) == 0
            )

        await self.fed.wait_until(
            preallocated,
            timeout_s=CONFIRM_TIMEOUT_S,
            what=f"{name} at its final size with no block written (empty write at the final size)",
        )

    async def _confirm_prefix(self, file_id: str, name: str, ids: tuple[str, str]) -> None:
        owner_id, _ = ids

        async def prefix_only() -> bool:
            d = await self.fed.distribution("owner", file_id)
            return (
                d.readable(owner_id)
                and d.size_of(owner_id) == FILE_SIZE
                and d.bytes_of(owner_id) == CHUNK
            )

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
            return bool(attrs) and attrs.get("size") == built.spec.size

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
            data = content_for(fs.name) if fs.size == FILE_SIZE else b""
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
            elif fs.size == FILE_SIZE:
                await self.fed.put("owner", file_id, data, None)  # whole body, no offset
                await self._confirm_at_owner(file_id, fs.name, ids)
            else:  # a large file: parts at explicit offsets, each made on its own
                for offset in range(0, fs.size, PART):
                    part = content_for(f"{fs.name}@{offset}", min(PART, fs.size - offset))
                    await self.fed.put("owner", file_id, part, offset)
                await self._confirm_at_owner(file_id, fs.name, ids, fs.size)

        # The agent, the transfer and the QoS rule all work through the LOCAL provider: it must
        # know each file (at its final size) before it is asked to replicate or hold one.
        for bf in fixture.files.values():
            await self._wait_local_sees(bf)

        role_ids = {"owner": owner_id, "local": local_id}
        for fs in spec.files:
            bf = fixture.files[fs.name]
            if fs.kind == "s0_local":
                fixture.transfers[fs.name] = await self.fed.replicate("local", bf.file_id, local_id)
                await self._wait_local_covered(bf, local_id)
            elif fs.kind in QOS_KINDS:
                roles, replicas = QOS_KINDS[fs.kind]
                expression = "|".join(f"providerId={role_ids[r]}" for r in roles)
                rule = await self.fed.add_qos("local", bf.file_id, expression, replicas)
                fixture.qos_rules[fs.name] = rule
                await self._wait_rule_fulfilled(
                    bf, rule, local_id, local=fs.kind not in NOT_LOCAL_KINDS
                )

        if spec.transfers_listed_within:  # ST4 (design/16)
            await self._wait_transfers_listed(fixture, spec.transfers_listed_within)

        if spec.entries:  # the file-tool scenarios (design/13-15)
            await self._build_entries(fixture, spec.entries, spec.beyond_first_page)

        s2 = [fixture.files[f.name] for f in spec.files if f.kind == "s2"]
        if s2 and spec.writer_finish_s is not None:
            fixture.writer = ScheduledWriter(
                self.fed, s2, spec.writer_finish_s, self.clock, owner_id
            )
        await self._wait_files_baseline(fixture, spec, local_id)
        fixture.ready_at = self.clock.time()
        return fixture

    async def _wait_files_baseline(
        self, fixture: BuiltFixture, spec: FixtureSpec, local_id: str
    ) -> None:
        """Before the agent starts: each file's state, as the LOCAL provider reports it (its own
        blocks, the size, the effective QoS summary, read strictly), is the one its kind builds;
        recorded in `fixture.files_baseline`. A reading that fails or differs is retried until
        the timeout (a FixtureError, never a trial on an unknown start). `s2` files are recorded
        only: their writer has not started yet, and their own waits confirmed the prefix."""
        for fs in spec.files:
            bf = fixture.files[fs.name]
            want_qos = (fixture.qos_rules[fs.name],) if fs.name in fixture.qos_rules else ()
            last: dict[str, dict] = {}

            async def as_built(
                bf: BuiltFile = bf, kind: str = fs.kind, want_qos=want_qos, last=last
            ) -> bool:
                reading = await self._local_reading(bf, local_id)
                if reading is None:
                    return False
                last["reading"] = reading
                return _baseline_holds(kind, reading, want_qos, bf.spec.size)

            await self.fed.wait_until(
                as_built,
                timeout_s=CONFIRM_TIMEOUT_S,
                what=f"the local provider reports {fs.name} as its kind {fs.kind} builds it "
                f"(blocks, size, QoS requirements {list(want_qos)})",
            )
            fixture.files_baseline[fs.name] = {"kind": fs.kind, **last["reading"]}
            if fs.name in fixture.transfers:  # which transfer made the local copy (ST4, ST4i)
                fixture.files_baseline[fs.name]["transfer_id"] = fixture.transfers[fs.name]

    async def _local_reading(self, bf: BuiltFile, local_id: str) -> dict | None:
        """The local provider's own view of a file, or None while it cannot be read."""
        try:
            dist = await self.fed.distribution("local", bf.file_id)
            qos = await self.fed.qos_ids("local", bf.file_id, missing_ok=False)
        except Exception:  # noqa: BLE001 - not readable yet; the caller's timeout decides
            return None
        return {
            "size": dist.size_of(local_id),
            "size_known": dist.readable(local_id),
            "local_bytes": dist.bytes_of(local_id),
            "local_covered": dist.covers(local_id),
            "qos_ids": list(qos),
            "observed": dist.observed(),
        }

    async def _wait_local_covered(self, bf: BuiltFile, local_id: str) -> None:
        async def covered() -> bool:
            return (await self.fed.distribution("local", bf.file_id)).covers(local_id)

        await self.fed.wait_until(
            covered,
            timeout_s=REPLICATION_TIMEOUT_S,
            what=f"the local provider covers {bf.name} (blocks, not transfer status)",
        )

    async def _wait_rule_fulfilled(
        self, bf: BuiltFile, rule: str, local_id: str, *, local: bool = True
    ) -> None:
        """The rule is fulfilled and, when it keeps a copy at the local provider (`local`), the
        file is locally covered (blocks, not the rule's status alone)."""

        async def done() -> bool:
            if await self.fed.qos_status("local", rule) != "fulfilled":
                return False
            return not local or (await self.fed.distribution("local", bf.file_id)).covers(local_id)

        await self.fed.wait_until(
            done,
            timeout_s=REPLICATION_TIMEOUT_S,
            what=f"the placement rule on {bf.name} is fulfilled"
            + (" and the file is locally covered" if local else ""),
        )

    async def _wait_transfers_listed(self, fixture: BuiltFixture, within: int) -> None:
        """ST4 (design/16): the fixture's transfers are all among the first `within` transfers
        the LOCAL provider lists as ended in the space (waited for: a transfer whose blocks have
        arrived may not read ended yet). Else a FixtureError: the listing does not put them
        first (its order on 25.0 is unverified), and the scenario must not reach a model."""
        if not fixture.transfers:
            raise FixtureError("transfers_listed_within is set, but the fixture made no transfer")
        wanted = set(fixture.transfers.values())
        space_id = await self.fed.transfer_space_id("local", next(iter(wanted)))

        async def listed() -> bool:
            return wanted <= set(await self.fed.ended_transfers("local", space_id, within))

        await self.fed.wait_until(
            listed,
            timeout_s=CONFIRM_TIMEOUT_S,
            what=f"the local provider lists the fixture's {len(wanted)} transfers among the first "
            f"{within} ended transfers of the space (newest first is assumed; unverified on 25.0)",
        )

    # --- the file-tool scenarios' entries (design/13-15) -----------------

    async def _build_entries(
        self, fixture: BuiltFixture, entries: tuple[EntrySpec, ...], beyond: tuple[str, ...] = ()
    ) -> None:
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
        if beyond:
            await self._check_beyond_first_page(fixture, beyond)

    async def _check_beyond_first_page(self, fixture: BuiltFixture, names: tuple[str, ...]) -> None:
        listed, last = await self.fed.first_page_names("local", fixture.dir_id, PAGE_LIMIT_MAX)
        on_first = sorted(set(names) & set(listed))
        if last or on_first:
            raise FixtureError(
                f"{fixture.dir_path}: at limit {PAGE_LIMIT_MAX} the local provider's first page "
                + ("is the last" if last else f"already lists {on_first}")
                + ": an answer would not need a second page"
            )

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
            # Each small file's bytes, read back at the local provider (status 200, the length
            # written), must be the bytes written: a wrong start is the fixture's, never a model's.
            for path, entry in expected.items():
                if entry.kind != "file" or not entry.content:
                    continue
                try:
                    data = await self.fed.content("local", fixture.entries[path])
                except Exception:  # noqa: BLE001 - not readable yet; the timeout decides
                    return False
                if data != entry.content:
                    return False
                tree[path]["sha256"] = hashlib.sha256(data).hexdigest()
            return True

        await self.fed.wait_until(
            as_built,
            timeout_s=LOCAL_SEES_TIMEOUT_S,
            what=f"the local provider shows the {len(expected)} entries built in "
            f"{fixture.dir_path} (types, sizes, modes, and each small file's bytes read back)",
        )
        return tree

    # --- reset ----------------------------------------------------------

    async def purge_dir(self, dir_id: str, tracked_rules: tuple[str, ...] = ()) -> list[str]:
        """Delete the QoS rules the fixture made (`tracked_rules`) and any rule set ON one of the
        directory's own entries, then the entries and the directory. Returns problems.

        A file's QoS summary is effective: it also lists rules inherited from its ancestors, up
        to the space root. A rule found there is deleted only when its own `fileId` is the entry
        itself, so a rule set anywhere above the trial is not touched here (the space root's own
        rules are `purge_root_rules`')
        """
        problems: list[str] = []
        ids = [dir_id]
        try:
            ids.extend(k["fileId"] for k in await self.fed.children("owner", dir_id))
        except Exception as e:  # noqa: BLE001
            problems.append(f"list {type(e).__name__}: {e}")
        for qid in tracked_rules:
            try:
                await self.fed.delete_qos("local", qid)
            except Exception as e:  # noqa: BLE001
                problems.append(f"qos {type(e).__name__}: {e}")
        for fid in ids:
            try:
                for qid in await self.fed.qos_ids("local", fid):
                    if qid not in tracked_rules and await self.fed.qos_file_id("local", qid) == fid:
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

    async def purge_root_rules(self) -> list[str]:
        """Delete every QoS rule ATTACHED to the space root (its own `fileId` is the root): a
        model's debris that every later file of the space would inherit, so that every later
        fixture would fail its start state. Rules attached anywhere else are left alone."""
        problems: list[str] = []
        try:
            root_id = await self.fed.lookup("owner", f"/{self.space}")
            if root_id is None:
                return [f"space {self.space} not found at the owner provider"]
            for qid in await self.fed.qos_ids("local", root_id):
                if await self.fed.qos_file_id("local", qid) == root_id:
                    await self.fed.delete_qos("local", qid)
        except Exception as e:  # noqa: BLE001
            problems.append(f"root qos {type(e).__name__}: {e}")
        return problems

    async def sweep_space(self) -> list[str]:
        """Once per config at run start: delete the QoS rules attached to the space root, and
        every `logs` / `logs-*` directory left in the space by an earlier run (with the QoS
        rules on its files), and wait until the owner answers the old ids absent. Per-trial
        teardown touches only the trial's own directory and the root's rules, so two trials of
        one config never wipe each other's live fixture."""
        root_id = await self.fed.lookup("owner", f"/{self.space}")
        if root_id is None:
            raise FixtureError(f"space {self.space} not found at the owner provider")
        problems: list[str] = await self.purge_root_rules()
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
        problems = await self.purge_dir(fixture.dir_id, tuple(fixture.qos_rules.values()))
        return problems + await self.purge_root_rules()
