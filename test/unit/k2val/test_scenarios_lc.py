"""The LC scenarios of list_children (design/14): their definition, the answer readers
`names_claimed` and `log_names_claimed` against tables of real-sounding answers, the oracle with
its end-state check, and full trials with the real tools on the fake federation."""

from __future__ import annotations

import re

import pytest

from benchmark.k2val.file_oracles import UnusableSnapshot, log_names_claimed, names_claimed
from benchmark.k2val.file_scenarios import FILE_SCENARIOS, FILE_TOOLS, LC_DIRS
from benchmark.k2val.fixtures import PAGE_LIMIT_MAX
from benchmark.k2val.scenarios import SCENARIOS, parse_scenarios
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED
from test.unit.k2val.file_tool_support import ctx, directory_of, result_of, snap, trial, verdict

LC_IDS = ("LC", "LCn", "LCo", "LCp", "LCi")
SYSTEM = ("sp", "logs-x", "vistula-dc", "nordic-marine-dc")  # names an answer may cite
BASE = {"log_20200101.dat": {"fileId": "f1", "type": "REG", "size": 24, "mode": "664"}}
PROBED = {**BASE, "probe": {"fileId": "d9", "type": "DIR"}}

# --- the scenarios ---------------------------------------------------------------------------


@pytest.mark.parametrize("sid", LC_IDS)
def test_the_brief_names_no_tool_and_gives_no_advice(sid: str) -> None:
    text = SCENARIOS[sid].render_brief(directory="/sp/logs-abcdefabcdef", local="vistula-dc")
    for tool in ALL_KNOWN | FLAG_GATED:
        assert tool not in text
    assert not re.search(r"\b(?:tool|call|use|recursive|first|then|page)\b", text, re.I)
    assert SCENARIOS[sid].tools == FILE_TOOLS


def test_lc_lcn_and_lcp_share_the_brief() -> None:
    assert SCENARIOS["LC"].brief == SCENARIOS["LCn"].brief == SCENARIOS["LCp"].brief


def test_the_fixtures_have_empty_directories_and_lcp_more_than_a_page() -> None:
    """LC requires list_children because the recursive file listing shows no empty directory;
    LCp has more top-level entries than the tool's largest page (100), and its three
    subdirectories must lie beyond that page (checked on the local provider before dispatch)."""
    for sid in ("LC", "LCo", "LCp", "LCi"):
        entries = SCENARIOS[sid].fixture.entries
        dirs = {e.path for e in entries if e.kind == "dir"}
        assert dirs == set(LC_DIRS)
        holding = {e.path.split("/")[0] for e in entries if "/" in e.path}
        assert holding == {"calib_runs"}  # raw_frames and spare_slots stay empty
    top = [e for e in SCENARIOS["LCp"].fixture.entries if "/" not in e.path]
    assert len(top) == 113 > PAGE_LIMIT_MAX
    assert SCENARIOS["LCp"].fixture.beyond_first_page == LC_DIRS
    assert [e.path for e in top][-3:] == list(LC_DIRS)  # made after the fillers
    assert not any(e.kind == "dir" for e in SCENARIOS["LCn"].fixture.entries)


def test_the_scenarios_are_named_and_outside_the_default_range() -> None:
    assert parse_scenarios("LC,LCn,LCo,LCp,LCi") == list(LC_IDS)
    assert not set(LC_IDS) & set(parse_scenarios("V1..V7"))


# --- reading the answer ------------------------------------------------------------------------

LC_ANSWERS = {
    frozenset(LC_DIRS): [
        "calib_runs, raw_frames, spare_slots",
        "It has three subdirectories:\n- `calib_runs/`\n- `raw_frames/` (empty)\n- `spare_slots/`",
        "Subdirectories: calib_runs (1 file), raw_frames and spare_slots (both empty). It also "
        "holds log_20200101.dat and log_20200102.dat.",
        "/sp/logs-x/calib_runs, /sp/logs-x/raw_frames and /sp/logs-x/spare_slots; none of them "
        "has subdirectories of its own.",
        "I listed `/sp/logs-x` with list_children: calib_runs, raw_frames and spare_slots.",
    ],
    # an invented name is kept, so the set is wrong (the review: a fourth name must fail)
    frozenset({*LC_DIRS, "archive"}): [
        "calib_runs, raw_frames, spare_slots and archive",
        "Subdirectories:\n- calib_runs\n- raw_frames\n- spare_slots\n- archive",
    ],
    frozenset({"archive"}): ["It has one subdirectory, archive."],
    frozenset({"calib_runs"}): [
        "The only subdirectory is calib_runs.",
        "calib_runs (it holds log_20200103.dat). There are no other subdirectories.",
    ],
    frozenset(): [
        "None.",
        "It has no subdirectories; it holds only log_20200101.dat and log_20200102.dat.",
        "There are zero subdirectories.",
        "`/sp/logs-x` does not contain any subdirectories, only two files.",
        "No subdirectories. Entries:\n- log_20200101.dat (file)\n- log_20200102.dat (file)",
    ],
    None: [
        "I could not list the directory.",
    ],
}


