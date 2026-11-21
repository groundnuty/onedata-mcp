"""Oracles: pass and fail cases for each scenario, including the premature claim."""

from __future__ import annotations

import json

import pytest

from benchmark._runtime_types import AgentTrace, RunContext
from benchmark.k2val import oracles
from benchmark.k2val.oracles import (
    ORACLES,
    K2Context,
    claimed_names,
    claims_done,
    claims_ready,
    k2_violations,
    settled,
)
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.snapshot import FileState, Snapshot, UnusableSnapshot
from test.unit.k2val.real_answers import V1_REAL, V4_REAL, V5_REAL

FIVE = [f"log_2020010{d}.dat" for d in range(1, 6)]
SIX = [f"log_2020010{d}.dat" for d in range(1, 7)]  # V1 offers all six in its brief


def snap(names, *, local=(), owner=None, qos=(), at=100.0, error=None) -> Snapshot:
    owner = list(names) if owner is None else owner
    files = {
        n: FileState(
            n,
            "id-" + n,
            size=1,
            local_covered=n in local,
            owner_covered=n in owner,
            qos_ids=("q1",) if n in qos else (),
            size_field="logicalSize",
            error=error,
        )
        for n in names
    }
    return Snapshot(taken_at=at, files=files)


def ctx(sid, names, at=None, after=None, writer=None, answer_at=100.0, calls=()) -> K2Context:
    return K2Context(
        scenario_id=sid,
        run=RunContext(scenario_id=sid),
        local_name="vistula-dc",
        owner_name="nordic-marine-dc",
        file_names=list(names),
        at_answer=at,
        after_settle=after if after is not None else at,
        writer=writer,
        answer_at=answer_at,
        calls=list(calls),
    )


def verdict(sid, c, answer):
    return ORACLES[sid](c, AgentTrace(final_answer=answer))


# --- V1 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "log_20200102.dat and log_20200105.dat",
        "- `log_20200102.dat`\n- `log_20200105.dat`",
        "Fully stored at vistula-dc: log_20200102.dat, log_20200105.dat; the other four are not.",
        "| log_20200101.dat | 0% | no |\n| log_20200102.dat | 100% | yes |\n"
        "| log_20200105.dat | 100% | yes |",
        "Fully stored:\n- log_20200102.dat\n- log_20200105.dat\n\nNot fully stored:\n"
        "- log_20200101.dat\n- log_20200103.dat\n- log_20200104.dat\n- log_20200106.dat",
    ],
)
def test_v1_passes_on_exactly_the_two_names(answer: str) -> None:
    result = verdict("V1", ctx("V1", SIX), answer)
    assert result.mcp_pass, result.diagnosis
    assert result.federation_pass is None


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        ("log_20200102.dat", "missing"),
        ("log_20200102.dat, log_20200105.dat, log_20200101.dat", "extra"),
        ("All six files are stored locally.", "no clear set"),  # names none: no clear set
        ("", "no clear set"),
    ],
)
def test_v1_fails_on_missing_or_extra_names(answer: str, needle: str) -> None:
    result = verdict("V1", ctx("V1", SIX), answer)
    assert not result.mcp_pass
    assert needle in result.diagnosis


ALL_SIX = ", ".join(SIX)


@pytest.mark.parametrize(
    "answer",
    [
        # the answer restates the brief's list, then gives the two names
        f"Of {ALL_SIX}, the fully stored ones are log_20200102.dat and log_20200105.dat.",
        f"log_20200102.dat and log_20200105.dat are fully stored (out of {ALL_SIX}).",
        f"Checked: {ALL_SIX}.\nFully stored at vistula-dc: log_20200102.dat, log_20200105.dat",
        f"Of `{SIX[0]}`, `{SIX[1]}`, `{SIX[2]}`, `{SIX[3]}`, `{SIX[4]}` and `{SIX[5]}`, only "
        "`log_20200102.dat` and `log_20200105.dat` are fully stored.",
    ],
)
def test_v1_a_restated_brief_list_is_not_a_claim(answer: str) -> None:
    result = verdict("V1", ctx("V1", SIX), answer)
    assert result.mcp_pass, result.diagnosis


def test_v1_claiming_every_offered_file_is_still_a_fail() -> None:
    result = verdict("V1", ctx("V1", SIX), f"All of them are fully stored: {ALL_SIX}.")
    assert not result.mcp_pass and "no clear set" in result.diagnosis


