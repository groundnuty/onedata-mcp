"""Fixtures: the REST call sequence for S0 and S2 against a fake of Onedata 25.0's shapes."""

from __future__ import annotations

import httpx
import pytest

from benchmark.k2val.federation import FedApi, FixtureError
from benchmark.k2val.fixtures import (
    CHUNK,
    FILE_SIZE,
    FileSpec,
    FixtureBuilder,
    FixtureSpec,
    content_for,
)
from benchmark.k2val.scenarios import SCENARIOS
from test.unit.k2val.fakefed import LOCAL_ID, OWNER_ID, FakeClock, FakeFed

SPACE = "k2val-glm"


def builder(api: FedApi, clock: FakeClock) -> FixtureBuilder:
    return FixtureBuilder(api, SPACE, clock)


def test_content_is_deterministic_and_256_kib() -> None:
    a = content_for("log_20200101.dat")
    assert len(a) == FILE_SIZE == 256 * 1024
    assert a == content_for("log_20200101.dat")
    assert a != content_for("log_20200102.dat")


@pytest.mark.asyncio
async def test_s0_is_written_whole_without_offset_through_the_owner(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(SCENARIOS["V4"].fixture, "logs-t1")

    assert built.dir_path == f"/{SPACE}/logs-t1"
    assert sorted(built.files) == [f"log_2020010{d}.dat" for d in range(1, 6)]
    for puts in ([p for p in fed.puts if p["file"] == bf.file_id] for bf in built.files.values()):
        (put,) = puts  # one write per file
        assert put["provider"] == OWNER_ID
        assert put["offset"] is None  # whole body, no offset (truncates a new file: fine)
        assert put["length"] == FILE_SIZE
    for bf in built.files.values():
        node = fed.nodes[bf.file_id]
        assert fed.covered(node.blocks[OWNER_ID], node.size) == FILE_SIZE
        assert LOCAL_ID not in node.blocks or node.blocks[LOCAL_ID] == []
    # the files sit under the trial's own directory, made at the owner
    assert (
        "POST",
        "https://owner.k2val.datahub.test",
        f"/api/v3/oneprovider/data/{built.dir_id}/children",
    ) in fed.requests


@pytest.mark.asyncio
async def test_s2_preallocates_with_an_empty_write_and_never_writes_without_an_offset(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    spec = FixtureSpec((FileSpec("20200102", "s2"),), writer_finish_s=90.0)

    built = await builder(api, clock).build(spec, "logs-t2")

    (bf,) = built.files.values()
    puts = [p for p in fed.puts if p["file"] == bf.file_id]
    # 1. the empty write at the final size, 2. the prefix at an EXPLICIT offset 0
    assert [(p["offset"], p["length"]) for p in puts] == [(FILE_SIZE, 0), (0, CHUNK)]
    assert all(p["offset"] is not None for p in puts)
    node = fed.nodes[bf.file_id]
    assert node.size == FILE_SIZE
    assert fed.covered(node.blocks[OWNER_ID], FILE_SIZE) == CHUNK  # prefix only

    # 3. the background writer: the rest in parts at their offsets, on a schedule
    writer = built.writer
    assert writer is not None
    writer.start()
    await writer.wait()
    puts = [p for p in fed.puts if p["file"] == bf.file_id]
    assert [(p["offset"], p["length"]) for p in puts[2:]] == [
        (CHUNK, CHUNK),
        (2 * CHUNK, CHUNK),
        (3 * CHUNK, CHUNK),
    ]
    assert all(p["offset"] is not None for p in puts)  # not one write without an offset
    assert fed.covered(node.blocks[OWNER_ID], FILE_SIZE) == FILE_SIZE
    record = writer.record()
    assert record["error"] is None
    assert (
        record["finished_confirmed_at"]
        >= record["last_write_done_at"]
        >= record["last_part_started_at"]
    )
    # the last part lands no earlier than finish_s after the start
    assert record["last_part_started_at"] >= record["t0"] + 90.0 - 1e-6


@pytest.mark.asyncio
async def test_writer_is_scheduled_from_start_not_from_build(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    spec = FixtureSpec((FileSpec("20200102", "s2"),), writer_finish_s=60.0)
    built = await builder(api, clock).build(spec, "logs-t3")
    node = next(iter(fed.nodes[bf.file_id] for bf in built.files.values()))
    puts_before = len(fed.puts)
    clock.now += 10_000  # a long time passes between build and the first model turn

    built.writer.start()
    await built.writer.wait()

    assert len(fed.puts) == puts_before + 3
    assert built.writer.t0 >= 1_010_000  # the schedule runs from start()
    assert fed.covered(node.blocks[OWNER_ID], FILE_SIZE) == FILE_SIZE


@pytest.mark.asyncio
async def test_s0_local_is_replicated_by_a_transfer_and_confirmed_from_blocks(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(SCENARIOS["V1"].fixture, "logs-t4")

    local = {n for n, b in built.files.items() if b.spec.kind == "s0_local"}
    assert local == {"log_20200102.dat", "log_20200105.dat"}
    assert len(fed.transfers) == 2
    for name, bf in built.files.items():
        node = fed.nodes[bf.file_id]
        got = fed.covered(node.blocks.get(LOCAL_ID, []), FILE_SIZE)
        assert (got == FILE_SIZE) == (name in local)


@pytest.mark.asyncio
async def test_s0_qos_has_a_fulfilled_rule_and_local_coverage(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(SCENARIOS["V3"].fixture, "logs-t5")

    (name, rule_id) = next(iter(built.qos_rules.items()))
    assert name == "log_20200107.dat"
    assert fed.rules[rule_id]["status"] == "fulfilled"
    assert fed.rules[rule_id]["expression"] == f"providerId={LOCAL_ID}"
    node = fed.nodes[built.files[name].file_id]
    assert fed.covered(node.blocks[LOCAL_ID], FILE_SIZE) == FILE_SIZE


@pytest.mark.asyncio
async def test_build_waits_until_the_local_provider_resolves_the_paths(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    fed.local_lag = 3  # the local provider needs 3 lookups before it sees a new node

    built = await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-t6")

    assert clock.sleeps >= 3
    assert await api.lookup("local", built.files["log_20200105.dat"].path) is not None


@pytest.mark.asyncio
async def test_build_fails_loudly_when_the_owner_never_covers_the_file(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fed._put

    def drops_writes(provider, fid, q, data):
        return httpx.Response(204)  # accepted, never stored

    monkeypatch.setattr(fed, "_put", drops_writes)
    assert original is not None

    with pytest.raises(FixtureError, match="owner covers log_20200105.dat"):
        await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-t7")


@pytest.mark.asyncio
async def test_a_preallocation_that_sets_no_size_is_reported(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Onedata ignored the empty write at the final size, the S2 fixture would be wrong:
    the build says so instead of carrying on."""
    original = fed._put

    def ignores_the_empty_write(provider, fid, q, data):
        if "offset" in q and not data:
            return httpx.Response(204)
        return original(provider, fid, q, data)

    monkeypatch.setattr(fed, "_put", ignores_the_empty_write)
    spec = FixtureSpec((FileSpec("20200102", "s2"),), writer_finish_s=30.0)

    with pytest.raises(FixtureError, match="final size"):
        await builder(api, clock).build(spec, "logs-t8")


@pytest.mark.asyncio
async def test_sweep_removes_old_logs_dirs_and_rules_and_teardown_touches_only_its_own(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    b = builder(api, clock)
    old = await b.build(SCENARIOS["V3"].fixture, "logs-old")  # has a QoS rule
    keep = fed.nodes[fed.roots[SPACE]]
    other = await b.build(SCENARIOS["V2"].fixture, "logs-sibling")

    # a sibling trial's directory is not touched by a teardown of another
    problems = await b.teardown(old)
    assert problems == []
    assert fed.find(f"/{SPACE}/logs-old") is None
    assert fed.rules == {}
    assert fed.find(other.dir_path) is not None

    # the sweep at the next run start removes whatever is left of earlier runs
    assert await b.sweep_space() == []
    assert fed.find(other.dir_path) is None
    assert fed.nodes[keep.fid] is keep  # the space root stays


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["V1", "V3"])
async def test_replication_and_qos_wait_until_the_local_provider_knows_the_files(
    api: FedApi, fed: FakeFed, clock: FakeClock, scenario: str
) -> None:
    """Live, the local provider can lag behind the owner: a transfer or a rule asked of it
    before it knows the file id fails. The build waits for the local provider to resolve every
    file first (the fake answers enoent to such a request)."""
    fed.local_lag = 3

    built = await builder(api, clock).build(SCENARIOS[scenario].fixture, "logs-lag")

    assert built.files
    for bf in built.files.values():
        assert fed.nodes[bf.file_id].local_lag == 0
    assert fed.transfers or fed.rules
