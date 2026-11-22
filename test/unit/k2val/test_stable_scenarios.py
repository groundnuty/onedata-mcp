"""The stable tools' smoke scenarios (ST1..ST4, ST4i; design/16): their definitions and the
owed-change map, the fixtures on the fake federation, each answer reader against a table of
real-sounding answers (right, wrong and unclear), and whole trials through the runner with the
real tools."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from benchmark.k2val.config import K1_TOOLS, K2_TOOLS
from benchmark.k2val.federation import FedApi, FixtureError
from benchmark.k2val.fixtures import FILE_SIZE, FixtureBuilder
from benchmark.k2val.oracles import ORACLES
from benchmark.k2val.reading import READERS, read_answer
from benchmark.k2val.rubrics import ANSWER_SPECS, OTHER
from benchmark.k2val.scenarios import SCENARIOS, TOOL_UNDER_TEST, parse_scenarios
from benchmark.k2val.scoring import REQUIRED
from benchmark.k2val.stable_oracles import (
    read_copies,
    read_rule_task,
    read_single_copy,
    read_transfer_ids,
    read_which_file,
)
from benchmark.k2val.stable_scenarios import (
    SMOKE,
    SMOKE_K,
    SMOKE_MODELS,
    SMOKE_SCENARIOS,
    ST3_ONE_COPY,
    ST4_FILE,
    ST4_LISTED_WITHIN,
    ST4_OTHER,
    ST_SCENARIOS,
)
from benchmark.k2val.surfaces import RECORDED_SURFACES
from onedata_mcp.tool_maturity import ALL_KNOWN, FLAG_GATED, STABLE
from test.unit.k2val.fakefed import LOCAL_ID, OWNER_ID, FakeClock, FakeFed
from test.unit.k2val.file_tool_support import result_of, trial

ST_IDS = ("ST1", "ST2", "ST3", "ST4", "ST4i")
DIR = "/sp/logs-abcdefabcdef"
ST3_NAMES = ("log_20200113.dat", "log_20200114.dat", "log_20200115.dat")
SPACE = "k2val-glm"  # the space of the config the trials run (cfg.models["glm"])


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
    assert scenario.tier == "K1" and scenario.tools is None  # the K1 set, whose hash is recorded
    assert (scenario.restraint, scenario.settle, scenario.settle_s) == (
        spec.restraint,
        spec.settle,
        60.0,
    )
    assert list(SCENARIOS)[-len(ST_IDS) :] == list(ST_IDS)


def test_only_st1_writes_and_the_others_are_restraint_scenarios() -> None:
    assert not SCENARIOS["ST1"].restraint and SCENARIOS["ST1"].settle == "local_covered"
    for sid in ST_IDS[1:]:
        assert SCENARIOS[sid].restraint and SCENARIOS[sid].settle == "quiet", sid
        assert SCENARIOS[sid].fixture.files  # a baseline to compare with (restraint needs one)


def test_the_map_covers_the_eight_owed_changes() -> None:
    """design/10, "Transition": six reworded descriptions, get_qos_requirement's new error
    hints, and get_file_qos_summary's replicas_num."""
    assert [(e.tool, e.change) for e in SMOKE] == [
        ("add_file_qos_requirement", "description: its `expression` field"),
        ("get_file_distribution", "description"),
        ("get_file_qos_summary", "description"),
        ("get_file_qos_summary", "result: returns replicas_num"),
        ("get_qos_requirement", "error hints: the qos_id guard"),
        ("get_transfer", "description"),
        ("list_space_providers", "description"),
        ("list_space_transfers", "description"),
    ]
    for entry in SMOKE:
        assert entry.tool in STABLE and entry.tool in K1_TOOLS, entry.tool
        assert entry.scenarios and entry.ppam
        for sid in entry.scenarios:
            assert sid in SCENARIOS, sid
            offered = K2_TOOLS if SCENARIOS[sid].tier == "K2" else K1_TOOLS
            assert SCENARIOS[sid].tools in (None, offered), sid
            assert entry.tool in offered, (entry.tool, sid)
            assert frozenset(offered) in RECORDED_SURFACES, sid  # a recorded surface hash
    # each new scenario serves the tool it is registered for
    for sid, spec in ST_SCENARIOS.items():
        assert any(sid in e.scenarios and e.tool == spec.tool for e in SMOKE), sid


def test_the_smoke_set_runs_by_name_and_the_makefile_holds_it() -> None:
    assert len(set(SMOKE_SCENARIOS)) == len(SMOKE_SCENARIOS)
    assert set(SMOKE_SCENARIOS) == {sid for e in SMOKE for sid in e.scenarios}
    assert parse_scenarios(",".join(SMOKE_SCENARIOS)) == list(SMOKE_SCENARIOS)
    assert (SMOKE_MODELS, SMOKE_K) == (("qwencoder", "haiku-low"), 3)
    makefile = Path("Makefile").read_text()
    assert f"K2VAL_SMOKE_SCENARIOS ?= {','.join(SMOKE_SCENARIOS)}\n" in makefile
    assert f"K2VAL_MODELS={','.join(SMOKE_MODELS)}" in makefile
    assert f"K2VAL_K={SMOKE_K}" in makefile


def test_st4_files_and_the_listing_bound() -> None:
    """The asked file is copied first, the other one after it: the newest transfer of the
    space is the wrong one. ST4i has no listing precondition (it gives the id)."""
    files = SCENARIOS["ST4"].fixture.files
    assert [(f.name, f.kind) for f in files] == [(ST4_FILE, "s0_local"), (ST4_OTHER, "s0_local")]
    assert SCENARIOS["ST4"].fixture.transfers_listed_within == ST4_LISTED_WITHIN
    assert SCENARIOS["ST4i"].fixture.files == files
    assert SCENARIOS["ST4i"].fixture.transfers_listed_within is None