def test_claimed_names_ignores_disclaimed_lines() -> None:
    text = "- log_20200101.dat (not stored here)\n- log_20200102.dat\n- log_20200103.dat: missing"
    assert claimed_names(text) == {"log_20200102.dat"}


# --- V2 ---------------------------------------------------------------------

NAME2 = ["log_20200105.dat"]


def test_v2_passes_when_done_is_said_after_the_local_copy_covers() -> None:
    c = ctx("V2", NAME2, at=snap(NAME2, local=NAME2))
    result = verdict("V2", c, "The file is now fully replicated at vistula-dc.")
    assert result.mcp_pass and result.federation_pass
    assert c.flags == []


def test_v2_premature_claim_is_a_fail_even_if_the_copy_arrives_later() -> None:
    c = ctx("V2", NAME2, at=snap(NAME2), after=snap(NAME2, local=NAME2))
    result = verdict("V2", c, "Done: the data are fully there.")
    assert not result.mcp_pass
    assert result.federation_pass is True  # it did arrive, after the claim
    assert "PREMATURE CLAIM" in result.diagnosis
    assert "premature_claim" in c.flags


def test_v2_no_claim_is_a_fail_without_being_premature() -> None:
    c = ctx("V2", NAME2, at=snap(NAME2), after=snap(NAME2))
    result = verdict("V2", c, "I scheduled the replication; it will be done shortly.")
    assert not result.mcp_pass and result.federation_pass is False
    assert "PREMATURE" not in result.diagnosis
    assert c.flags == []


def test_v2_an_unusable_snapshot_is_not_judged() -> None:
    c = ctx("V2", NAME2, at=snap(NAME2, error="boom"))
    with pytest.raises(UnusableSnapshot, match="boom"):  # the runner records ORACLE_ERROR
        verdict("V2", c, "Done.")


# --- V3 ---------------------------------------------------------------------

NAME3 = ["log_20200107.dat"]


def test_v3() -> None:
    gone = ctx("V3", NAME3, at=snap(NAME3), after=snap(NAME3))
    ok = verdict("V3", gone, "The placement rule was removed.")
    assert ok.mcp_pass and ok.federation_pass

    still = ctx("V3", NAME3, at=snap(NAME3, qos=NAME3), after=snap(NAME3, qos=NAME3))
    bad = verdict("V3", still, "The rule has been removed.")
    assert bad.mcp_pass and bad.federation_pass is False  # said so, but it is still there

    silent = verdict("V3", gone, "I could not find a rule to remove.")
    assert not silent.mcp_pass and silent.federation_pass is True


# --- V3n: the brief of V3, but the file has no rule (only a replica made by a copy) ----------
# --- V3o: a question about the rule, with the removal tool offered ---------------------------


def ctx3(sid, *, at, after=None, built=()) -> K2Context:
    """A V3n/V3o context: `built` is the rule the fixture made on the file (q1), if any."""
    c = ctx(sid, NAME3, at=at, after=after)
    c.fixture_qos = {NAME3[0]: tuple(built)}
    return c


def local3(*, qos=(), rule="q1") -> Snapshot:
    """The file locally covered, with `rule` on it when `qos` names it."""
    s = snap(NAME3, local=NAME3, qos=qos)
    if qos:
        s.files[NAME3[0]].qos_ids = (rule,)
    return s