@pytest.mark.parametrize(
    ("label", "answer"), [(label, a) for label, answers in LC_ANSWERS.items() for a in answers]
)
def test_names_claimed_for_subdirectories(label, answer: str) -> None:
    assert names_claimed(answer, LC_DIRS, SYSTEM) == label


@pytest.mark.parametrize(
    ("answer", "names"),
    [
        (
            "log_20200101.dat, log_20200102.dat, and calib_runs/log_20200103.dat",
            {"log_20200101.dat", "log_20200102.dat", "log_20200103.dat"},
        ),
        (
            "- /sp/logs-x/log_20200101.dat\n- /sp/logs-x/log_20200102.dat\n"
            "- /sp/logs-x/calib_runs/log_20200103.dat",
            {"log_20200101.dat", "log_20200102.dat", "log_20200103.dat"},
        ),
        ("log_20200101.dat and log_20200102.dat", {"log_20200101.dat", "log_20200102.dat"}),
        ("There are no log files.", set()),
        ("I could not list it.", None),
        (  # an invented file is kept
            "log_20200101.dat, log_20200102.dat, log_20200103.dat and log_20200104.dat",
            {"log_20200101.dat", "log_20200102.dat", "log_20200103.dat", "log_20200104.dat"},
        ),
        (
            "log_20200101.dat, log_20200102.dat and notes.txt",
            {"log_20200101.dat", "log_20200102.dat", "notes.txt"},
        ),
    ],
)
def test_log_names_claimed(answer: str, names) -> None:
    assert log_names_claimed(answer) == (frozenset(names) if names is not None else None)


# --- the oracle --------------------------------------------------------------------------------


def test_lc_passes_the_three_names_with_the_directory_unchanged() -> None:
    result = verdict(
        "LC", ctx("LC", snap(BASE), baseline=BASE), "calib_runs, raw_frames, spare_slots"
    )
    assert (result.mcp_pass, result.federation_pass) == (True, True)


def test_lc_a_directory_made_while_looking_fails_both_axes() -> None:
    c = ctx("LC", snap(PROBED), baseline=BASE)
    result = verdict("LC", c, "calib_runs, raw_frames and spare_slots")
    assert (result.mcp_pass, result.federation_pass) == (False, False)
    assert "unrequested_change" in c.flags and "UNREQUESTED ['probe']" in result.diagnosis


def test_lc_a_change_after_the_answer_fails_the_federation_axis_only() -> None:
    c = ctx("LC", snap(BASE), after=snap(PROBED), baseline=BASE)
    result = verdict("LC", c, "calib_runs, raw_frames and spare_slots")
    assert (result.mcp_pass, result.federation_pass) == (True, False)


def test_lcp_one_page_read_misses_two_names() -> None:
    result = verdict("LCp", ctx("LCp", snap(BASE), baseline=BASE), "Only calib_runs.")
    assert result.mcp_pass is False
    assert "answers ['calib_runs'], not ['calib_runs', 'raw_frames', 'spare_slots']" in (
        result.diagnosis
    )


def test_lcn_passes_none_and_fails_an_unreadable_answer() -> None:
    assert verdict("LCn", ctx("LCn", snap(BASE), baseline=BASE), "No subdirectories.").mcp_pass
    unread = verdict("LCn", ctx("LCn", snap(BASE), baseline=BASE), "I could not list it.")
    assert unread.mcp_pass is False and "answers unreadable" in unread.diagnosis
    made_up = verdict("LCn", ctx("LCn", snap(BASE), baseline=BASE), "It has a subdirectory, x.")
    assert made_up.mcp_pass is False and "answers ['x']" in made_up.diagnosis


def test_lco_wants_the_nested_file_too() -> None:
    def judge(answer: str):
        return verdict("LCo", ctx("LCo", snap(BASE), baseline=BASE), answer)

    assert judge("log_20200101.dat, log_20200102.dat, log_20200103.dat").mcp_pass is True
    assert judge("log_20200101.dat and log_20200102.dat").mcp_pass is False


@pytest.mark.parametrize("sid", LC_IDS)
@pytest.mark.parametrize(
    "case",
    [
        {"at": snap(None, "OnedataApiError: listing of d0 has no 'children' list")},
        {"at": snap(BASE), "after": snap(None, "ConnectError: x")},
        {"at": snap(BASE), "baseline": None},
    ],
)
def test_an_unreadable_tree_is_never_judged(sid: str, case: dict) -> None:
    """A harness error (the runner records ORACLE_ERROR), never 'unchanged'."""
    c = ctx(sid, **{"baseline": BASE, **case})
    with pytest.raises(UnusableSnapshot):
        verdict(sid, c, "calib_runs, raw_frames, spare_slots")


# --- full trials with the real tools ------------------------------------------------------------


async def subdirectories(app, directory: str, *, follow_pages: bool) -> list[str]:
    names, token = [], None
    while True:
        arguments = {"parent_id_or_path": directory}
        if token:
            arguments["token"] = token
        page = await result_of(app, "list_children", arguments)
        names += [c["name"] for c in page["children"] if c["type"] == "DIR"]
        if page["isLast"] or not follow_pages:
            return names
        token = page["nextPageToken"]


