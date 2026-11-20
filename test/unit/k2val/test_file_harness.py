"""The harness of the file-tool scenarios (design/13-15) on the fake federation, with inline
specs: the entries, the baseline the trial waits for, the strict tree listing, the tree in the
snapshot, the ids in a brief, the scenario's own tool set, and the end-state check."""

from __future__ import annotations

import httpx
import pytest

from benchmark.k2val.federation import FedApi, FixtureError
from benchmark.k2val.file_oracles import UnusableSnapshot, _baseline, end_state, tree_delta
from benchmark.k2val.file_scenarios import FILE_TOOLS, FileScenario
from benchmark.k2val.fixtures import EntrySpec, FixtureBuilder, FixtureSpec
from benchmark.k2val.runner import _tools_for
from benchmark.k2val.scenarios import Scenario
from benchmark.k2val.snapshot import take_snapshot
from test.unit.k2val.fakefed import OWNER_ID, FakeClock, FakeFed
from test.unit.k2val.file_tool_support import ctx, snap

SPACE = "k2val-glm"
SPEC = FixtureSpec(
    (),
    entries=(
        EntrySpec("a.dat", "file", b"alpha\n", "600"),
        EntrySpec("sub", "dir"),
        EntrySpec("sub/b.dat", "file", b"beta\n"),
        EntrySpec("empty", "dir"),
    ),
)


def builder(api: FedApi, clock: FakeClock) -> FixtureBuilder:
    return FixtureBuilder(api, SPACE, clock)


# --- entries and the baseline ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entries_are_made_at_the_owner_and_the_baseline_is_the_local_tree(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(SPEC, "logs-t1")

    assert sorted(built.entries) == ["a.dat", "empty", "sub", "sub/b.dat"]
    for path, file_id in built.entries.items():
        assert fed.path_of(fed.nodes[file_id]) == f"/{SPACE}/logs-t1/{path}"
    assert fed.nodes[built.entries["a.dat"]].blocks == {OWNER_ID: [[0, 6]]}  # through the owner
    tree = built.baseline_tree
    assert {p: (e["type"], e["size"]) for p, e in tree.items()} == {
        "a.dat": ("REG", 6),
        "sub": ("DIR", None),
        "sub/b.dat": ("REG", 5),
        "empty": ("DIR", None),
    }
    assert tree["a.dat"]["mode"] == "600"
    assert {p: e["fileId"] for p, e in tree.items()} == built.entries
    assert built.files == {}  # no 256 KiB logs: the file tools need none