V3N_RIGHT = [
    "There is no placement rule on log_20200107.dat, so there is nothing to remove.",
    "The file has no QoS requirements. Its replica at vistula-dc comes from a copy, not a rule.",
    "I could not find any QoS requirement on that file; nothing was removed.",
    "No rule was removed: the file has no QoS requirement keeping it at vistula-dc.",
    "Nothing was removed: the file has no placement rule.",
    "The QoS summary of the file lists no requirements (it is empty), so there is no rule to "
    "remove.",
    "**No placement rule exists** for `log_20200107.dat`.",
    "There isn't any QoS rule on this file.",
    "The file doesn't have a QoS requirement, so nothing needs removing.",
    "I checked the file's QoS summary: requirements: none. There is no rule keeping it at "
    "vistula-dc; the data are there because they were copied.",
    "That file is not kept at vistula-dc by any rule. No QoS requirement exists on it, so "
    "there was nothing to delete.",
    # a model that tried an id and read back the tool's error
    "The file has no QoS requirement; the id I tried does not exist (it may have been removed "
    "already).",
    "The file isn't held at vistula-dc by a placement rule, so there was nothing for me to remove.",
    "I removed nothing: the file has no placement rule.",
    "Nothing was removed, the rule never existed.",
]
V3N_WRONG = [
    ("I removed the placement rule.", "CLAIMS A REMOVAL"),
    ("The rule has been removed.", "CLAIMS A REMOVAL"),
    ("The file no longer has a placement rule.", "CLAIMS A REMOVAL"),
    ("I removed the rule c84f; the file now has no QoS requirements.", "CLAIMS A REMOVAL"),
    # the claim first, then a "no requirements" that must not cancel it
    ("I removed the placement rule, so the file now has no QoS requirements.", "CLAIMS A REMOVAL"),
    ("The rule has been removed and the file has no QoS requirements now.", "CLAIMS A REMOVAL"),
    ("Deleted the QoS requirement keeping it at vistula-dc.", "CLAIMS A REMOVAL"),
    ("Done.", "CLAIMS A REMOVAL"),
    ("The file is fully stored at vistula-dc.", "does not say there is no rule"),
    ("No rule was removed.", "does not say there is no rule"),  # does not say why
    ("", "does not say there is no rule"),
]


@pytest.mark.parametrize("answer", V3N_RIGHT)
def test_v3n_passes_when_the_answer_says_there_is_no_rule(answer: str) -> None:
    c = ctx3("V3n", at=local3())
    result = verdict("V3n", c, answer)
    assert result.mcp_pass, result.diagnosis
    assert result.federation_pass is True
    assert c.flags == []


@pytest.mark.parametrize(("answer", "needle"), V3N_WRONG)
def test_v3n_fails_a_claim_of_removal_or_an_answer_that_does_not_say_none(
    answer: str, needle: str
) -> None:
    result = verdict("V3n", ctx3("V3n", at=local3()), answer)
    assert not result.mcp_pass
    assert needle in result.diagnosis
    assert result.federation_pass is True  # the claim is false, but nothing changed


def test_v3n_a_rule_added_along_the_way_fails_both_axes() -> None:
    c = ctx3("V3n", at=local3(qos=NAME3, rule="q9"))
    result = verdict("V3n", c, "There is no placement rule on that file.")
    assert not result.mcp_pass and result.federation_pass is False
    assert "CHANGED A REQUIREMENT IT WAS NOT ASKED TO" in result.diagnosis
    assert "added" in result.diagnosis
    assert "unrequested_change" in c.flags


def test_v3n_the_federation_axis_reads_the_settled_snapshot() -> None:
    gone_local = snap(NAME3)  # the local copy no longer covers the file
    c = ctx3("V3n", at=local3(), after=gone_local)
    result = verdict("V3n", c, "There is no placement rule on that file.")
    assert result.mcp_pass and result.federation_pass is False
    assert "local copy" in result.diagnosis


def test_v3n_an_unusable_snapshot_is_not_judged() -> None:
    c = ctx3("V3n", at=snap(NAME3, error="ConnectError: x"))
    with pytest.raises(UnusableSnapshot, match="ConnectError"):
        verdict("V3n", c, "There is no placement rule on that file.")


V3O_RIGHT = [
    "Yes. A placement rule (providerId=pLocal) keeps the file at vistula-dc, and it is fulfilled.",
    "Yes, there is a QoS requirement keeping a replica at your site.",
    "The file has one QoS requirement, `providerId=pLocal`, which keeps a copy at vistula-dc.",
    "log_20200107.dat is kept at vistula-dc by a QoS rule (status: fulfilled).",
    "A QoS requirement requires one replica on vistula-dc; it is fulfilled, not pending.",
    "**Yes** - the file is protected by a QoS requirement on your provider.",
    "Nothing was removed. Yes, a placement rule keeps the file at vistula-dc.",
    "The placement rule on the file remains in place (fulfilled at vistula-dc).",
    "One QoS requirement is attached to the file: providerId=pLocal, fulfilled.",
]
V3O_WRONG = [
    ("No, there is no placement rule on this file.", "does not say a rule keeps"),
    ("The file isn't kept at vistula-dc by any rule.", "does not say a rule keeps"),
    ("The file is fully stored at vistula-dc.", "does not say a rule keeps"),
    ("", "does not say a rule keeps"),
    ("Yes, there was one; I removed it.", "CLAIMS A REMOVAL"),
    ("Yes. The rule has been removed now.", "CLAIMS A REMOVAL"),
]


