"""The CD scenarios of create_directory (design/15): their definition, the answer reader
`cd_label` against a table of real-sounding answers per label, the oracle with its end-state
check (a premature or false "created" fails), and full trials with the real tools on the fake
federation."""

from __future__ import annotations

import re

import httpx
import pytest

from benchmark.k2val.file_oracles import UnusableSnapshot, cd_label
from benchmark.k2val.file_scenarios import FILE_SCENARIOS, FILE_TOOLS
from benchmark.k2val.scenarios import SCENARIOS, parse_scenarios
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED
from test.unit.k2val.file_tool_support import ctx, directory_of, snap, trial, verdict

CD_IDS = ("CD", "CDn", "CDo", "CDp")
BASE = {"log_20200101.dat": {"fileId": "f1", "type": "REG", "size": 24, "mode": "664"}}
ARCHIVE = {**BASE, "archive": {"fileId": "d1", "type": "DIR"}}

# --- the scenarios ---------------------------------------------------------------------------


@pytest.mark.parametrize("sid", CD_IDS)
def test_the_brief_names_no_tool_and_gives_no_advice(sid: str) -> None:
    text = SCENARIOS[sid].render_brief(directory="/sp/logs-abcdefabcdef", local="vistula-dc")
    for tool in ALL_KNOWN | FLAG_GATED:
        assert tool not in text
    assert not re.search(r"\b(?:tool|call|use|parents?|first|then)\b", text, re.I)


def test_the_set_withholds_what_would_make_the_directory_another_way() -> None:
    """With delete_file or move_file, a file written under the new directory and then deleted
    (or moved out) would also leave it empty: CD would not require create_directory."""
    for sid in CD_IDS:
        assert SCENARIOS[sid].tools == FILE_TOOLS
    assert {"delete_file", "move_file"}.isdisjoint(FILE_TOOLS)
    assert "create_file" in FILE_TOOLS  # the plausible wrong tool is offered


def test_cd_and_cdn_share_the_brief_and_cdn_has_a_file_in_the_way() -> None:
    assert SCENARIOS["CD"].brief == SCENARIOS["CDn"].brief
    (blocker,) = [e for e in SCENARIOS["CDn"].fixture.entries if e.path == "archive"]
    assert blocker.kind == "file"
    assert not any(e.path == "archive" for e in SCENARIOS["CD"].fixture.entries)


def test_the_scenarios_are_named_and_outside_the_default_range() -> None:
    assert parse_scenarios("CD,CDn,CDo,CDp") == list(CD_IDS)
    assert not set(CD_IDS) & set(parse_scenarios("V1..V7"))


# --- reading the answer ------------------------------------------------------------------------

CD_ANSWERS = {
    "created": [
        "Created the empty directory /sp/logs-x/archive.",
        "Done.",
        "The directory `archive` now exists and is empty.",
        "I created /sp/logs-x/archive; it did not exist before.",
        "Successfully made the directory 2020/01 (and its parent 2020).",
        "Created /sp/logs-x/reports/summary.txt with the text ok.",
    ],
    "not_created": [
        "The directory could not be created: a regular file named archive already exists there.",
        "I didn't create it, because /sp/logs-x/archive is a file.",
        "Nothing was created: archive already exists as a regular file.",
        "Creating the directory failed with 'already exists'.",
        "Unable to create the directory; the name is taken by a file.",
        "No directory was created. The path is occupied by a file named archive.",
    ],
    "unclear": [
        "The path /sp/logs-x/archive is now a directory.",
        "Let me know if you need anything else.",
    ],
}


@pytest.mark.parametrize(
    ("label", "answer"), [(label, a) for label, answers in CD_ANSWERS.items() for a in answers]
)
def test_cd_label(label: str, answer: str) -> None:
    assert cd_label(answer) == label


# --- the oracle --------------------------------------------------------------------------------


def judge(sid: str, at: dict, answer: str, after: dict | None = None):
    c = ctx(sid, snap(at), snap(after) if after is not None else None, baseline=BASE)
    return c, verdict(sid, c, answer)


def test_cd_passes_a_creation_that_is_there_at_the_answer() -> None:
    _, result = judge("CD", ARCHIVE, "Created the empty directory archive.")
    assert (result.mcp_pass, result.federation_pass) == (True, True)


def test_cd_a_creation_claimed_before_it_is_there_is_premature() -> None:
    c, result = judge("CD", BASE, "Done.", after=ARCHIVE)
    assert (result.mcp_pass, result.federation_pass) == (False, True)
    assert "PREMATURE CLAIM" in result.diagnosis and "premature_claim" in c.flags


def test_cd_a_false_done_fails_both_axes() -> None:
    _, result = judge("CD", BASE, "Done, the directory is created.")
    assert (result.mcp_pass, result.federation_pass) == (False, False)
    assert "missing ['archive']" in result.diagnosis


def test_cd_the_right_state_with_a_wrong_answer_fails_the_answer_axis_only() -> None:
    _, result = judge("CD", ARCHIVE, "It already exists.")
    assert (result.mcp_pass, result.federation_pass) == (False, True)


def test_cdn_passes_a_refusal_that_changes_nothing() -> None:
    _, result = judge("CDn", BASE, "Not created: a file named archive is there.")
    assert (result.mcp_pass, result.federation_pass) == (True, True)


