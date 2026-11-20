"""A snapshot of a trial's files as the two providers report them.

Taken right after the final answer (the mcp axis: was the claim true when it was made?) and
again after a bounded settle (the federation axis). The oracles are pure functions of snapshots.

Each provider is asked for its OWN blocks (the local provider for local coverage, the owner for
owner coverage), and each also reports what it sees of the other (`*_via_*`), so a live run
shows whether one provider's view of the other lags.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from benchmark.k2val.clock import Clock
from benchmark.k2val.federation import FedApi
from benchmark.k2val.fixtures import BuiltFixture


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


@dataclass
class Snapshot:
    taken_at: float
    files: dict[str, FileState] = field(default_factory=dict)

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
        return [f"{f.name}: {f.error}" for f in self.files.values() if f.error]

    def to_json(self) -> dict:
        return {
            "taken_at": self.taken_at,
            "files": {n: {**vars(f), "qos_ids": list(f.qos_ids)} for n, f in self.files.items()},
        }


async def _file_state(fed: FedApi, name: str, file_id: str, owner_id: str, local_id: str):
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
        if not view.size_known:
            unreadable.append(f"the {role} provider's distribution has no size")
        if pid in view.failed:
            unreadable.append(f"the {role} provider's distribution failed for its own entry")
    if unreadable:
        state.error = "; ".join(unreadable)
        return state
    state.size = max(via_owner.size, via_local.size)
    state.size_field = via_local.size_field or via_owner.size_field
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
        *[
            _file_state(fed, name, bf.file_id, owner_id, local_id)
            for name, bf in fixture.files.items()
        ]
    )
    return Snapshot(taken_at=taken, files={s.name: s for s in states})