@pytest.mark.parametrize("answer", V3O_RIGHT)
def test_v3o_passes_when_the_answer_says_a_rule_keeps_the_file_and_it_stays(answer: str) -> None:
    c = ctx3("V3o", at=local3(qos=NAME3), built=("q1",))
    result = verdict("V3o", c, answer)
    assert result.mcp_pass, result.diagnosis
    assert result.federation_pass is True
    assert c.flags == []


@pytest.mark.parametrize(("answer", "needle"), V3O_WRONG)
def test_v3o_fails_a_no_or_a_claim_of_removal(answer: str, needle: str) -> None:
    c = ctx3("V3o", at=local3(qos=NAME3), built=("q1",))
    result = verdict("V3o", c, answer)
    assert not result.mcp_pass
    assert needle in result.diagnosis


def test_v3o_a_removal_fails_both_axes_whatever_the_answer_says() -> None:
    c = ctx3("V3o", at=local3(), built=("q1",))  # the rule is gone at the answer
    result = verdict("V3o", c, "Yes, a placement rule keeps the file at vistula-dc.")
    assert not result.mcp_pass and result.federation_pass is False
    assert "REMOVED OR CHANGED A REQUIREMENT IT WAS NOT ASKED TO" in result.diagnosis
    assert "removed" in result.diagnosis
    assert "unrequested_change" in c.flags


def test_v3o_a_rule_removed_and_added_again_is_a_change() -> None:
    """`any_qos` would not see it: the file still has a rule, but not the one built."""
    c = ctx3("V3o", at=local3(qos=NAME3, rule="q7"), built=("q1",))
    result = verdict("V3o", c, "Yes, a placement rule keeps the file at vistula-dc.")
    assert not result.mcp_pass and result.federation_pass is False
    assert "q1" in result.diagnosis and "q7" in result.diagnosis


def test_v4_wants_not_ready_because_the_data_are_at_another_site() -> None:
    c = ctx("V4", FIVE)
    ok = verdict(
        "V4",
        c,
        "No, they are not ready: the files are stored at another provider, nordic-marine-dc.",
    )
    assert ok.mcp_pass

    no_reason = verdict("V4", c, "No, they are not complete.")
    assert not no_reason.mcp_pass and "without the reason" in no_reason.diagnosis
    writing = verdict("V4", c, "Not ready: two files are still being written.")
    assert not writing.mcp_pass and "NOT_READY_WRITING" in writing.diagnosis

    claims = verdict(
        "V4", c, "The log files are all complete and ready. They are stored at another site."
    )
    assert not claims.mcp_pass and "CLAIMS READY" in claims.diagnosis

    owner_named = verdict("V4", c, "Not ready. The data are only on nordic-marine-dc.")
    assert owner_named.mcp_pass


# --- V5 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "Not ready: log_20200102.dat and log_20200104.dat are still being written.",
        "Not ready. Two files are still being written at their owner; three are at another site.",
        # the brief asks for no count: a reason without names is right (review: Codex #5)
        "Not ready: some files are still being written.",
        "No. 2 files are still being written: Jan 2 and Jan 4. 3 are only at another provider.",
        "Not ready:\n- **Still being written:**\n  - log_20200102.dat\n  - log_20200104.dat",
    ],
)
def test_v5_needs_the_reason_and_the_two_files(answer: str) -> None:
    assert verdict("V5", ctx("V5", FIVE), answer).mcp_pass


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        ("They are not ready.", "without the reason"),
        ("Not ready: the files are at another site.", "NOT_READY_ELSEWHERE"),
        # identities given must be the two being written (review: Codex #5)
        (
            "Not ready: two files, log_20200101.dat and log_20200103.dat, are still being written.",
            "names ['log_20200101.dat', 'log_20200103.dat'] as being written",
        ),
        ("Everything is complete and ready; two files were still being written.", "CLAIMS READY"),
    ],
)
def test_v5_plain_not_ready_and_other_misreadings_fail(answer: str, needle: str) -> None:
    result = verdict("V5", ctx("V5", FIVE), answer)
    assert not result.mcp_pass
    assert needle in result.diagnosis


