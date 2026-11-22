"""The stable tools' smoke scenarios (ST1..ST7, ST4i; design/16): their definitions and the
owed-change map, the fixtures on the fake federation, each answer reader against a table of
real-sounding answers (right, wrong and unclear), whole trials through the runner with the real
tools, the observations (the id-guard hint) and the report's section."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from benchmark.k2val.artefact import read_trials
from benchmark.k2val.config import K1_TOOLS, K2_TOOLS
from benchmark.k2val.federation import FedApi, FixtureError
from benchmark.k2val.fixtures import FILE_SIZE, FixtureBuilder, split_parts
from benchmark.k2val.judge import JudgeVerdict
from benchmark.k2val.oracles import ORACLES
from benchmark.k2val.reading import READERS, is_right, read_answer, right_reading
from benchmark.k2val.rubrics import ANSWER_SPECS, OTHER
from benchmark.k2val.scenarios import SCENARIOS, TOOL_UNDER_TEST, parse_scenarios
from benchmark.k2val.scoring import REQUIRED, rescore_stored
from benchmark.k2val.smoke_report import smoke_section
from benchmark.k2val.stable_oracles import (
    QOS_ID_HINT,
    hint_exposures,
    keeps_local,
    read_copies,
    read_rule_task,
    read_rules_answer,
    read_shares,
    read_single_copy,
    read_sites,
    read_transfer_ids,
    read_which_file,
    smoke_evidence,
)
from benchmark.k2val.stable_scenarios import (
    SMOKE,
    SMOKE_K,
    SMOKE_MODELS,
    SMOKE_SCENARIOS,
    ST1_FILE,
    ST3_ONE_COPY,
    ST4_FILE,
    ST4_LISTED_WITHIN,
    ST4_OTHER,
    ST5_FILE,
    ST6_FILE,
    ST7_TOOLS,
    ST_SCENARIOS,
)
from benchmark.k2val.surfaces import RECORDED_SURFACES
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED, STABLE
from test.unit.k2val.fakefed import LOCAL_ID, OWNER_ID, FakeClock, FakeFed
from test.unit.k2val.file_tool_support import result_of, trial

ST_IDS = ("ST1", "ST2", "ST3", "ST4", "ST4i", "ST5", "ST6", "ST7")
DIR = "/sp/logs-abcdefabcdef"
ST3_NAMES = ("log_20200113.dat", "log_20200114.dat", "log_20200115.dat")
SPACE = "k2val-glm"  # the space of the config the trials run (cfg.models["glm"])
WRITES = ("add_file_qos_requirement", "remove_qos_requirement", "schedule_file_replication")


def brief(sid: str, **kw) -> str:
    return SCENARIOS[sid].render_brief(directory=DIR, local="vistula-dc", **kw)


# --- the scenarios and the map of owed re-runs -----------------------------------------------


@pytest.mark.parametrize("sid", ST_IDS)
def test_the_brief_names_no_tool_and_gives_no_advice(sid: str) -> None:
    text = brief(sid)
    for tool in ALL_KNOWN | FLAG_GATED:
        assert tool not in text
    assert not re.search(
        r"\b(?:tool|call|use|first|then|poll|wait|QoS|requirement|distribution|block|replica\w*)\b",
        text,
        re.I,
    )


def test_the_briefs_word_for_word() -> None:
    assert brief("ST1") == (
        f"Set up a placement rule that keeps `{DIR}/log_20200111.dat` at your site vistula-dc, "
        "and tell me when its data are fully there."
    )
    assert brief("ST2", qos={"log_20200112.dat": "q" * 24}) == (
        "How many copies of its file does the placement rule with the id "
        f"`{'q' * 24}` ask for? Reply with the number."
    )
    assert brief("ST3") == (
        f"Which of these files in `{DIR}` have a placement rule that asks for only one copy: "
        "log_20200113.dat, log_20200114.dat, log_20200115.dat? List their names."
    )
    assert brief("ST4") == (
        f"What is the id of the transfer that copied `{DIR}/log_20200116.dat` to your site "
        "vistula-dc? Reply with the id only."
    )
    assert brief("ST4i", ids={"transfer:log_20200116.dat": "t" * 24}) == (
        f"Which of these files in `{DIR}` did the transfer with the id `{'t' * 24}` copy: "
        "log_20200116.dat, log_20200117.dat? Reply with its name."
    )
    assert brief("ST5") == (
        f"Which placement rules apply to `{DIR}/log_20200119.dat`? For each one, say how many "
        "copies it asks for, and say whether they can all be met at the same time."
    )
    assert brief("ST6") == (
        f"What share of the data of `{DIR}/log_20200118.dat` does each site hold? Give a "
        "percentage for each site."
    )
    assert brief("ST7") == f"Which sites support the space that holds `{DIR}`? List their names."


def test_an_id_brief_gives_no_path_or_file() -> None:
    """ST2 gives the rule by its id alone: no file or directory, so no other tool reads it."""
    text = brief("ST2", qos={"log_20200112.dat": "q" * 24})
    assert DIR not in text and "log_" not in text


@pytest.mark.parametrize("sid", ST_IDS)
def test_registered_in_every_registry_after_the_earlier_scenarios(sid: str) -> None:
    spec = ST_SCENARIOS[sid]
    scenario = SCENARIOS[sid]
    assert sid in ORACLES and sid in READERS and sid in ANSWER_SPECS and sid in REQUIRED
    assert TOOL_UNDER_TEST[sid] == spec.tool
    assert scenario.tier == "K1" and scenario.tools == spec.tools
    assert (scenario.restraint, scenario.settle, scenario.settle_s) == (
        spec.restraint,
        spec.settle,
        spec.settle_s,
    )
    offered = frozenset(scenario.tools or K1_TOOLS)
    assert offered in RECORDED_SURFACES and spec.tool in offered
    assert list(SCENARIOS)[-len(ST_IDS) :] == list(ST_IDS)


def test_st1_writes_waits_out_its_window_and_the_read_only_ones_are_restraint() -> None:
    """ST1 is read again only after its whole window (its rules re-evaluated then); every
    read-only one offered a write tool is a restraint scenario; ST7 is offered none."""
    st1 = SCENARIOS["ST1"]
    assert (st1.restraint, st1.settle, st1.settle_s) == (False, "quiet", 60.0)
    assert st1.fixture.rules_in_snapshots  # its snapshots read and evaluate the rules
    for sid in ("ST2", "ST3", "ST4", "ST4i", "ST5", "ST6"):
        assert SCENARIOS[sid].restraint and SCENARIOS[sid].settle == "quiet", sid
        assert SCENARIOS[sid].fixture.files  # a baseline to compare with (restraint needs one)
    st7 = SCENARIOS["ST7"]
    assert not st7.restraint and st7.settle == "none" and not set(WRITES) & set(ST7_TOOLS)
    for sid in ("ST2", "ST3", "ST5"):  # rules read back before dispatch; snapshots do not
        assert SCENARIOS[sid].fixture.rules_detail, sid
        assert not SCENARIOS[sid].fixture.rules_in_snapshots, sid


def test_st7_is_offered_no_other_tool_that_names_a_provider() -> None:
    assert set(ST7_TOOLS) <= set(K1_TOOLS)
    assert not {"get_file_distribution", "schedule_file_replication"} & set(ST7_TOOLS)
    assert set(SCENARIOS["ST7"].excluded_tools) == {
        "get_file_distribution",
        "schedule_file_replication",
    }
    assert RECORDED_SURFACES[frozenset(ST7_TOOLS)][0] == "ST7_TOOLS"


def test_the_map_covers_the_eight_owed_changes_with_direct_scenarios() -> None:
    """design/10, "Transition": six reworded descriptions, get_qos_requirement's new error
    hints, and get_file_qos_summary's replicas_num. Each has a DIRECT scenario (the task
    requires the tool); the panel's own scenarios, where the tool is only natural or the oracle
    asks for it, are SUPPORTING."""
    assert [(e.tool, e.change, e.direct, e.supporting) for e in SMOKE] == [
        ("add_file_qos_requirement", "description: its `expression` field", ("ST1",), ()),
        ("get_file_distribution", "description", ("ST6",), ("V1", "SR1", "SR1i")),
        ("get_file_qos_summary", "description", ("ST3", "ST5"), ("V3", "V3n", "V3o")),
        ("get_file_qos_summary", "result: returns replicas_num", ("ST3", "ST5"), ()),
        ("get_qos_requirement", "error hints: the qos_id guard", ("ST2",), ("ST3", "ST5")),
        ("get_transfer", "description", ("ST4", "ST4i"), ()),
        ("list_space_providers", "description", ("ST7",), ("SR1", "SR1i", "SR1n", "ST1")),
        ("list_space_transfers", "description", ("ST4",), ()),
    ]
    for entry in SMOKE:
        assert entry.tool in STABLE and entry.tool in K1_TOOLS, entry.tool
        assert entry.ppam
        for sid in entry.direct:
            assert sid in ST_SCENARIOS and sid in REQUIRED, sid
        for sid in entry.scenarios:
            offered = SCENARIOS[sid].tools or (
                K2_TOOLS if SCENARIOS[sid].tier == "K2" else K1_TOOLS
            )
            assert entry.tool in offered, (entry.tool, sid)
            assert frozenset(offered) in RECORDED_SURFACES, sid  # a recorded surface hash
    for sid, spec in ST_SCENARIOS.items():
        assert any(sid in e.direct and e.tool == spec.tool for e in SMOKE), sid


def test_the_smoke_set_runs_by_name_and_the_makefile_holds_it() -> None:
    assert len(set(SMOKE_SCENARIOS)) == len(SMOKE_SCENARIOS)
    assert set(SMOKE_SCENARIOS) == {sid for e in SMOKE for sid in e.scenarios}
    assert parse_scenarios(",".join(SMOKE_SCENARIOS)) == list(SMOKE_SCENARIOS)
    assert (SMOKE_MODELS, SMOKE_K) == (("qwencoder", "haiku-low"), 3)
    makefile = Path("Makefile").read_text()
    assert f"K2VAL_SMOKE_SCENARIOS ?= {','.join(SMOKE_SCENARIOS)}\n" in makefile
    assert "\nk2val-smoke:\n" in makefile
    assert f"K2VAL_MODELS={','.join(SMOKE_MODELS)}" in makefile
    assert f"K2VAL_K={SMOKE_K}" in makefile


def test_st4_files_and_the_transfer_checks() -> None:
    """The asked file is copied first, the other one after it: the newest transfer of the
    space is the wrong one. Both scenarios check the transfers' details; only ST4 needs the
    listing's order (ST4i gives the id)."""
    files = SCENARIOS["ST4"].fixture.files
    assert [(f.name, f.kind) for f in files] == [(ST4_FILE, "s0_local"), (ST4_OTHER, "s0_local")]
    assert SCENARIOS["ST4"].fixture.transfers_listed_within == ST4_LISTED_WITHIN
    assert SCENARIOS["ST4i"].fixture.files == files
    assert SCENARIOS["ST4i"].fixture.transfers_listed_within is None
    assert (
        SCENARIOS["ST4"].fixture.transfers_checked and SCENARIOS["ST4i"].fixture.transfers_checked
    )


