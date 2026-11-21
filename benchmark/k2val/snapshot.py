"""A snapshot of a trial's files as the two providers report them.

Taken right after the final answer (the mcp axis: was the claim true when it was made?) and
again after a bounded settle (the federation axis). The oracles are pure functions of snapshots.

Each provider is asked for its OWN blocks (the local provider for local coverage, the owner for
owner coverage), and each also reports what it sees of the other (`*_via_*`), so a live run
shows whether one provider's view of the other lags.

One usability rule for every scenario: a reading that fails or is malformed (a QoS summary
without its `requirements` map, a block map without a size, with the provider's own entry or one
of its storages failed, or with a size other than the one the fixture wrote; a tree listing that
fails) is recorded as an error in the snapshot, never as "0 bytes", "no rule" or "empty". A
snapshot with any error is not judged: `require_usable` raises `UnusableSnapshot`, and the
runner records the trial as ORACLE_ERROR (a harness outcome), never as a FAIL.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

from benchmark.k2val.clock import Clock
from benchmark.k2val.federation import FedApi
from benchmark.k2val.fixtures import BuiltFile, BuiltFixture


class UnusableSnapshot(RuntimeError):
    """A federation reading (at the answer, after the settle, or the baseline) could not be
    used: a harness or deployment problem, never a model's failure."""


def require_usable(snap: Snapshot | None, which: str) -> Snapshot:
    """The snapshot, or UnusableSnapshot when it is missing or holds an error."""
    if snap is None:
        raise UnusableSnapshot(f"no snapshot {which}")
    if errors := snap.errors():
        raise UnusableSnapshot(f"the snapshot {which} is unusable: " + "; ".join(errors))
    return snap


@dataclass
class FileState:
    name: str
    file_id: str
    size: int = 0
    local_bytes: int = 0
    owner_bytes: int = 0
    local_bytes_via_owner: int = 0
    owner_bytes_via_local: int = 0
    local_covered: bool = False
    owner_covered: bool = False
    qos_ids: tuple[str, ...] = ()
    size_field: str = ""
    error: str | None = None
    # the validated source observations: each provider's view of every provider (own size,
    # size field, merged intervals, or failed), kept so a stored trial can be rescored offline
    observed: dict | None = None


@dataclass
class Snapshot:
    taken_at: float
    files: dict[str, FileState] = field(default_factory=dict)
    # The file-tool scenarios (design/13-15): the trial directory's tree at the LOCAL provider
    # (`FedApi.tree`'s form, plus `content` for a small file that is not in the baseline), or the
    # error that kept it from being read. Never an empty tree in place of an unreadable one.
    tree: dict[str, dict] | None = None
    tree_error: str | None = None

    def all_local_covered(self, names: list[str] | None = None) -> bool:
        pick = names if names is not None else list(self.files)
        return bool(pick) and all(
            (f := self.files.get(n)) is not None and f.local_covered and f.error is None
            for n in pick
        )

    def all_owner_covered(self, names: list[str] | None = None) -> bool:
        pick = names if names is not None else list(self.files)
        return bool(pick) and all(
            (f := self.files.get(n)) is not None and f.owner_covered and f.error is None
            for n in pick
        )

    def any_qos(self, names: list[str] | None = None) -> bool:
        pick = names if names is not None else list(self.files)
        return any(self.files[n].qos_ids for n in pick if n in self.files)

    def errors(self) -> list[str]:
        out = [f"{f.name}: {f.error}" for f in self.files.values() if f.error]
        if self.tree_error:
            out.append(f"tree: {self.tree_error}")
        return out

    def to_json(self) -> dict:
        out: dict = {
            "taken_at": self.taken_at,
            "files": {n: {**vars(f), "qos_ids": list(f.qos_ids)} for n, f in self.files.items()},
        }
        if self.tree is not None or self.tree_error is not None:
            out["tree"] = self.tree
            out["tree_error"] = self.tree_error
        return out


async def _file_state(fed: FedApi, name: str, bf: BuiltFile, owner_id: str, local_id: str):
    file_id = bf.file_id
    state = FileState(name=name, file_id=file_id)
    try:
        via_owner, via_local, qos = await asyncio.gather(
            fed.distribution("owner", file_id),
            fed.distribution("local", file_id),
            fed.qos_ids("local", file_id, missing_ok=False),
        )
    except Exception as e:  # noqa: BLE001 - a snapshot records the problem, it does not raise
        state.error = f"{type(e).__name__}: {e}"
        return state
    # Each provider's own blocks must be readable: an answer without a size, or with the
    # provider's own entry failed, is an unusable reading, never "0 bytes".
    unreadable = []
    for role, view, pid in (("owner", via_owner, owner_id), ("local", via_local, local_id)):
        if not view.readable(pid):
            unreadable.append(
                f"the {role} provider's own entry of its distribution is missing or unreadable "
                "(no size, a failed or empty storage map, or a malformed interval)"
            )
        elif view.size_of(pid) != bf.spec.size:
            unreadable.append(
                f"the {role} provider reports size {view.size_of(pid)}, not the "
                f"{bf.spec.size} written"
            )
    state.observed = {"owner_view": via_owner.observed(), "local_view": via_local.observed()}
    if unreadable:
        state.error = "; ".join(unreadable)
        return state
    state.size = via_local.size_of(local_id)
    state.size_field = via_local.size_fields.get(local_id, "")
    state.owner_bytes = via_owner.bytes_of(owner_id)
    state.local_bytes_via_owner = via_owner.bytes_of(local_id)
    state.local_bytes = via_local.bytes_of(local_id)
    state.owner_bytes_via_local = via_local.bytes_of(owner_id)
    state.owner_covered = via_owner.covers(owner_id)
    state.local_covered = via_local.covers(local_id)
    state.qos_ids = qos
    return state


async def take_snapshot(fed: FedApi, fixture: BuiltFixture, clock: Clock) -> Snapshot:
    owner_id = await fed.provider_id("owner")
    local_id = await fed.provider_id("local")
    taken = clock.time()
    states = await asyncio.gather(
        *[_file_state(fed, name, bf, owner_id, local_id) for name, bf in fixture.files.items()]
    )
    snap = Snapshot(taken_at=taken, files={s.name: s for s in states})
    if fixture.baseline_tree is not None:
        await _take_tree(fed, fixture, snap)
    return snap


# A file the agent made with at most this many bytes has its content read (CDo checks it).
SMALL_FILE = 1024


async def _take_tree(fed: FedApi, fixture: BuiltFixture, snap: Snapshot) -> None:
    """The file-tool scenarios: the trial directory's tree at the LOCAL provider, where the agent
    works. A failed listing or read is recorded as `tree_error` (the oracle then refuses to
    judge), never as an empty tree."""
    known = {e["fileId"] for e in (fixture.baseline_tree or {}).values()}
    try:
        tree = await fed.tree("local", fixture.dir_id)
        for entry in tree.values():
            size = entry.get("size")
            small = entry.get("type") == "REG" and isinstance(size, int) and 0 < size <= SMALL_FILE
            if not small:
                continue
            # Every small file's bytes are read: a fixture file must keep its known bytes
            # (`sha256`), and a file the agent made must hold what was asked (`content`).
            data = await fed.content("local", entry["fileId"])
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            if entry.get("fileId") not in known:
                entry["content"] = data.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - recorded; the oracle refuses an unusable snapshot
        snap.tree_error = f"{type(e).__name__}: {e}"
        return
    snap.tree = tree