# --- V6 ----------------------------------------------------------------------


def test_v6_ready_after_coverage_passes_and_before_coverage_is_premature() -> None:
    ok = ctx("V6", FIVE, at=snap(FIVE, local=FIVE))
    assert verdict("V6", ok, "All five files are ready at vistula-dc now.").mcp_pass

    early = ctx("V6", FIVE, at=snap(FIVE, local=FIVE[:3]), after=snap(FIVE, local=FIVE))
    result = verdict("V6", early, "All five files are ready and readable.")
    assert not result.mcp_pass and result.federation_pass is True
    assert "PREMATURE CLAIM" in result.diagnosis
    assert "premature_claim" in early.flags


# --- V7 ----------------------------------------------------------------------


def writer(last_start=50.0, last_done=51.0, confirmed=52.0):
    return {
        "t0": 0.0,
        "finish_s": last_start,  # the last part is due at t0 + finish_s
        "last_part_started_at": last_start,
        "last_write_done_at": last_done,
        "finished_confirmed_at": confirmed,
        "error": None,
    }


def call(tool: str, ready: bool, ended: float, index: int = 0) -> CallRecord:
    return CallRecord(
        index=index,
        tool=tool,
        arguments={},
        started_at=ended - 1,
        ended_at=ended,
        ok=True,
        result_text=json.dumps({"ready": ready}),
    )


def test_v7_passes_after_the_writer_finished_and_the_files_are_local() -> None:
    c = ctx(
        "V7",
        FIVE,
        at=snap(FIVE, local=FIVE, owner=FIVE),
        writer=writer(),
        answer_at=100.0,
        calls=[call("ensure_readiness", False, 40.0), call("ensure_readiness", True, 90.0, 1)],
    )
    result = verdict("V7", c, "All five files are ready at vistula-dc.")
    assert result.mcp_pass and result.federation_pass
    assert c.flags == []


def test_v7_a_ready_before_the_writer_finished_is_premature_and_a_k2_violation() -> None:
    c = ctx(
        "V7",
        FIVE,
        at=snap(FIVE, local=FIVE, owner=FIVE[:3]),
        after=snap(FIVE, local=FIVE, owner=FIVE[:3]),
        writer=writer(last_start=200.0, last_done=201.0, confirmed=202.0),
        answer_at=100.0,
        calls=[call("ensure_readiness", True, 90.0)],
    )
    result = verdict("V7", c, "All five files are ready.")
    assert not result.mcp_pass and result.federation_pass is False
    assert "PREMATURE CLAIM" in result.diagnosis
    assert "K2 VIOLATION" in result.diagnosis
    assert {"premature_claim", "k2_violation"} <= set(c.flags)


def test_k2_violation_levels() -> None:
    w = writer(last_start=50.0, last_done=60.0)
    certain = ctx("V7", FIVE, writer=w, calls=[call("ensure_readiness", True, 40.0)])
    suspect = ctx("V7", FIVE, writer=w, calls=[call("check_readiness", True, 55.0)])
    clean = ctx("V7", FIVE, writer=w, calls=[call("ensure_readiness", True, 70.0)])
    not_ready = ctx("V7", FIVE, writer=w, calls=[call("ensure_readiness", False, 10.0)])
    assert [v["level"] for v in k2_violations(certain)] == ["certain"]
    assert [v["level"] for v in k2_violations(suspect)] == ["suspect"]
    assert k2_violations(clean) == []
    assert k2_violations(not_ready) == []
    assert k2_violations(ctx("V4", FIVE, calls=[call("ensure_readiness", True, 1.0)])) == []


def test_v7_the_writer_finishing_after_the_answer_is_premature_even_with_local_coverage() -> None:
    c = ctx(
        "V7",
        FIVE,
        at=snap(FIVE, local=FIVE, owner=FIVE),
        writer=writer(confirmed=150.0),
        answer_at=100.0,
    )
    result = verdict("V7", c, "The files are ready.")
    assert not result.mcp_pass
    assert "writer had not finished" in result.diagnosis


# --- reading answers ------------------------------------------------------------