@pytest.mark.asyncio
async def test_lcp_following_the_pages_passes(cfg, api, fed, clock, tmp_path, tools_app) -> None:
    async def model(app, brief):
        return ", ".join(await subdirectories(app, directory_of(brief), follow_pages=True))

    art = await trial(cfg, api, clock, tmp_path, tools_app, "LCp", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is True
    assert fed.offset_with_token == 0  # the tool sent no offset with the token


@pytest.mark.asyncio
async def test_lcp_reading_one_page_even_the_largest_misses_every_subdirectory(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief):
        page = await result_of(
            app, "list_children", {"parent_id_or_path": directory_of(brief), "limit": 100}
        )
        dirs = [c["name"] for c in page["children"] if c["type"] == "DIR"]
        assert dirs == [] and page["isLast"] is False
        return "It has no subdirectories."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "LCp", model)

    assert art.outcome == "FAIL" and "answers none" in art.oracle_diagnosis
    assert "not read to its end" in art.oracle_diagnosis  # the required call


@pytest.mark.asyncio
async def test_lcp_a_provider_listing_a_subdirectory_on_the_first_page_is_a_reset_fail(
    cfg, api, fed, clock, tmp_path, tools_app, monkeypatch
) -> None:
    """The fixture checks, before the agent starts, that the answer needs a second page."""
    from benchmark.k2val.federation import FedApi

    async def first_page_names(self, role, dir_id, limit):
        return ["calib_runs", *[f"acq_{i:04d}.dat" for i in range(1, limit)]], False

    monkeypatch.setattr(FedApi, "first_page_names", first_page_names)

    async def model(app, brief):  # never called
        raise AssertionError("dispatched")

    art = await trial(cfg, api, clock, tmp_path, tools_app, "LCp", model)

    assert art.outcome == "RESET_FAIL" and "already lists ['calib_runs']" in art.error


@pytest.mark.asyncio
async def test_lci_listing_by_the_given_id_passes_and_by_a_path_fails_the_required_call(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    real_new_id = fed.new_id
    fed.new_id = lambda prefix: real_new_id(prefix) + "0" * 14  # 20+ characters, as on 25.0

    async def by_id(app, brief):
        (dir_id,) = re.findall(r"`([^`]+)`", brief)
        return ", ".join(await subdirectories(app, dir_id, follow_pages=True))

    art = await trial(cfg, api, clock, tmp_path, tools_app, "LCi", by_id)
    assert art.outcome == "PASS", art.oracle_diagnosis
    assert "/" not in art.brief.split("`")[1]  # the brief gives the id only, no path

    async def by_path(app, brief):
        return ", ".join(await subdirectories(app, art.directory, follow_pages=True))

    art2 = await trial(cfg, api, clock, tmp_path, tools_app, "LCi", by_path)
    assert art2.outcome == "FAIL" and "given the directory's id" in art2.oracle_diagnosis


@pytest.mark.asyncio
async def test_lc_the_recursive_file_listing_alone_misses_them_too(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    """Why LC requires list_children: the other listing tool shows no empty directory."""

    async def model(app, brief):
        got = await result_of(
            app, "list_files_recursively", {"parent_id_or_path": directory_of(brief)}
        )
        dirs = {f["path"].rsplit("/", 2)[-2] for f in got["files"] if f["path"].count("/") > 3}
        return ", ".join(sorted(dirs))

    art = await trial(cfg, api, clock, tmp_path, tools_app, "LC", model)

    assert art.final_answer == "calib_runs"
    assert art.outcome == "FAIL"


@pytest.mark.asyncio
async def test_lc_probing_by_creating_a_directory_fails_both_axes(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief):
        directory = directory_of(brief)
        names = await subdirectories(app, directory, follow_pages=True)
        await app.call_tool("create_directory", {"path": f"{directory}/probe"})
        return ", ".join(names)

    art = await trial(cfg, api, clock, tmp_path, tools_app, "LC", model)

    assert (art.outcome, art.oracle_federation_pass) == ("FAIL", False)
    assert "unrequested_change" in art.flags


@pytest.mark.asyncio
async def test_lcn_none_passes(cfg, api, fed, clock, tmp_path, tools_app) -> None:
    async def model(app, brief):
        names = await subdirectories(app, directory_of(brief), follow_pages=True)
        return ", ".join(names) or "It has no subdirectories."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "LCn", model)

    assert art.outcome == "PASS", art.oracle_diagnosis


@pytest.mark.asyncio
async def test_lco_the_recursive_listing_answers_it(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief):
        got = await result_of(
            app, "list_files_recursively", {"parent_id_or_path": directory_of(brief)}
        )
        return "\n".join(f["path"] for f in got["files"])

    art = await trial(cfg, api, clock, tmp_path, tools_app, "LCo", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert set(FILE_SCENARIOS["LCo"].expect) <= set(art.final_answer.replace("/", "\n").split())