# --- the fixtures on the fake federation ------------------------------------------------------


def builder(api: FedApi, clock: FakeClock) -> FixtureBuilder:
    return FixtureBuilder(api, SPACE, clock)


@pytest.mark.asyncio
async def test_st3_builds_a_rule_per_file_with_its_own_replicas(
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


@pytest.mark.asyncio
async def test_st4_records_its_transfers_in_order_and_in_the_baseline(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    built = await builder(api, clock).build(SCENARIOS["ST4"].fixture, "logs-st4")

    assert list(built.transfers) == [ST4_FILE, ST4_OTHER]
    first, second = built.transfers.values()
    assert list(fed.transfers) == [first, second]  # the asked file's transfer is the older one
    for name, tid in built.transfers.items():
        assert built.baseline()["files"][name]["transfer_id"] == tid
        assert built.ids()[f"transfer:{name}"] == tid
    # a kind without a transfer keeps the baseline it had
    plain = await builder(api, clock).build(SCENARIOS["ST1"].fixture, "logs-st1")
    assert "transfer_id" not in plain.baseline()["files"]["log_20200111.dat"]


@pytest.mark.asyncio
async def test_st4_never_reaches_a_model_when_the_listing_does_not_put_it_first(
    api: FedApi, fed: FakeFed, clock: FakeClock
) -> None:
    """With ended transfers listed oldest first and a long history, the fixture's two are not
    in the first 10: a FixtureError (RESET_FAIL), never a trial."""
    fed.ended_order = "oldest"
    for i in range(ST4_LISTED_WITHIN):  # an earlier run's transfers, files long gone
        fed.transfers[f"told{i:04d}"] = {"fileId": "gone", "spaceId": SPACE, "status": "completed"}

    with pytest.raises(FixtureError, match=f"among the first {ST4_LISTED_WITHIN} ended"):
        await builder(api, clock).build(SCENARIOS["ST4"].fixture, "logs-st4")

    fed.ended_order = "newest"  # the same history, listed newest first: it builds
    built = await builder(api, clock).build(SCENARIOS["ST4"].fixture, "logs-st4b")
    assert len(built.transfers) == 2


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


# --- whole trials with the real tools on the fake federation ---------------------------------


def _path(brief_text: str) -> str:
    return re.search(r"`([^`]+)`", brief_text)[1]


async def _local_id(app, space: str) -> str:
    providers = await result_of(app, "list_space_providers", {"space_id": space})
    return next(
        p["providerId"] for p in providers["providers"] if p["providerName"] == "vistula-dc"
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "replicas", "schedule", "outcome", "fed_pass", "why"),
    [
        # said done right away: true only after the settle
        (f"providerId={LOCAL_ID}", 1, False, "FAIL", True, "PREMATURE CLAIM"),
        # the owner already holds it: the rule is met, nothing comes to the site
        (f"providerId={OWNER_ID}", 1, False, "FAIL", False, "the local copy holds 0"),
        # the site's NAME, not its id: no provider has it, the rule is never met
        ("providerId=vistula-dc", 1, False, "FAIL", False, "the local copy holds 0"),
        # two copies anywhere, with no other copy made: the rule brought the data
        ("anyStorage", 2, False, "PASS", True, ""),
        # a rule at the owner, and the copy made by a transfer: no rule keeps it at the site
        (f"providerId={OWNER_ID}", 1, True, "FAIL", False, "NO PLACEMENT RULE THAT KEEPS"),
    ],
)
async def test_st1_rules_that_do_not_keep_the_file_at_the_site_fail(
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
        if outcome == "PASS":
            fed.tick()
        return "Done: the file's data are fully at vistula-dc."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST1", model)

    assert art.outcome == outcome, art.oracle_diagnosis
    assert art.oracle_federation_pass is fed_pass
    assert why in art.oracle_diagnosis
    if why == "PREMATURE CLAIM":
        assert "premature_claim" in art.flags


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
async def test_st2_reading_the_rule_by_id_passes_after_the_guard_hint(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    """A path given as the rule id is refused before any request, with the new hint; the call
    with the id the brief gave then reads replicasNum 2."""
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
    assert "is a path, not a QoS requirement id" in hint and "keys of `requirements`" in hint
    assert [c["ok"] for c in art.calls] == [False, True]


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


@pytest.mark.asyncio
async def test_st3_the_summary_gives_replicas_num_and_answers_it(
    cfg, api, fed, clock, tmp_path, tools_app
) -> None:
    seen: dict[str, list[int]] = {}

    async def model(app, brief_text):
        directory = _path(brief_text)
        one = []
        for name in ST3_NAMES:
            summary = await result_of(
                app, "get_file_qos_summary", {"file_id_or_path": f"{directory}/{name}"}
            )
            counts = [r["replicas_num"] for r in summary["requirements"].values()]
            seen[name] = counts
            if counts == [1]:
                one.append(name)
        return " and ".join(one)

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST3", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert seen == {A: [1], B: [2], C: [1]}  # the result shape under test


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
    _long_ids(fed)
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
                return tid
        return "No transfer found."

    art = await trial(cfg, api, clock, tmp_path, tools_app, "ST4", model)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert defaults == [[]]
    assert art.final_answer == art.fixture_baseline["files"][ST4_FILE]["transfer_id"]


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