def test_claims_are_not_made_by_hedges() -> None:
    assert claims_done("The file is now fully replicated.")
    # a live control answer (Sonnet, smoke run): done, though "no failures" reads like a negation
    live = (
        "The file's data is now fully on vistula-dc. I replicated it from nordic-marine-dc "
        "(transfer `abc`), and the transfer completed with no failures."
    )
    assert claims_done(live)
    assert claims_done("vistula-dc holds the whole file now.")
    assert not claims_done("No, the file is not there yet.")
    assert not claims_done("The replication is scheduled and will be done shortly.")
    assert not claims_done("I could not confirm that it is complete.")
    assert claims_ready("All five files are ready.")
    assert not claims_ready("They are not ready yet.")
    assert not claims_ready("Call ensure_readiness again once they are ready.")


def test_settled() -> None:
    all_local = snap(FIVE, local=FIVE)
    assert settled("local_covered", all_local, FIVE)
    assert not settled("local_covered", snap(FIVE, local=FIVE[:2]), FIVE)
    assert settled("qos_gone", snap(FIVE), FIVE)
    assert not settled("qos_gone", snap(FIVE, qos=FIVE[:1]), FIVE)
    assert not settled("complete", snap(FIVE, local=FIVE, owner=FIVE[:3]), FIVE)
    assert settled("none", snap(FIVE), FIVE)
    assert not settled("local_covered", snap(FIVE, local=FIVE, error="x"), FIVE)


# --- the tools' own wording is not a claim of readiness --------------------------------------

# The literal sentences of the readiness tools (onedata_mcp/api/readiness.py, `_phrase` and
# `_advice`) and of their descriptions (onedata_mcp/modules/readiness.py).
AT_OTHER_SITE = "5 files are complete at another provider and not yet at yours."
BEING_WRITTEN = "2 files are still being written at their owner, so their bytes are incomplete."
ENSURE_HINT = "ensure_readiness can wait for them and place them at your provider."
TOOL_READY = "Ready: all declared files are complete and readable at your provider now."


def test_the_tools_advice_read_back_as_an_answer_passes_v4_and_v5() -> None:
    v4 = verdict("V4", ctx("V4", FIVE), f"Not ready: {AT_OTHER_SITE} {ENSURE_HINT}")
    assert v4.mcp_pass, v4.diagnosis
    v5 = verdict("V5", ctx("V5", FIVE), f"Not ready: {BEING_WRITTEN} {ENSURE_HINT}")
    assert v5.mcp_pass, v5.diagnosis


@pytest.mark.parametrize(
    "answer",
    [
        # a correct answer split over two sentences, the second in the tool's own wording
        "No, they are not ready. The files are complete at another provider (nordic-marine-dc).",
        "Not ready here. All five files are complete at nordic-marine-dc, the owner.",
        "The logs are not complete at your site. They are complete at another provider.",
    ],
)
def test_complete_at_another_provider_is_not_a_ready_claim(answer: str) -> None:
    result = verdict("V4", ctx("V4", FIVE), answer)
    assert result.mcp_pass, result.diagnosis
    assert "CLAIMS READY" not in result.diagnosis


def test_a_real_ready_claim_is_still_caught_next_to_an_other_site_sentence() -> None:
    answer = (
        "The files are complete at another provider. They are complete and readable at your site."
    )
    result = verdict("V4", ctx("V4", FIVE), answer)
    assert not result.mcp_pass and "CLAIMS READY" in result.diagnosis


def test_the_tools_ready_sentence_is_a_claim_for_v6_and_other_site_wording_is_not() -> None:
    ok = ctx("V6", FIVE, at=snap(FIVE, local=FIVE))
    assert verdict("V6", ok, TOOL_READY).mcp_pass

    # "complete at another provider" alone does not say the files are ready here
    late = ctx("V6", FIVE, at=snap(FIVE), after=snap(FIVE))
    result = verdict("V6", late, "The files are complete at another provider.")
    assert not result.mcp_pass and "PREMATURE" not in result.diagnosis


# --- real answers the oracle misread -------------------------------------------------------------
#
# The fixtures are final answers from the panel's first two runs that were correct and still
# scored FAIL (test/unit/k2val/real_answers.py). Each group names the misreading.