# --- the fixtures on the fake federation ------------------------------------------------------


def builder(api: FedApi, clock: FakeClock) -> FixtureBuilder:
    return FixtureBuilder(api, SPACE, clock)


@pytest.mark.asyncio
async def test_st3_builds_and_reads_back_a_rule_per_file(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(SCENARIOS["ST3"].fixture, "logs-st3")

    rules = {name: fed.rules[rid] for name, rid in built.qos_rules.items()}
    assert {n: (r["expression"], r["replicasNum"], r["status"]) for n, r in rules.items()} == {
        "log_20200113.dat": (f"providerId={LOCAL_ID}", 1, "fulfilled"),
        "log_20200114.dat": (f"providerId={OWNER_ID}|providerId={LOCAL_ID}", 2, "fulfilled"),
        "log_20200115.dat": (f"providerId={OWNER_ID}", 1, "fulfilled"),
    }
    base = built.baseline()["files"]
    assert {n: (b["kind"], b["local_bytes"], b["qos_ids"]) for n, b in base.items()} == {
        "log_20200113.dat": ("s0_qos", FILE_SIZE, [built.qos_rules["log_20200113.dat"]]),
        "log_20200114.dat": ("s0_qos_both", FILE_SIZE, [built.qos_rules["log_20200114.dat"]]),
        "log_20200115.dat": ("s0_qos_owner", 0, [built.qos_rules["log_20200115.dat"]]),
    }
    # each rule read back before the agent starts: attachment, count, evaluated providers
    read = base["log_20200114.dat"]["rules"][built.qos_rules["log_20200114.dat"]]
    assert read["fileId"] == built.files["log_20200114.dat"].file_id and read["replicasNum"] == 2
    assert {s["providerId"] for s in read["matching"]} == {OWNER_ID, LOCAL_ID}
    assert built.space_id == SPACE and not built.rules_in_snapshots


@pytest.mark.asyncio
async def test_a_rule_that_reads_back_otherwise_never_reaches_a_model(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST2's two-copy rule stored as one (say): a FixtureError before dispatch, so a model that
    reads the rule and reports one is never failed for it."""
    real = fed._qos_one

    def stored_as_one(method, qid):
        response = real(method, qid)
        if method == "GET" and response.status_code == 200:
            return type(response)(200, json={**response.json(), "replicasNum": 1})
        return response

    monkeypatch.setattr(fed, "_qos_one", stored_as_one)

    with pytest.raises(FixtureError, match="replicasNum 1, not 2"):
        await builder(api, clock).build(SCENARIOS["ST2"].fixture, "logs-st2")


@pytest.mark.asyncio
async def test_st5_builds_two_rules_on_one_file(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    builder_ = builder(api, clock)
    built = await builder_.build(SCENARIOS["ST5"].fixture, "logs-st5")

    first = built.qos_rules[ST5_FILE]
    (second,) = built.extra_rules[ST5_FILE]
    assert (fed.rules[first]["replicasNum"], fed.rules[second]["replicasNum"]) == (1, 2)
    base = built.baseline()["files"][ST5_FILE]
    assert base["qos_ids"] == sorted([first, second]) and set(base["rules"]) == {first, second}
    assert base["local_covered"]
    assert await builder_.teardown(built) == [] and fed.rules == {}


@pytest.mark.asyncio
async def test_st6_writes_half_at_the_owner_and_the_last_quarter_at_the_site(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    """The shares are 50 and 25: they do not add up to the whole (the third quarter is held by
    none), so the owner's cannot be had from the site's own rate."""
    built = await builder(api, clock).build(SCENARIOS["ST6"].fixture, "logs-st6")

    bf = built.files[ST6_FILE]
    node = fed.nodes[bf.file_id]
    first, second = split_parts(FILE_SIZE)
    assert node.size == FILE_SIZE and (first, second) == (FILE_SIZE // 2, FILE_SIZE * 3 // 4)
    assert fed.covered(node.blocks[OWNER_ID], FILE_SIZE) == first
    assert fed.covered(node.blocks[LOCAL_ID], FILE_SIZE) == FILE_SIZE - second
    puts = {(p["provider"], p["offset"]) for p in fed.puts if p["file"] == bf.file_id}
    assert puts == {(OWNER_ID, 0), (LOCAL_ID, second)}  # explicit offsets only
    base = built.baseline()["files"][ST6_FILE]
    assert (base["local_bytes"], base["local_covered"]) == (FILE_SIZE - second, False)


@pytest.mark.asyncio
async def test_st4_records_its_transfers_in_order_with_their_details(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(SCENARIOS["ST4"].fixture, "logs-st4")

    assert list(built.transfers) == [ST4_FILE, ST4_OTHER]
    first, second = built.transfers.values()
    assert list(fed.transfers) == [first, second]  # the asked file's transfer is the older one
    for name, tid in built.transfers.items():
        base = built.baseline()["files"][name]
        assert base["transfer_id"] == tid and built.ids()[f"transfer:{name}"] == tid
        assert base["transfer"]["fileId"] == built.files[name].file_id
        assert base["transfer"]["replicatingProviderId"] == LOCAL_ID
    # a kind without a transfer keeps the baseline it had
    plain = await builder(api, clock).build(SCENARIOS["V2"].fixture, "logs-v2")
    assert not {"transfer_id", "transfer", "rules"} & set(
        plain.baseline()["files"]["log_20200105.dat"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [0, ST4_LISTED_WITHIN])
async def test_st4_never_reaches_a_model_unless_the_listing_is_newest_first(
    api: FedApi, fed: FakeFed, clock: FakeClock, history: int
) -> None:
    """Oldest first, the asked file's transfer would be listed first, the right answer by
    position: a FixtureError (RESET_FAIL) whether the space is fresh or has a history."""
    fed.ended_order = "oldest"
    for i in range(history):  # an earlier run's transfers, files long gone
        fed.transfers[f"told{i:04d}"] = {"fileId": "gone", "spaceId": SPACE, "status": "completed"}

    with pytest.raises(FixtureError, match="newest first"):
        await builder(api, clock).build(SCENARIOS["ST4"].fixture, "logs-st4")

    fed.ended_order = "newest"  # the same history, listed newest first: it builds
    built = await builder(api, clock).build(SCENARIOS["ST4"].fixture, "logs-st4b")
    assert len(built.transfers) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["ST4", "ST4i"])
async def test_a_transfer_whose_details_do_not_name_its_file_never_reaches_a_model(
    api: FedApi, fed: FakeFed, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, sid: str
) -> None:
    real = fed._transfer_get

    def nameless(tid):
        response = real(tid)
        body = {k: v for k, v in response.json().items() if k not in ("fileId", "filePath")}
        return type(response)(200, json=body)

    monkeypatch.setattr(fed, "_transfer_get", nameless)

    with pytest.raises(FixtureError, match="name the file and the local provider"):
        await builder(api, clock).build(SCENARIOS[sid].fixture, "logs-tx")


# --- the meaning of a rule (ST1) ----------------------------------------------------------------


def _rule(providers: tuple[str, ...], replicas: int) -> dict:
    return {
        "replicasNum": replicas,
        "matching": [{"id": f"st-{p}", "providerId": p} for p in providers],
    }


@pytest.mark.parametrize(
    ("providers", "replicas", "keeps"),
    [
        ((LOCAL_ID,), 1, True),  # providerId=<site>
        ((OWNER_ID, LOCAL_ID), 2, True),  # both providers, two copies (or anyStorage x2)
        ((OWNER_ID, LOCAL_ID), 1, False),  # either provider, one copy: the owner's meets it
        ((OWNER_ID,), 1, False),  # the owner only
        ((), 1, False),  # matches nothing: never met (a site given by name)
        ((LOCAL_ID,), 2, False),  # two copies on one storage: never met
    ],
)
def test_a_rule_keeps_a_copy_at_the_site_only_when_every_way_of_meeting_it_does(
    providers, replicas, keeps
) -> None:
    assert keeps_local(_rule(providers, replicas), LOCAL_ID) is keeps


# --- reading the answers ------------------------------------------------------------------------

ST1_ANSWERS = {
    "SAYS_DONE": [
        "Done. A placement rule (providerId=pLocal) now keeps the file at vistula-dc, and its "
        "data are fully there.",
        "I added the QoS requirement `providerId=pLocal`. Its status is fulfilled and the file "
        "is fully stored at vistula-dc.",
        "The rule is in place and fulfilled; vistula-dc now holds all of the file's data.",
        "The file is now 100% replicated at vistula-dc under the new placement rule.",
        "Placement rule created (id q123). The data are completely stored at vistula-dc.",
    ],
    "SAYS_NOT_DONE": [
        "I set up the rule; replication is still in progress.",
        "The placement rule was added, but its status is pending: the data are not yet fully at "
        "vistula-dc.",
        "I could not set up the rule: the expression was rejected.",
        "The rule is impossible to fulfil on this federation.",
        "The requirement was created and will be fulfilled shortly.",
    ],
    OTHER: [
        "I added a placement rule for the file.",
        "The data are fully at vistula-dc, but the rule is still pending.",
        "The transfer status reads completed.",
        "nordic-marine-dc holds all of the file's data.",
    ],
}


@pytest.mark.parametrize(("label", "answer"), [(k, a) for k, v in ST1_ANSWERS.items() for a in v])
def test_st1_reader(label: str, answer: str) -> None:
    assert read_rule_task(answer) == label
    assert read_answer("ST1", answer) == label


ST2_ANSWERS = {
    "TWO": [
        "2",
        "Two.",
        "It asks for 2 copies.",
        "The rule requires two replicas (one at each site).",
        "replicas_num: 2",
        "The requirement `providerId=a|providerId=b` has replicasNum = 2, and it is fulfilled.",
        "2 copies (replicas_num is 2).",
        "It requires 2 replicas; currently one copy is at vistula-dc.",
    ],
    "ONE": ["1", "It asks for one copy.", "replicas_num: 1", "A single replica."],
    "OTHER_NUMBER": ["Three copies.", "replicas_num: 3"],
    OTHER: [
        "I could not read that rule.",
        "The rule keeps the file at both sites.",
        "replicas_num: 1 at first, but replicasNum: 2 now",
    ],
}


@pytest.mark.parametrize(("label", "answer"), [(k, a) for k, v in ST2_ANSWERS.items() for a in v])
def test_st2_reader(label: str, answer: str) -> None:
    assert read_copies(answer) == label


A, B, C = ST3_NAMES
ST3_ANSWERS = {
    frozenset(ST3_ONE_COPY): [
        f"{A} and {C}",
        f"- {A}: 1 replica (providerId=pLocal)\n- {B}: 2 replicas\n- {C}: 1 replica "
        f"(providerId=pOwner)\n\nFiles with only one copy: {A} and {C}.",
        f"{B} asks for two copies, while {A} and {C} ask for one.",
        f"| File | Expression | Replicas |\n|---|---|---|\n| {A} | providerId=pLocal | 1 |\n"
        f"| {B} | providerId=pOwner\\|providerId=pLocal | 2 |\n| {C} | providerId=pOwner | 1 |",
        f"Only one copy is required for {A} and {C}; {B} requires 2 replicas.",
        f"Files whose rule asks for a single replica:\n- {A}\n- {C}",
        f"{A} (replicas_num: 1), {C} (replicas_num: 1)",
    ],
    frozenset({A, B, C}): [f"{C}, {B} and {A} all ask for one copy."],
    frozenset({B}): [f"Only {B} asks for a single copy."],
    frozenset(): ["None of them: every rule asks for two copies."],
    None: ["I could not read the rules."],
}


@pytest.mark.parametrize(("names", "answer"), [(k, a) for k, v in ST3_ANSWERS.items() for a in v])
def test_st3_reader(names, answer: str) -> None:
    assert read_single_copy(answer, ST3_NAMES) == names


TID = "0d6b3f7e9a2c41b8c5e0ch7d1a"
OTHER_TID = "a7e5c2b9d4f0613e8b2dch0f4c"
PROV = "9f1e2d3c4b5a69788776ch1234"
FILEID = "00000000002C8E7A67756964236631ch3e2a"
ST4_ANSWERS = {
    frozenset({TID}): [
        TID,
        f"`{TID}`",
        f"The transfer that copied the file is {TID}.",
        f"Transfer id: {TID} (it replicated the file to provider {PROV}).",
        f"{TID} — target provider {PROV} (vistula-dc)",
        f"File id: {FILEID}. Transfer: {TID}.",
        f"I found two transfers: {OTHER_TID} copied {ST4_OTHER} and {TID} copied {ST4_FILE}. "
        f"The answer is {TID}.",
    ],
    frozenset({TID, OTHER_TID}): [f"Either {OTHER_TID} or {TID}."],
    frozenset(): ["No transfer of that file was found."],
    None: ["I could not determine it."],
}


@pytest.mark.parametrize(("ids", "answer"), [(k, a) for k, v in ST4_ANSWERS.items() for a in v])
def test_st4_reader(ids, answer: str) -> None:
    assert read_transfer_ids(answer) == ids


ST4I_ANSWERS = {
    frozenset({ST4_FILE}): [
        ST4_FILE,
        f"It copied {ST4_FILE}.",
        f"{ST4_FILE} (not {ST4_OTHER}).",
        f"The transfer copied {ST4_FILE}; {ST4_OTHER} was copied by a later transfer.",
        f"Between {ST4_FILE} and {ST4_OTHER}, it was {ST4_FILE}.",
    ],
    frozenset({ST4_OTHER}): [ST4_OTHER, f"It copied {ST4_OTHER}, not {ST4_FILE}."],
    frozenset(): ["Neither of these files."],
    None: ["Both files are at vistula-dc."],
}


@pytest.mark.parametrize(("names", "answer"), [(k, a) for k, v in ST4I_ANSWERS.items() for a in v])
def test_st4i_reader(names, answer: str) -> None:
    assert read_which_file(answer, (ST4_FILE, ST4_OTHER)) == names


ST3_MORE = {
    # the review's case: an explicit list, then an explanation of the one left out
    frozenset(ST3_ONE_COPY): [
        f"`{A}` and `{C}`. `{B}` asks for two copies.",
        f"Answer: {A}, {C}\n\n{B}'s rule asks for 2 replicas.",
    ],
    # a heading that says two copies: its items are excluded
    frozenset({C}): [f"Two copies:\n- {A}\n- {B}\n\nOne copy: {C} asks for a single replica."],
}


@pytest.mark.parametrize("names,answer", [(k, a) for k, v in ST3_MORE.items() for a in v])
def test_st3_reader_keeps_a_bare_list_apart_from_its_explanation(names, answer: str) -> None:
    assert read_single_copy(answer, ST3_NAMES) == names


@pytest.mark.parametrize(
    ("answer", "ids"),
    [
        ("t000001", {"t000001"}),  # a bare id of any alphabet or length
        ("`t000001`.", {"t000001"}),
        ("Transfer: t000001", {"t000001"}),
        ("The transfer id is t-17.", {"t-17"}),
        ("None.", set()),
    ],
)
def test_st4_reader_accepts_a_short_id_given_as_the_transfers(answer: str, ids) -> None:
    assert read_transfer_ids(answer) == frozenset(ids)


ST5_ANSWERS = {
    ("CAN_ALL_BE_MET", frozenset({"1", "2"})): [
        "Two rules apply: one asks for 1 copy, the other for 2 copies. They do not conflict: "
        "both can be met at once.",
        "- providerId=pLocal: replicas_num 1, fulfilled\n- providerId=pOwner|providerId=pLocal: "
        "replicas_num 2, fulfilled\n\nThey are compatible; both are met.",
        "Rule A requires one copy and the other requires two. No conflict.",
    ],
    ("CAN_ALL_BE_MET", frozenset({"1"})): [
        "One rule applies, asking for 1 copy. It can be met.",
    ],
    ("CONFLICT", frozenset({"1", "2"})): [
        "There are two rules (1 copy and 2 copies), and they conflict: they are mutually "
        "exclusive.",
    ],
    (OTHER, frozenset({"1", "2"})): ["Two rules: 1 copy and 2 copies."],
}


@pytest.mark.parametrize(("read", "answer"), [(k, a) for k, v in ST5_ANSWERS.items() for a in v])
def test_st5_reader(read, answer: str) -> None:
    assert read_rules_answer(answer) == read
    assert read_answer("ST5", answer) == read


ST6_ANSWERS = {
    frozenset({"nordic-marine-dc=50", "vistula-dc=25"}): [
        "nordic-marine-dc holds 50% and vistula-dc 25%.",
        "| site | share |\n|---|---|\n| nordic-marine-dc | 50.0% |\n| vistula-dc | 25.0% |",
        "Your site holds 25 percent of the data; nordic-marine-dc holds 50 percent.",
        "- vistula-dc: 25% (65536 of 262144 bytes)\n- nordic-marine-dc: 50%",
    ],
    frozenset({"nordic-marine-dc=100", "vistula-dc=25"}): [
        "nordic-marine-dc: 100%; vistula-dc: 25%",
    ],
    None: ["The data are split between the two sites."],
}


@pytest.mark.parametrize(("pairs", "answer"), [(k, a) for k, v in ST6_ANSWERS.items() for a in v])
def test_st6_reader(pairs, answer: str) -> None:
    assert read_shares(answer) == pairs


ST7_ANSWERS = {
    frozenset({"nordic-marine-dc", "vistula-dc"}): [
        "nordic-marine-dc and vistula-dc",
        "| providerName | providerId |\n|---|---|\n| vistula-dc | pLocal |\n"
        "| nordic-marine-dc | pOwner |",
        "Two sites: vistula-dc (yours) and nordic-marine-dc, not oder-dc.",
    ],
    frozenset({"nordic-marine-dc", "vistula-dc", "oder-dc"}): [
        "vistula-dc, nordic-marine-dc and oder-dc",
    ],
    frozenset({"vistula-dc"}): ["Only vistula-dc."],
    None: ["I could not find out."],
}


@pytest.mark.parametrize(("names", "answer"), [(k, a) for k, v in ST7_ANSWERS.items() for a in v])
def test_st7_reader(names, answer: str) -> None:
    assert read_sites(answer) == names


# --- whole trials with the real tools on the fake federation ---------------------------------


def _path(brief_text: str) -> str:
    return re.search(r"`([^`]+)`", brief_text)[1]


async def _local_id(app, space: str) -> str:
    providers = await result_of(app, "list_space_providers", {"space_id": space})
    return next(
        p["providerId"] for p in providers["providers"] if p["providerName"] == "vistula-dc"
    )


def _row(tmp_path: Path, sid: str) -> dict:
    (row,) = [r for r in read_trials(tmp_path / "run") if r["scenario_id"] == sid]
    return row


def assert_rescores_offline(tmp_path: Path, sid: str, value, answer: str) -> None:
    """The stored trial scores again offline (rejudge): the context the oracle needs (the
    calls, the site's id, the rules and transfers in the baseline and the snapshots) survives
    the artefact. Its fake verdict has no input digest, so without a verdict it is
    NEEDS_REVIEW, and PASS is what the deterministic reading would give; with an agreeing
    verdict it is PASS."""
    row = _row(tmp_path, sid)
    again = rescore_stored(row)
    assert again["outcome"] == "NEEDS_REVIEW"
    assert again["outcome_if"]["deterministic"] == "PASS", again
    verdict = JudgeVerdict(sid, value=value, evidence=answer[:20], evidence_quoted=True)
    assert rescore_stored(row, verdict)["outcome"] == "PASS"


@pytest.mark.asyncio
async def test_st1_a_rule_naming_the_site_said_done_once_there_passes(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    fed.list_spaces = True

    async def model(app, brief_text):
        path = _path(brief_text)
        local = await _local_id(app, path.split("/")[1])
        await app.call_tool(
            "add_file_qos_requirement",
            {"file_id_or_path": path, "expression": f"providerId={local}"},
        )
        fed.tick()  # the rule is met; the model reads the blocks
        await app.call_tool("get_file_distribution", {"file_id_or_path": path})
        return "Done: a placement rule keeps the file at vistula-dc, and its data are fully there."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST1", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is True
    assert [c["tool"] for c in art.calls] == [
        "list_space_providers",
        "add_file_qos_requirement",
        "get_file_distribution",
    ]
    # read again only after the whole window, the rule re-read and evaluated then
    at, after = art.snapshot_at_answer, art.snapshot_after_settle
    assert after["taken_at"] - at["taken_at"] >= 60.0
    (rule,) = after["files"][ST1_FILE]["rules"].values()
    assert rule["replicasNum"] == 1 and [s["providerId"] for s in rule["matching"]] == [LOCAL_ID]
    assert_rescores_offline(tmp_path, "ST1", "SAYS_DONE", art.final_answer)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "replicas", "schedule", "outcome", "fed_pass", "why"),
    [
        # said done right away: true only after the settle
        (f"providerId={LOCAL_ID}", 1, False, "FAIL", True, "PREMATURE CLAIM"),
        # the owner already holds it: the rule is met, nothing comes to the site
        (f"providerId={OWNER_ID}", 1, False, "FAIL", False, "the local copy holds 0"),
        # the site's NAME, not its id: no storage matches it, the rule is never met
        ("providerId=vistula-dc", 1, False, "FAIL", False, "the local copy holds 0"),
        # two copies anywhere: the rule needs the site's storage
        ("anyStorage", 2, False, "PASS", True, ""),
        # ... and a scheduled copy beside it does not change that (the review's case)
        ("anyStorage", 2, True, "PASS", True, ""),
        # a rule at the owner, and the copy made by a transfer: no rule keeps it at the site
        (f"providerId={OWNER_ID}", 1, True, "FAIL", False, "no rule on the file keeps a copy"),
        # either site, one copy, and a scheduled copy: the owner meets the rule (the review's
        # false pass): the local blocks are there, but nothing keeps them
        (
            f"providerId={OWNER_ID}|providerId={LOCAL_ID}",
            1,
            True,
            "FAIL",
            False,
            "no rule on the file keeps a copy",
        ),
    ],
)
async def test_st1_judges_each_rule_by_its_meaning(
    cfg,
    api,
    fed,
    clock,
    tmp_path,
    tools_app,
    expression,
    replicas,
    schedule,
    outcome,
    fed_pass,
    why,
) -> None:
    async def model(app, brief_text):
        path = _path(brief_text)
        await app.call_tool(
            "add_file_qos_requirement",
            {"file_id_or_path": path, "expression": expression, "replicas_num": replicas},
        )
        if schedule:
            await app.call_tool(
                "schedule_file_replication",
                {"file_id_or_path": path, "target_provider_id": LOCAL_ID},
            )
        if outcome == "PASS" or schedule:
            fed.tick()
        return "Done: the file's data are fully at vistula-dc."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST1", model)

    assert art.outcome == outcome, art.oracle_diagnosis
    assert art.oracle_federation_pass is fed_pass
    assert why in art.oracle_diagnosis
    if why == "PREMATURE CLAIM":
        assert "premature_claim" in art.flags


@pytest.mark.asyncio
async def test_st1_a_rule_naming_the_site_removed_after_the_copy_fails(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    """The data arrive by a rule naming the site, which the agent then removes, keeping one
    at the owner: no rule keeps the file at the site, though its blocks are still there."""

    async def model(app, brief_text):
        path = _path(brief_text)
        for site in (OWNER_ID, LOCAL_ID):
            made = await result_of(
                app,
                "add_file_qos_requirement",
                {"file_id_or_path": path, "expression": f"providerId={site}"},
            )
        fed.tick()
        await app.call_tool("remove_qos_requirement", {"qos_id": made["qosRequirementId"]})
        return "Done: the file's data are fully at vistula-dc."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST1", model)

    assert art.outcome == "FAIL"
    assert art.oracle_federation_pass is False
    assert "no rule on the file keeps a copy at the site" in art.oracle_diagnosis


@pytest.mark.asyncio
async def test_st1_without_a_rule_misses_the_required_call(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief_text):
        await app.call_tool(
            "schedule_file_replication",
            {"file_id_or_path": _path(brief_text), "target_provider_id": LOCAL_ID},
        )
        fed.tick()
        return "Done: the file's data are fully at vistula-dc."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST1", model)

    assert art.outcome == "FAIL" and "required_call_missing" in art.flags


@pytest.mark.asyncio
async def test_st2_a_path_given_as_the_id_shows_the_hint_and_the_agent_recovers(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    """A path given as the rule id is refused before any request, with the new hint; the trial
    records that the hint was shown (`qos_id_hint_shown`) and that the agent's next call of a
    rule tool succeeded (`qos_id_hint_recovered`)."""
    hints: list[str] = []

    async def model(app, brief_text):
        qos_id = _path(brief_text)
        with pytest.raises(Exception) as refused:
            await app.call_tool("get_qos_requirement", {"qos_id": f"/{SPACE}/log_20200112.dat"})
        hints.append(str(refused.value))
        rule = await result_of(app, "get_qos_requirement", {"qos_id": qos_id})
        assert rule["replicasNum"] == 2
        return "It asks for 2 copies."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST2", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    (hint,) = hints
    assert "is a path, not a QoS requirement id" in hint and QOS_ID_HINT in hint
    assert [c["ok"] for c in art.calls] == [False, True]
    assert {"qos_id_hint_shown", "qos_id_hint_recovered"} <= set(art.flags)


@pytest.mark.asyncio
async def test_a_hint_not_followed_by_a_good_call_is_recorded_unrecovered(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief_text):
        for bad in ("providerId=x", "/a/path"):
            with pytest.raises(Exception, match="QoS requirement id"):
                await app.call_tool("get_qos_requirement", {"qos_id": bad})
        return "Two."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST2", model)

    assert {"qos_id_hint_shown", "qos_id_hint_unrecovered"} <= set(art.flags)
    assert [e["recovered"] for e in hint_exposures(art.calls)] == [False, False]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remove", "outcome", "flag"),
    [(False, "FAIL", "required_call_missing"), (True, "FAIL", "unrequested_change")],
)
async def test_st2_a_guess_or_a_removal_fails(
    cfg, api, fed, clock, tmp_path, tools_app, remove, outcome, flag
) -> None:
    async def model(app, brief_text):
        qos_id = _path(brief_text)
        if remove:
            await app.call_tool("get_qos_requirement", {"qos_id": qos_id})
            await app.call_tool("remove_qos_requirement", {"qos_id": qos_id})
        return "Two."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST2", model)

    assert art.outcome == outcome and flag in art.flags, art.oracle_diagnosis
    assert "qos_id_hint_shown" not in art.flags  # no hint was shown


async def _st3_model(app, brief_text, seen: dict | None = None):
    directory = _path(brief_text)
    one = []
    for name in ST3_NAMES:
        summary = await result_of(
            app, "get_file_qos_summary", {"file_id_or_path": f"{directory}/{name}"}
        )
        counts = [r["replicas_num"] for r in summary["requirements"].values()]
        if seen is not None:
            seen[name] = counts
        if counts == [1]:
            one.append(name)
    return " and ".join(one)


@pytest.mark.asyncio
async def test_st3_the_summary_gives_replicas_num_and_passes_its_shape_check(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    seen: dict[str, list[int]] = {}

    async def model(app, brief_text):
        return await _st3_model(app, brief_text, seen)

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST3", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert seen == {A: [1], B: [2], C: [1]}  # the result shape under test
    (shape,) = [c for c in art.checks if c["name"] == "summary_shape"]
    assert shape["passed"] is True


@pytest.mark.asyncio
async def test_st3_a_summary_without_replicas_num_fails_its_shape_check_whatever_the_answer(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    """The regression the smoke run must catch: summaries that carry the rule ids but no
    replicas_num, with a right answer reached through get_qos_requirement. The stored trial,
    its summaries stripped of the field, scores FAIL on `summary_shape`; its answer stays right."""

    async def model(app, brief_text):
        return await _st3_model(app, brief_text)

    await trial(cfg, api, clock, tmp_path, tools_app, "ST3", model)
    row = _row(tmp_path, "ST3")
    for c in row["calls"]:
        body = json.loads(c["result_text"])
        for entry in body["requirements"].values():
            entry.pop("replicas_num")
        c["result_text"] = json.dumps(body)
    verdict = JudgeVerdict("ST3", value=frozenset(ST3_ONE_COPY), evidence=A, evidence_quoted=True)

    again = rescore_stored(row, verdict)

    assert again["outcome"] == "FAIL" and again["oracle_mcp_pass"] is True
    assert "summary_shape" in again["flags"]
    (shape,) = [c for c in again["checks"] if c["name"] == "summary_shape"]
    assert shape["passed"] is False and "RESULT SHAPE" in shape["detail"]


@pytest.mark.asyncio
async def test_st3_one_summary_and_a_guess_misses_the_required_call(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def model(app, brief_text):
        await app.call_tool("get_file_qos_summary", {"file_id_or_path": f"{_path(brief_text)}/{A}"})
        return f"{A} and {C}"

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST3", model)

    assert art.outcome == "FAIL" and "required_call_missing" in art.flags


def _long_ids(fed: FakeFed) -> None:
    real_new_id = fed.new_id
    fed.new_id = lambda prefix: real_new_id(prefix) + "0" * 14  # 20+ characters, as on 25.0


@pytest.mark.asyncio
async def test_st4_reading_the_ended_transfers_passes(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    fed.list_spaces = True
    defaults: list[list] = []

    async def model(app, brief_text):
        path = _path(brief_text)
        space = path.split("/")[1]
        ongoing = await result_of(app, "list_space_transfers", {"space_id": space})
        defaults.append(ongoing["transfers"])  # the default lists ongoing transfers: none
        ended = await result_of(app, "list_space_transfers", {"space_id": space, "state": "ended"})
        for tid in ended["transfers"]:
            detail = await result_of(app, "get_transfer", {"transfer_id": tid})
            if detail["filePath"] == path:
                return tid  # the fake's short id, verbatim
        return "No transfer found."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST4", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert defaults == [[]]
    tid = art.fixture_baseline["files"][ST4_FILE]["transfer_id"]
    assert art.final_answer == tid
    assert_rescores_offline(tmp_path, "ST4", frozenset({tid}), art.final_answer)


@pytest.mark.asyncio
async def test_st4_the_newest_transfer_unread_is_the_wrong_answer(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    fed.list_spaces = True
    _long_ids(fed)

    async def model(app, brief_text):
        space = _path(brief_text).split("/")[1]
        ended = await result_of(app, "list_space_transfers", {"space_id": space, "state": "ended"})
        return ended["transfers"][0]  # the newest: the other file's

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST4", model)

    assert art.outcome == "FAIL" and "required_call_missing" in art.flags
    assert f"the later transfer, of {ST4_OTHER}" in art.oracle_diagnosis


@pytest.mark.asyncio
@pytest.mark.parametrize(("pick", "outcome"), [("filePath", "PASS"), ("other", "FAIL")])
async def test_st4i_reading_the_transfer_by_id(
    cfg, api, fed, clock, tmp_path, tools_app, pick, outcome
) -> None:
    _long_ids(fed)

    async def model(app, brief_text):
        tid = re.search(r"id `([^`]+)`", brief_text)[1]
        detail = await result_of(app, "get_transfer", {"transfer_id": tid})
        name = detail["filePath"].rsplit("/", 1)[-1]
        return name if pick == "filePath" else ({ST4_FILE, ST4_OTHER} - {name}).pop()

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST4i", model)

    assert art.outcome == outcome, art.oracle_diagnosis
    assert "<id of" not in art.brief


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "outcome"),
    [("They do not conflict: both can be met at once.", "PASS"), ("They conflict.", "FAIL")],
)
async def test_st5_both_rules_read_from_the_summary(
    cfg, api, fed, clock, tmp_path, tools_app, verdict, outcome
) -> None:
    async def model(app, brief_text):
        summary = await result_of(
            app, "get_file_qos_summary", {"file_id_or_path": _path(brief_text)}
        )
        counts = sorted(r["replicas_num"] for r in summary["requirements"].values())
        assert counts == [1, 2]  # both entries, each with its replicas_num
        return (
            f"Two rules apply: one asks for {counts[0]} copy, the other for {counts[1]} copies. "
            + verdict
        )

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST5", model)

    assert art.outcome == outcome, art.oracle_diagnosis
    (shape,) = [c for c in art.checks if c["name"] == "summary_shape"]
    assert shape["passed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("read_blocks", "outcome"), [(True, "PASS"), (False, "FAIL")])
async def test_st6_each_sites_share_from_the_block_map(
    cfg, api, fed, clock, tmp_path, tools_app, read_blocks, outcome
) -> None:
    fed.list_spaces = True  # the block map names the providers

    async def model(app, brief_text):
        if not read_blocks:  # the site's own rate only, and the rest by subtraction
            await app.call_tool(
                "get_file_attributes",
                {"file_id_or_path": _path(brief_text), "attributes": ["localReplicationRate"]},
            )
            return "nordic-marine-dc holds 75% and vistula-dc 25%."  # 100 - 25: wrong
        dist = await result_of(app, "get_file_distribution", {"file_id_or_path": _path(brief_text)})
        shares = []
        for entry in dist["distributionPerProvider"].values():
            blocks = next(iter(entry["distributionPerStorageBackend"].values()))["blocks"]
            held = sum(length for _, length in blocks)
            shares.append(
                f"{entry['providerName']} holds {round(100 * held / entry['logicalSize'])}%"
            )
        return " and ".join(shares) + "."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST6", model)

    assert art.outcome == outcome, art.oracle_diagnosis
    if not read_blocks:  # the subtraction fails on the answer itself, not only on the call
        assert art.oracle_mcp_pass is False and "nordic-marine-dc=75" in art.oracle_diagnosis
        assert "required_call_missing" in art.flags


@pytest.mark.asyncio
async def test_st7_the_sites_from_the_space_listing(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    fed.list_spaces = True

    async def model(app, brief_text):
        with pytest.raises(Exception, match="not offered"):
            await app.call_tool("get_file_distribution", {"file_id_or_path": _path(brief_text)})
        space = _path(brief_text).split("/")[1]
        listing = await result_of(app, "list_space_providers", {"space_id": space})
        return " and ".join(sorted(p["providerName"] for p in listing["providers"]))

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST7", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert sorted(art.offered_tools) == sorted(ST7_TOOLS)


# --- the offline right reading, the hint's text, and the report ------------------------------


def test_st4s_right_reading_is_the_trials_own_id() -> None:
    """rejudge's "says right" columns: ST4's right reading is the trial's transfer id, not a
    label, so it cannot be told offline (None), never True for an unreadable answer."""
    assert right_reading("ST4") is None
    assert is_right("ST4", None) is None and is_right("ST4", frozenset({"x" * 20})) is None
    assert is_right("ST3", frozenset(ST3_ONE_COPY)) is True
    assert is_right("ST2", "TWO") is True and is_right("ST2", "ONE") is False
    assert is_right("ST5", ("CAN_ALL_BE_MET", frozenset({"1", "2"}))) is True


def test_the_hint_mark_is_the_tools_own_sentence() -> None:
    from onedata_mcp.api.qos import _WHERE_IDS_ARE

    assert QOS_ID_HINT in _WHERE_IDS_ARE


def test_smoke_evidence_counts_target_matched_calls_only() -> None:
    row = {
        "space": SPACE,
        "directory": f"/{SPACE}/logs-x",
        "oracle_context": {"fixture_paths": {f"/{SPACE}/logs-x/log_20200107.dat": "f1"}},
        "fixture_baseline": {"files": {"log_20200107.dat": {"qos_ids": []}}},
        "calls": [
            {"tool": "get_file_qos_summary", "ok": True, "arguments": {"file_id_or_path": "f1"}},
            {
                "tool": "get_file_distribution",
                "ok": True,
                "arguments": {"file_id_or_path": f"/{SPACE}/elsewhere/log_20200107.dat"},
            },
            {"tool": "list_space_providers", "ok": False, "arguments": {"space_id": SPACE}},
        ],
    }
    assert smoke_evidence(row) == {"get_file_qos_summary"}


@pytest.mark.asyncio
async def test_the_report_section_counts_the_evidence_the_shape_and_the_hint(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    async def st3(app, brief_text):
        return await _st3_model(app, brief_text)

    await trial(cfg, api, clock, tmp_path, tools_app, "ST3", st3)
    lines = "\n".join(smoke_section(read_trials(tmp_path / "run")))
    assert "## The stable tools' smoke re-run (design/16)" in lines
    assert "checked in 1 trial(s), failed in 0" in lines
    assert (
        "| get_file_qos_summary | result: returns replicas_num | 1 of 1 (ST3, ST5) | - |" in lines
    )
    assert "| get_qos_requirement | error hints: the qos_id guard | 0 of 0 (ST2) | 0 of 1" in lines
    assert "NOT VERIFIED" in lines  # no trial was shown the hint

    async def st2(app, brief_text):
        with pytest.raises(Exception, match="is a path"):
            await app.call_tool("get_qos_requirement", {"qos_id": "/a/path"})
        await app.call_tool("get_qos_requirement", {"qos_id": _path(brief_text)})
        return "Two."

    await trial(cfg, api, clock, tmp_path, tools_app, "ST2", st2)
    lines = "\n".join(smoke_section(read_trials(tmp_path / "run")))
    assert "shown 1 time(s), followed by a recovery 1 time(s)" in lines
    assert "NOT VERIFIED" not in lines

    from benchmark.k2val.report import build_report

    assert "The stable tools' smoke re-run" in build_report(tmp_path / "run", cfg, run_id="r")


@pytest.mark.asyncio
async def test_the_http_legs_record_the_hint_where_the_flags_look_for_it(
    tmp_path: Path, tools_app
) -> None:
    """The Claude configs (and the CLI legs) reach the tools through `mcp_http`'s recording
    middleware, not the in-process RecordingApp: a refused get_qos_requirement must be
    recorded failed, with the hint in its `error` (where `hint_exposures` looks), and the
    model must be shown it."""
    from fastmcp import Client

    from benchmark.k2val.mcp_http import make_recording_middleware

    record = tmp_path / "mcp-calls.jsonl"
    tools_app.add_middleware(make_recording_middleware(record))
    async with Client(tools_app) as client:
        shown = await client.call_tool(
            "get_qos_requirement", {"qos_id": "/k2val-glm/log_20200112.dat"}, raise_on_error=False
        )

    assert shown.is_error is True and QOS_ID_HINT in shown.content[0].text
    (row,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert (row["tool"], row["ok"]) == ("get_qos_requirement", False)
    calls = [{**row, "index": 0, "source": "mcp"}]
    assert hint_exposures(calls) == [{"call": 0, "recovered": False, "next_call": None}]
