"""The FA scenarios of get_file_attributes (design/13): their definition, the answer reader
`fa_label` against a table of real-sounding answers per label, the oracle, and full trials with
the real tools on the fake federation."""

from __future__ import annotations

import re

import pytest

from benchmark.k2val.config import K1_TOOLS
from benchmark.k2val.file_oracles import fa_label
from benchmark.k2val.file_scenarios import FA_NAMES, FILE_SCENARIOS
from benchmark.k2val.scenarios import SCENARIOS, parse_scenarios
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED
from test.unit.k2val.file_tool_support import ctx, directory_of, result_of, snap, trial, verdict
from test.unit.k2val.whole_ids import whole_id_of

FA_IDS = ("FA", "FAi", "FAn")

# --- the scenarios ---------------------------------------------------------------------------


@pytest.mark.parametrize("sid", FA_IDS)
def test_the_brief_names_no_tool_and_gives_no_advice(sid: str) -> None:
    text = SCENARIOS[sid].render_brief(directory="/sp/logs-abcdefabcdef", local="vistula-dc")
    for tool in ALL_KNOWN | FLAG_GATED:
        assert tool not in text
    assert not re.search(r"\b(?:tool|call|use|attribute|first|then)\b", text, re.I)
    for name in FA_NAMES:  # the K1 set cannot list the directory: the brief names the files
        assert name in SCENARIOS[sid].brief or f"ids[{name}]" in SCENARIOS[sid].brief


def test_fa_and_fan_share_the_brief_and_differ_in_the_modes() -> None:
    """Only the state differs, so a model that answers without reading fails one of them."""
    assert SCENARIOS["FA"].brief == SCENARIOS["FAn"].brief
    modes = {sid: [e.mode for e in SCENARIOS[sid].fixture.entries] for sid in ("FA", "FAi", "FAn")}
    assert modes == {
        "FA": ["644", "644", "600", "644"],
        "FAi": ["644", "600", "644", "644"],
        "FAn": ["644", "644", "644", "644"],
    }


def test_fa_is_offered_no_other_reader_of_permissions() -> None:
    """FA runs on the K1 set: it has no listing tool (both take `attributes`), so only
    get_file_attributes reads a file's posixPermissions."""
    for sid in FA_IDS:
        assert FILE_SCENARIOS[sid].tools is None and SCENARIOS[sid].tier == "K1"
    assert "get_file_attributes" in K1_TOOLS
    assert {"list_children", "list_files_recursively"}.isdisjoint(K1_TOOLS)
    assert set(K1_TOOLS) <= ALL_KNOWN


def test_the_scenarios_are_named_and_outside_the_default_range() -> None:
    assert parse_scenarios("FA,FAi,FAn,V1") == ["FA", "FAi", "FAn", "V1"]
    assert not set(FA_IDS) & set(parse_scenarios("V1..V7"))


# --- reading the answer ------------------------------------------------------------------------

FA_ANSWERS = {
    "log_20200103.dat": [
        "log_20200103.dat",
        "**log_20200103.dat**",
        "The file with different permissions is `log_20200103.dat` (600); all others are 644.",
        "log_20200103.dat has mode 600 while the other three share 644.",
        "log_20200101.dat: 644\nlog_20200102.dat: 644\nlog_20200103.dat: 600\n"
        "log_20200104.dat: 644\n\nSo log_20200103.dat is the one that differs.",
        "| file | permissions |\n|---|---|\n| log_20200101.dat | rw-r--r-- |\n"
        "| log_20200102.dat | rw-r--r-- |\n| log_20200103.dat | rw------- |\n"
        "| log_20200104.dat | rw-r--r-- |\n\nThe odd one out: log_20200103.dat.",
        "log_20200101.dat, log_20200102.dat and log_20200104.dat do not differ; "
        "log_20200103.dat does.",
        "log_20200103.dat. None of the others differ from each other.",
    ],
    "none": [
        "none",
        "None.",
        "None: all four files have the same permissions (644).",
        "No file differs: they all have rw-r--r--.",
        "Zero files differ.",
        "log_20200101.dat: 644\nlog_20200102.dat: 644\nlog_20200103.dat: 644\n"
        "log_20200104.dat: 644\n\nThey all have the same permissions, so none.",
        "All four of them have identical permissions (0644); none differs.",
        "None of the four files has different permissions.",
    ],
    "unclear": [
        "I could not read the permissions.",
        "log_20200102.dat or log_20200103.dat, I am not sure which.",
    ],
}