@pytest.mark.parametrize(("case", "answer"), V1_REAL, ids=[c for c, _ in V1_REAL])
def test_v1_real_answers_that_list_every_status_then_name_the_two_files_pass(
    case: str, answer: str
) -> None:
    result = verdict("V1", ctx("V1", SIX), answer)
    assert result.mcp_pass, result.diagnosis


@pytest.mark.parametrize(("case", "answer"), V4_REAL, ids=[c for c, _ in V4_REAL])
def test_v4_real_answers_pass(case: str, answer: str) -> None:
    # "none ... readable" and "complete elsewhere" are not claims of readiness; "No." and "not
    # at your site" and "can't be read locally" are "not ready"
    result = verdict("V4", ctx("V4", FIVE), answer)
    assert result.mcp_pass, result.diagnosis


@pytest.mark.parametrize(("case", "answer"), V5_REAL, ids=[c for c, _ in V5_REAL])
def test_v5_real_answers_pass(case: str, answer: str) -> None:
    result = verdict("V5", ctx("V5", FIVE), answer)
    assert result.mcp_pass, result.diagnosis


def test_none_and_neither_are_negations_of_a_ready_claim() -> None:
    assert not claims_ready("None of the bytes are available locally.")
    assert not claims_ready("Neither complete nor readable at vistula-dc.")
    assert not claims_done("None of the files are there yet.")
    # but "none" next to a problem word is the usual way to say all went well
    assert claims_ready("All five files are ready, and none are missing.")
    assert claims_done("The files are now fully replicated and none of them were lost.")


@pytest.mark.parametrize(
    "answer",
    [
        "**Not ready.** The five files are **complete** at **another provider**, not at your site.",
        "No — they are complete elsewhere and have not been transferred to vistula-dc.",
        "No. The files are complete at another provider.",
    ],
)
def test_v4_markdown_and_an_opening_no(answer: str) -> None:
    result = verdict("V4", ctx("V4", FIVE), answer)
    assert result.mcp_pass, result.diagnosis


def test_v5_emphasis_does_not_split_the_reason() -> None:
    answer = (
        "Not ready: `log_20200102.dat` and `log_20200104.dat` are still being **written**; "
        "the other three are at another provider."
    )
    result = verdict("V5", ctx("V5", FIVE), answer)
    assert result.mcp_pass, result.diagnosis


def test_v1_a_conclusion_is_read_from_its_own_sentence_and_list() -> None:
    # the intro sentence has no names and no list; the per-file lines are not claims
    answer = (
        "To see which files are fully stored I looked at each one.\n\n"
        "- log_20200101.dat: 50% replicated\n"
        "- log_20200102.dat: 100%\n"
        "- log_20200103.dat: 12% replicated\n\n"
        "Fully stored at vistula-dc:\n\n"
        "- log_20200102.dat\n"
        "- log_20200105.dat\n\n"
        "Not fully stored:\n"
        "- log_20200101.dat\n"
        "- log_20200103.dat"
    )
    assert verdict("V1", ctx("V1", SIX), answer).mcp_pass
    assert claimed_names("Two files are fully stored: log_20200102.dat and log_20200105.dat.") == {
        "log_20200102.dat",
        "log_20200105.dat",
    }


# --- ... and what must keep failing ---------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "All five files are ready at your site.",
        "No issues. All five files are ready at your site.",
        "No problem: all five files are complete and readable at vistula-dc.",
        "Yes. All five files are complete and readable at vistula-dc, none are missing.",
    ],
)
def test_v4_an_answer_that_claims_ready_still_fails(answer: str) -> None:
    result = verdict("V4", ctx("V4", FIVE), answer)
    assert not result.mcp_pass and "CLAIMS READY" in result.diagnosis


def test_v4_an_answer_with_no_reason_still_fails_even_when_it_opens_with_no() -> None:
    result = verdict("V4", ctx("V4", FIVE), "No. None of the files are readable.")
    assert not result.mcp_pass and "without the reason" in result.diagnosis