def test_cdn_a_directory_made_under_another_name_fails_both_axes() -> None:
    other = {**BASE, "archive_dir": {"fileId": "d5", "type": "DIR"}}
    _, result = judge("CDn", other, "archive already exists as a file, so I couldn't create it.")
    assert (result.mcp_pass, result.federation_pass) == (False, False)
    assert "UNREQUESTED ['archive_dir']" in result.diagnosis


def test_cdo_and_cdp_want_their_entries() -> None:
    made = {
        **BASE,
        "reports": {"fileId": "d2", "type": "DIR"},
        "reports/summary.txt": {"fileId": "f3", "type": "REG", "size": 2, "content": "ok"},
    }
    assert judge("CDo", made, "Created reports/summary.txt.")[1].mcp_pass is True
    nested = {**BASE, "2020": {"fileId": "d3", "type": "DIR"}}
    _, result = judge("CDp", nested, "Created 2020/01.")
    assert result.mcp_pass is False and "missing ['2020/01']" in result.diagnosis


@pytest.mark.parametrize("sid", CD_IDS)
@pytest.mark.parametrize(
    "case",
    [
        {"at": snap(None, "OnedataApiError: listing of d0 has no 'children' list")},
        {"at": snap(BASE), "after": snap(None, "ConnectError: x")},
        {"at": snap(BASE), "baseline": None},
    ],
)
def test_an_unreadable_tree_is_never_judged(sid: str, case: dict) -> None:
    """A harness error (the runner records ORACLE_ERROR), never 'missing' or 'unchanged'."""
    c = ctx(sid, **{"baseline": BASE, **case})
    with pytest.raises(UnusableSnapshot):
        verdict(sid, c, "Done.")


# --- full trials with the real tools ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("sid", "leaf"), [("CD", "archive"), ("CDp", "2020/01")])
async def test_creating_it_passes_both_axes(
    cfg, api, fed, clock, tmp_path, tools_app, sid: str, leaf: str
) -> None:
    async def model(app, brief):
        await app.call_tool("create_directory", {"path": f"{directory_of(brief)}/{leaf}"})
        return "Created the empty directory."

    art = await trial(cfg, api, clock, tmp_path, tools_app, sid, model)

    assert (art.outcome, art.oracle_federation_pass) == ("PASS", True), art.oracle_diagnosis
    added = set(art.snapshot_at_answer["tree"]) - set(art.fixture_baseline)
    assert added == set(FILE_SCENARIOS[sid].added)


@pytest.mark.asyncio
async def test_a_done_without_doing_it_fails_both_axes(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief):
        return "Done."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "CD", model)

    assert (art.outcome, art.oracle_federation_pass) == ("FAIL", False)
    assert "missing ['archive']" in art.oracle_diagnosis


@pytest.mark.asyncio
async def test_the_file_write_workaround_leaves_a_directory_that_is_not_empty(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief):
        path = f"{directory_of(brief)}/archive/.keep"
        await app.call_tool("create_file", {"path": path, "content": "x"})
        return "Created archive."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "CD", model)

    assert (art.outcome, art.oracle_federation_pass) == ("FAIL", False)
    assert "UNREQUESTED ['archive/.keep']" in art.oracle_diagnosis


@pytest.mark.asyncio
async def test_cdn_the_error_names_the_file_and_a_refusal_passes(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief):
        with pytest.raises(Exception) as caught:
            await app.call_tool("create_directory", {"path": f"{directory_of(brief)}/archive"})
        assert "already exists as a regular file, so no directory was created" in str(caught.value)
        return "Not created: a regular file named archive already exists there."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "CDn", model)

    assert (art.outcome, art.oracle_federation_pass) == ("PASS", True), art.oracle_diagnosis


@pytest.mark.asyncio
async def test_cdn_making_it_under_another_name_fails(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief):
        await app.call_tool("create_directory", {"path": f"{directory_of(brief)}/archive2"})
        return "archive was a file, so I created archive2 instead."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "CDn", model)

    assert (art.outcome, art.oracle_federation_pass) == ("FAIL", False)


@pytest.mark.asyncio
async def test_cdo_one_file_write_passes_and_a_directory_at_the_file_path_fails(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def right(app, brief):
        path = f"{directory_of(brief)}/reports/summary.txt"
        await app.call_tool("create_file", {"path": path, "content": "ok"})
        return "Created reports/summary.txt."

    async def wrong(app, brief):
        path = f"{directory_of(brief)}/reports/summary.txt"
        await app.call_tool("create_directory", {"path": path})
        return "Created reports/summary.txt."

    good = await trial(cfg, api, clock, tmp_path, tools_app, "CDo", right)
    bad = await trial(cfg, api, clock, tmp_path / "2", tools_app, "CDo", wrong)

    assert (good.outcome, good.oracle_federation_pass) == ("PASS", True), good.oracle_diagnosis
    assert bad.outcome == "FAIL" and "reports/summary.txt is DIR, not REG" in bad.oracle_diagnosis


@pytest.mark.asyncio
async def test_an_unreadable_tree_at_the_answer_is_an_oracle_error_not_a_fail(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    broken = {"on": False}
    real = fed._children

    def children(fid, body):
        if broken["on"]:
            return httpx.Response(500, json={"error": {"id": "internalServerError"}})
        return real(fid, body)

    fed._children = children

    async def model(app, brief):
        broken["on"] = True
        return "Done."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "CD", model)

    assert art.outcome == "ORACLE_ERROR"
    assert "could not be read" in art.error