@pytest.mark.parametrize(
    ("label", "answer"), [(label, a) for label, answers in FA_ANSWERS.items() for a in answers]
)
def test_fa_label(label: str, answer: str) -> None:
    assert fa_label(answer, FA_NAMES) == label


def test_fa_label_a_bare_enumeration_is_read_by_its_last_line() -> None:
    """A known limit, left to the rubric judge: with no conclusion, the last line decides."""
    answer = "log_20200101.dat: 644\nlog_20200103.dat: 600"
    assert fa_label(answer, FA_NAMES) == "log_20200103.dat"


# --- the oracle --------------------------------------------------------------------------------


@pytest.mark.parametrize("sid", FA_IDS)
def test_the_oracle_judges_the_answer_its_federation_axis_is_none(sid: str) -> None:
    """The oracle reads the answer only (federation None); that nothing changed, the K1 set
    offering write tools, is scoring.py's restraint check, which also refuses an unreadable
    tree (test_scoring.py)."""
    expect = FILE_SCENARIOS[sid].expect
    right = expect if expect == "none" else f"It is {expect}."
    assert verdict(sid, ctx(sid, snap(None, "x")), right).mcp_pass is True
    wrong = verdict(sid, ctx(sid, snap(None, "x")), "log_20200104.dat")
    assert (wrong.mcp_pass, wrong.federation_pass) == (False, None)
    assert "answers ['log_20200104.dat']" in wrong.diagnosis


# --- full trials with the real tools ------------------------------------------------------------


async def odd_one_out(app, targets: list[str], *, as_json_text: bool = False) -> str:
    modes = {}
    for target in targets:
        names = ["name", "posixPermissions"]
        attributes = '["name", "posixPermissions"]' if as_json_text else names
        got = await result_of(
            app, "get_file_attributes", {"file_id_or_path": target, "attributes": attributes}
        )
        modes[got["name"]] = got["posixPermissions"]
    values = list(modes.values())
    odd = [name for name, mode in modes.items() if values.count(mode) == 1]
    return odd[0] if len(odd) == 1 else "none"


@pytest.mark.asyncio
async def test_fa_reading_the_modes_passes(cfg, api, fed, clock, tmp_path, tools_app) -> None:
    async def model(app, brief):
        directory = directory_of(brief)
        return await odd_one_out(app, [f"{directory}/{n}" for n in FA_NAMES], as_json_text=True)

    art = await trial(cfg, api, clock, tmp_path, tools_app, "FA", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is None
    assert set(art.fixture_baseline["tree"]) == set(FA_NAMES)
    assert art.fixture_baseline["files"] == {}
    assert all(c["ok"] for c in art.calls)  # the JSON-text list was read as a list


@pytest.mark.asyncio
async def test_fa_offers_no_listing_tool(cfg, api, fed, clock, tmp_path, tools_app) -> None:
    async def model(app, brief):
        with pytest.raises(Exception, match="not offered"):
            await app.call_tool("list_children", {"parent_id_or_path": directory_of(brief)})
        return "log_20200101.dat"

    art = await trial(cfg, api, clock, tmp_path, tools_app, "FA", model)

    assert art.outcome == "FAIL"


@pytest.mark.asyncio
async def test_fai_gives_the_built_ids_and_wants_a_name(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    real_new_id = fed.new_id
    fed.new_id = lambda prefix: whole_id_of(real_new_id(prefix))  # whole ids, as on 25.0

    async def model(app, brief):
        ids = re.findall(r"\b([0-9A-F]{60,})\b", brief)
        assert len(ids) == 4
        return await odd_one_out(app, ids)

    art = await trial(cfg, api, clock, tmp_path, tools_app, "FAi", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert "<id of" not in art.brief


@pytest.mark.asyncio
async def test_fan_all_alike_is_none(cfg, api, fed, clock, tmp_path, tools_app) -> None:
    async def model(app, brief):
        directory = directory_of(brief)
        return await odd_one_out(app, [f"{directory}/{n}" for n in FA_NAMES])

    art = await trial(cfg, api, clock, tmp_path, tools_app, "FAn", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.final_answer == "none"