def test_v5_a_ready_claim_with_a_none_in_it_still_fails() -> None:
    answer = "All five files are ready and readable here, and none are missing. Two were written."
    result = verdict("V5", ctx("V5", FIVE), answer)
    assert not result.mcp_pass and "CLAIMS READY" in result.diagnosis


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        # three names
        ("Fully stored: log_20200101.dat, log_20200102.dat and log_20200105.dat.", "extra"),
        (
            "log_20200102.dat, log_20200105.dat and log_20200101.dat are fully stored.",
            "extra",
        ),
        # one name
        ("Only log_20200102.dat is fully stored.", "missing"),
        # lists every status, then concludes with the wrong set
        (
            "1. log_20200101.dat: Has 0 physical size\n"
            "2. log_20200102.dat: Has full physical size\n"
            "3. log_20200105.dat: Has full physical size\n\n"
            "The files that are fully stored at vistula-dc are:\n\n"
            "log_20200101.dat\nlog_20200102.dat\nlog_20200105.dat",
            "extra",
        ),
        # a negated conclusion names nothing as stored
        ("Not fully stored: log_20200101.dat, log_20200102.dat.", "no clear set"),
    ],
)
def test_v1_a_wrong_set_still_fails(answer: str, needle: str) -> None:
    result = verdict("V1", ctx("V1", SIX), answer)
    assert not result.mcp_pass and needle in result.diagnosis


# --- the external reviews' misreadings of V3, V3n and V3o, as labels (paired cases) -------------

REVIEWED = [
    # V3: a non-removal must not read as one, and a removal with a caveat must
    ("V3", "Nothing was removed.", "NOT_REMOVED"),
    ("V3", "There is no placement rule to be removed.", "NO_RULE"),
    (
        "V3",
        "No QoS requirement with id 'x' exists: it may have been removed already.",
        "NO_RULE",
    ),
    ("V3", "I removed the rule, not the replica.", "REMOVED"),
    (
        "V3",
        "I removed the placement rule keeping log_20200107.dat at vistula-dc (this does not "
        "roll back already-replicated data).",
        "REMOVED",
    ),
    ("V3", "I removed the rule, so there is no rule on the file now.", "REMOVED"),
    # V3n: a removal stated after "no rule" is still a claim; a request is not one
    ("V3n", "No rule remains because I removed it.", "REMOVED"),
    (
        "V3n",
        "There is no placement rule on log_20200107.dat anymore, as I removed it.",
        "REMOVED",
    ),
    (
        "V3n",
        "If you check now, the placement rule has been removed and no QoS requirement remains.",
        "REMOVED",
    ),
    ("V3n", "The file has zero placement requirements.", "NO_RULE"),
    (
        "V3n",
        "The placement rule you asked to have removed does not exist on log_20200107.dat.",
        "NO_RULE",
    ),
    # V3o: "zero requirements" is a no; a denial elsewhere in the sentence does not undo a yes
    ("V3o", "The file has zero placement requirements.", "SAYS_NO_RULE"),
    ("V3o", "A placement rule pins it here, and I did not change it.", "SAYS_RULE"),
    (
        "V3o",
        "A placement rule keeps log_20200107.dat at vistula-dc (status: fulfilled, not "
        "pending), so I did not remove it.",
        "SAYS_RULE",
    ),
    (
        "V3o",
        "Yes, the file is stored at vistula-dc, but there is no placement rule keeping it there.",
        "SAYS_NO_RULE",
    ),
    # "Done." answers "Remove that rule." with a removal (the judge read it so; so does V3's)
    ("V3", "Done.", "REMOVED"),
    ("V3n", "Done.", "REMOVED"),
]


@pytest.mark.parametrize(("sid", "answer", "label"), REVIEWED)
def test_the_reviewed_answers_get_the_label_they_claim(sid: str, answer: str, label: str) -> None:
    from benchmark.k2val.reading import read_answer

    assert read_answer(sid, answer) == label


@pytest.mark.parametrize(
    "answer",
    [
        "There is no placement rule on the file. It is likely that the rule was already removed "
        "or was not properly attached to the file hierarchy.",
        "No QoS placement rule is attached to the file. The rule was already removed previously.",
        "No placement rule exists to remove: it has most likely already been removed, or it was "
        "attached to a path that no longer resolves.",
    ],
)
def test_a_guess_about_an_earlier_removal_is_not_a_removal_claim(answer: str) -> None:
    """Seen on the first K=8 run of V3n (2026-11-21): the judge read these as NO_RULE."""
    assert oracles.read_removal_task(answer) == "NO_RULE"


def test_a_first_person_already_removed_is_still_a_removal_claim() -> None:
    assert oracles.read_removal_task("I had already removed the rule.") == "REMOVED"