@pytest.mark.asyncio
async def test_the_trial_waits_until_the_local_provider_shows_the_mode(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mode that never shows locally is a deployment artefact (FixtureError), not a trial."""

    async def ignored(role, file_id, mode) -> None:
        return None

    monkeypatch.setattr(api, "set_mode", ignored)

    with pytest.raises(FixtureError, match=r"the local provider shows the 4 entries"):
        await builder(api, clock).build(SPEC, "logs-t2")


@pytest.mark.asyncio
async def test_an_entry_whose_parent_is_not_an_entry_is_a_fixture_error(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    spec = FixtureSpec((), entries=(EntrySpec("missing/x.dat", "file", b"x"),))

    with pytest.raises(FixtureError, match=r"its parent missing is not an entry"):
        await builder(api, clock).build(spec, "logs-t3")


@pytest.mark.asyncio
async def test_teardown_removes_the_trial_directory_with_its_entries(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    b = builder(api, clock)
    built = await b.build(SPEC, "logs-t4")

    assert await b.teardown(built) == []
    assert all(fid not in fed.nodes for fid in built.entries.values())


# --- the tree: strict, and in the snapshot --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_listing_raises_and_is_never_an_empty_tree(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await builder(api, clock).build(SPEC, "logs-t5")
    monkeypatch.setattr(fed, "_children", lambda fid, body: httpx.Response(200, json={}))

    with pytest.raises(Exception, match=r"has no 'children' list"):
        await api.tree("local", built.dir_id)
    snapshot = await take_snapshot(api, built, clock)

    assert snapshot.tree is None and "has no 'children' list" in snapshot.tree_error
    assert snapshot.errors() == [f"tree: {snapshot.tree_error}"]
    assert snapshot.to_json()["tree_error"] == snapshot.tree_error


@pytest.mark.asyncio
async def test_a_listing_that_is_not_last_but_has_no_token_raises(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await builder(api, clock).build(SPEC, "logs-t6")
    page = {"children": [], "isLast": False, "nextPageToken": None}
    monkeypatch.setattr(fed, "_children", lambda fid, body: httpx.Response(200, json=page))

    with pytest.raises(Exception, match=r"not last, but no token"):
        await api.tree("local", built.dir_id)


@pytest.mark.asyncio
async def test_the_tree_is_read_page_by_page(api: FedApi, fed: FakeFed, clock: FakeClock) -> None:
    spec = FixtureSpec((), entries=tuple(EntrySpec(f"f{i:04d}", "file", b"x") for i in range(3)))
    built = await builder(api, clock).build(spec, "logs-t7")
    real = fed._children
    fed._children = lambda fid, body: real(fid, {**body, "limit": 2})  # pages of two

    assert sorted(await api.tree("local", built.dir_id)) == ["f0000", "f0001", "f0002"]


@pytest.mark.asyncio
async def test_the_snapshot_reads_the_text_of_a_small_file_the_agent_made(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(SPEC, "logs-t8")
    assert (
        fed._create(built.entries["sub"], {"name": "new.txt", "type": "REG"}, b"ok").status_code
        == 201
    )

    snapshot = await take_snapshot(api, built, clock)

    assert snapshot.tree["sub/new.txt"]["content"] == "ok"
    assert "content" not in snapshot.tree["a.dat"]  # a fixture file is not read again


@pytest.mark.asyncio
async def test_a_fixture_without_entries_takes_no_tree(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(FixtureSpec(()), "logs-t9")

    snapshot = await take_snapshot(api, built, clock)

    assert snapshot.tree is None and snapshot.tree_error is None
    assert "tree" not in snapshot.to_json()


# --- the brief's ids, and the scenario's own tool set -----------------------------------------


@pytest.mark.asyncio
async def test_a_brief_gets_the_built_ids_and_a_placeholder_before(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    scenario = Scenario("X", "x", "K1", "Is {ids[a.dat]} in `{dir}`?", SPEC)
    before = scenario.render_brief(directory="/d", local="v")
    built = await builder(api, clock).build(SPEC, "logs-t10")

    after = scenario.render_brief(directory="/d", local="v", ids=built.ids())

    assert before == "Is <id of a.dat> in `/d`?"
    assert after == f"Is {built.entries['a.dat']} in `/d`?"


def test_a_scenario_offers_its_own_tools_when_it_names_them(cfg) -> None:
    deps = type("Deps", (), {"cfg": cfg})()
    own = Scenario("X", "x", "K1", "b", SPEC, tools=FILE_TOOLS)
    tiered = Scenario("Y", "y", "K1", "b", SPEC)

    assert _tools_for(deps, own) == frozenset(FILE_TOOLS)
    assert _tools_for(deps, tiered) == frozenset(cfg.tools_k1)
    assert {"delete_file", "move_file"}.isdisjoint(FILE_TOOLS)


# --- the end state ----------------------------------------------------------------------------

BASE = {"log.dat": {"fileId": "f1", "type": "REG", "size": 24, "mode": "664"}}
MAKE_DIR = FileScenario(
    id="X",
    title="x",
    tool="create_directory",
    coverage="requires",
    brief="b",
    fixture=SPEC,
    tools=FILE_TOOLS,
    answer="a",
    rules="r",
    expect="created",
    added={"new": "DIR", "new/f.txt": "REG"},
    added_content={"new/f.txt": "ok"},
)


def test_tree_delta_sees_additions_removals_and_changes() -> None:
    now = {
        "log.dat": {"fileId": "f9", "type": "REG", "size": 24, "mode": "664"},
        "new": {"fileId": "d1", "type": "DIR"},
    }
    assert tree_delta(BASE, now) == {"added": ["new"], "removed": [], "changed": ["log.dat"]}
    assert tree_delta(BASE, {})["removed"] == ["log.dat"]
    resized = {"log.dat": {**BASE["log.dat"], "size": 25}}
    assert tree_delta(BASE, resized)["changed"] == ["log.dat"]


def test_end_state_wants_exactly_the_added_entries_of_their_type_and_text() -> None:
    good = {
        **BASE,
        "new": {"fileId": "d1", "type": "DIR"},
        "new/f.txt": {"fileId": "f2", "type": "REG", "content": "ok\n"},
    }
    assert end_state(MAKE_DIR, BASE, good) == []
    assert end_state(MAKE_DIR, BASE, BASE) == ["missing ['new', 'new/f.txt']"]
    wrong_text = {**good, "new/f.txt": {**good["new/f.txt"], "content": "OK!"}}
    assert end_state(MAKE_DIR, BASE, wrong_text) == ["new/f.txt holds 'OK!', not 'ok'"]
    as_dir = {**good, "new/f.txt": {"fileId": "d2", "type": "DIR"}}
    assert end_state(MAKE_DIR, BASE, as_dir) == ["new/f.txt is DIR, not REG"]
    extra = {**good, "new/g": {"fileId": "f3", "type": "REG"}}
    assert end_state(MAKE_DIR, BASE, extra) == ["UNREQUESTED ['new/g']"]
    without = {k: v for k, v in good.items() if k != "log.dat"}
    assert end_state(MAKE_DIR, BASE, without) == ["removed ['log.dat']"]


def test_no_baseline_is_never_judged() -> None:
    with pytest.raises(UnusableSnapshot, match="no baseline tree"):
        _baseline(ctx("X", snap(BASE), baseline=None))


# --- the known bytes of the fixture's files (review: a rewrite under the same id is a change) ---


@pytest.mark.asyncio
async def test_the_baseline_and_the_snapshots_carry_each_small_files_hash(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    import hashlib

    built = await builder(api, clock).build(SPEC, "logs-t9")
    assert built.baseline_tree["a.dat"]["sha256"] == hashlib.sha256(b"alpha\n").hexdigest()
    assert "sha256" not in built.baseline_tree["sub"]

    snap_ = await take_snapshot(api, built, clock)
    assert snap_.tree["a.dat"]["sha256"] == built.baseline_tree["a.dat"]["sha256"]
    assert tree_delta(built.baseline_tree, snap_.tree)["changed"] == []


def test_content_changed_under_the_same_id_size_and_mode_is_a_change() -> None:
    base = {"a.dat": {"fileId": "f1", "type": "REG", "size": 6, "mode": "600", "sha256": "x"}}
    now = {"a.dat": {**base["a.dat"], "sha256": "y"}}
    assert tree_delta(base, now)["changed"] == ["a.dat"]


# --- the listing is bounded (review: a repeated token must not loop) -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pages", "match"),
    [
        ([{"children": [{"name": "x"}], "isLast": False, "nextPageToken": "t1"}] * 3, "repeats"),
        ([{"children": [], "isLast": False}], "no token"),
        ([{"isLast": True}], "no 'children' list"),
    ],
)
async def test_a_malformed_listing_raises_never_loops(
    api: FedApi, monkeypatch: pytest.MonkeyPatch, pages: list, match: str
) -> None:
    from benchmark.k2val import federation
    from onedata_mcp.utils import OnedataApiError

    replies = iter(pages)

    async def request(cfg, method, path, json_body=None, **kw):
        return {"status_code": 200, "body": next(replies)}

    monkeypatch.setattr(federation, "request", request)
    with pytest.raises(OnedataApiError, match=match):
        await api.tree("local", "d0")


@pytest.mark.asyncio
async def test_a_listing_of_too_many_pages_raises(
    api: FedApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark.k2val import federation
    from onedata_mcp.utils import OnedataApiError

    n = iter(range(10_000))

    async def request(cfg, method, path, json_body=None, **kw):
        return {
            "status_code": 200,
            "body": {"children": [], "isLast": False, "nextPageToken": f"t{next(n)}"},
        }

    monkeypatch.setattr(federation, "request", request)
    with pytest.raises(OnedataApiError, match=f"more than {federation.MAX_LISTING_PAGES} pages"):
        await api.tree("local", "d0")
