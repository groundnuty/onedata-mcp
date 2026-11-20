"""The oracles of schedule_file_replication's scenarios (SR1, SR1n, SR1o; design/12, gate 4).

First the answer readers, each with a table of real-sounding answers per label (negations,
"nothing was ...", "zero ...", mixed sentences); then the verdicts on both axes, built from
snapshots and recorded calls, including the premature claim and the unrequested change.
"""

from __future__ import annotations

import pytest

from benchmark._runtime_types import AgentTrace, RunContext
from benchmark.k2val.oracles import ORACLES, K2Context, settled
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.scenarios import SR_FILE
from benchmark.k2val.snapshot import FileState, Snapshot
from benchmark.k2val.sr_oracles import read_copied_question, read_copy_answer, read_refusal

LOCAL = "vistula-dc"
BIG = 128 * 1024 * 1024
SMALL = 256 * 1024

# ---------------------------------------------------------------------------------------------
# SR1: read_copy_answer
# ---------------------------------------------------------------------------------------------

SR1_SAYS_WHOLE = [
    "The copy is complete: log_20200110.dat is now fully stored at vistula-dc.",
    "Done. vistula-dc now holds all 134217728 bytes of the file.",
    "I scheduled the replication and waited; the file is now fully stored at vistula-dc.",
    "The replication finished and the block map at vistula-dc covers the whole file (100%).",
    "The data are fully there, and no placement rule was set up.",
    "The data are fully at vistula-dc. No QoS requirement was created.",
    "Copied without any placement rule: the file is completely present at vistula-dc.",
    "**Done** - all of its data are at your site vistula-dc now.",
    "The transfer status read completed early, so I checked the blocks: the file is now fully "
    "at vistula-dc.",
]
SR1_SAYS_NOT_YET = [
    "I scheduled the replication (transfer t1). It is still in progress.",
    "The replication has been scheduled; it will finish shortly.",
    "The transfer status reads completed, but the block map shows 64 MiB of 128 MiB at "
    "vistula-dc, so the copy is not complete yet.",
    "The copy is partial: vistula-dc holds 40% of the file.",
    "The data are being copied to vistula-dc; not all blocks are there yet.",
    "The replication is pending.",
]
SR1_OTHER = [
    "",
    "I scheduled the replication to vistula-dc (transfer id t1).",  # says nothing of the data
    "The transfer status is completed.",  # the status alone is not the data
    "I could not copy the file.",
    "Nothing was copied.",
    "Zero bytes were copied.",
    # both at once: left to manual review, never a pass
    "Done: the file is fully at vistula-dc, though some blocks are still missing.",
]


@pytest.mark.parametrize("answer", SR1_SAYS_WHOLE)
def test_sr1_reads_a_claim_that_the_copy_is_whole(answer: str) -> None:
    assert read_copy_answer(answer) == "SAYS_WHOLE"


@pytest.mark.parametrize("answer", SR1_SAYS_NOT_YET)
def test_sr1_reads_an_answer_that_the_copy_is_not_whole_yet(answer: str) -> None:
    assert read_copy_answer(answer) == "SAYS_NOT_YET"


@pytest.mark.parametrize("answer", SR1_OTHER)
def test_sr1_reads_anything_else_as_other(answer: str) -> None:
    assert read_copy_answer(answer) == "OTHER"


# ---------------------------------------------------------------------------------------------
# SR1n: read_refusal
# ---------------------------------------------------------------------------------------------

SR1N_NOT_POSSIBLE = [
    "I can't copy it to oder-dc: oder-dc does not support this space. Its providers are "
    "vistula-dc and nordic-marine-dc.",
    "oder-dc is not one of the providers of this space, so nothing was copied.",
    "There is no provider named oder-dc in this space; the file stays where it is.",
    "No copy was made and no transfer was scheduled: oder-dc isn't among the space's providers.",
    "The space is supported only by vistula-dc and nordic-marine-dc, so a copy at oder-dc is not "
    "possible.",
    "oder-dc is an unknown site; I did not set up any placement rule or transfer.",
    "Not possible. oder-dc doesn't exist in this federation.",
    "Zero copies were made, because oder-dc is not a provider that supports the space.",
]
SR1N_CLAIMS_A_COPY = [
    "oder-dc does not support the space, so I copied it to vistula-dc instead.",
    "The copy at oder-dc is complete.",
    "I scheduled a replication to vistula-dc, since oder-dc is not available.",
    "Done: the data are fully there.",
]
SR1N_OTHER = [
    "",
    "I couldn't make the copy.",  # no reason
    "The replication failed.",
    "Nothing was done.",
]


@pytest.mark.parametrize("answer", SR1N_NOT_POSSIBLE)
def test_sr1n_reads_a_refusal_with_its_reason(answer: str) -> None:
    assert read_refusal(answer) == "SAYS_NOT_POSSIBLE"


@pytest.mark.parametrize("answer", SR1N_CLAIMS_A_COPY)
def test_sr1n_reads_a_claim_of_a_copy(answer: str) -> None:
    assert read_refusal(answer) == "CLAIMS_A_COPY"


@pytest.mark.parametrize("answer", SR1N_OTHER)
def test_sr1n_reads_a_bare_refusal_or_silence_as_other(answer: str) -> None:
    assert read_refusal(answer) == "OTHER"


# ---------------------------------------------------------------------------------------------
# SR1o: read_copied_question
# ---------------------------------------------------------------------------------------------

SR1O_NOT_COPIED = [
    "No. Its data are only at nordic-marine-dc.",
    "No, it has not been copied: vistula-dc holds 0 bytes of it.",
    "The file hasn't been copied to vistula-dc; all 262144 bytes are at nordic-marine-dc.",
    "It is not present at your site: none of its blocks are there.",
    "**No** - the block map shows zero bytes at vistula-dc.",
    "Nothing of it is at vistula-dc yet; the file is not replicated there.",
    "It is stored at nordic-marine-dc, not at vistula-dc.",
]
SR1O_COPIED = [
    "Yes, it has been copied.",
    "Yes. The file is fully present at vistula-dc.",
    "The file is already stored at vistula-dc.",
    "I copied it to vistula-dc just now.",
]
SR1O_OTHER = [
    "",
    "The file is 262144 bytes long.",
    "No, it had not been copied, so I have now copied it to vistula-dc.",  # both: a change
]


@pytest.mark.parametrize("answer", SR1O_NOT_COPIED)
def test_sr1o_reads_no(answer: str) -> None:
    assert read_copied_question(answer, LOCAL) == "SAYS_NOT_COPIED"


@pytest.mark.parametrize("answer", SR1O_COPIED)
def test_sr1o_reads_yes(answer: str) -> None:
    assert read_copied_question(answer, LOCAL) == "SAYS_COPIED"


@pytest.mark.parametrize("answer", SR1O_OTHER)
def test_sr1o_reads_silence_or_both_as_other(answer: str) -> None:
    assert read_copied_question(answer, LOCAL) == "OTHER"


# ---------------------------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------------------------


def state(*, size=BIG, local=0, qos=(), owner=True, error=None) -> Snapshot:
    """The SR file as both providers report it: `local` bytes at the local provider."""
    return Snapshot(
        taken_at=100.0,
        files={
            SR_FILE: FileState(
                SR_FILE,
                "fid-sr",
                size=size,
                local_bytes=local,
                owner_bytes=size if owner else 0,
                local_covered=local >= size,
                owner_covered=owner,
                qos_ids=tuple(qos),
                size_field="logicalSize",
                error=error,
            )
        },
    )


def call(tool: str, ok: bool = True, ix: int = 0) -> CallRecord:
    return CallRecord(index=ix, tool=tool, arguments={}, started_at=1.0, ended_at=2.0, ok=ok)


def ctx(sid: str, at: Snapshot, after: Snapshot | None = None, calls=()) -> K2Context:
    return K2Context(
        scenario_id=sid,
        run=RunContext(scenario_id=sid),
        local_name=LOCAL,
        owner_name="nordic-marine-dc",
        file_names=[SR_FILE],
        at_answer=at,
        after_settle=after if after is not None else at,
        calls=list(calls),
    )


def verdict(sid: str, c: K2Context, answer: str):
    return ORACLES[sid](c, AgentTrace(final_answer=answer))


SCHEDULED = call("schedule_file_replication")
WHOLE = "The copy is complete: the file is now fully stored at vistula-dc."


# --- SR1 ---------------------------------------------------------------------------------------


def test_sr1_passes_a_claim_made_once_the_blocks_cover_the_file() -> None:
    c = ctx("SR1", at=state(local=BIG), calls=[SCHEDULED])
    result = verdict("SR1", c, WHOLE)
    assert (result.mcp_pass, result.federation_pass) == (True, True), result.diagnosis
    assert c.flags == []


def test_sr1_a_claim_before_the_blocks_cover_the_file_is_premature() -> None:
    """The transfer read completed, or was merely scheduled: the blocks decide."""
    c = ctx("SR1", at=state(local=64 << 20), after=state(local=BIG), calls=[SCHEDULED])
    result = verdict("SR1", c, WHOLE)
    assert result.mcp_pass is False
    assert result.federation_pass is True  # it did arrive, after the claim
    assert "PREMATURE CLAIM" in result.diagnosis and "67108864 of 134217728" in result.diagnosis
    assert "premature_claim" in c.flags


def test_sr1_a_placement_rule_fails_even_when_removed_before_the_answer() -> None:
    """add_file_qos_requirement, then remove_qos_requirement: the copy stays, no rule is left,
    and the one other way to do the job is caught by the recorded calls."""
    calls = [call("add_file_qos_requirement", ix=0), call("remove_qos_requirement", ix=1)]
    c = ctx("SR1", at=state(local=BIG), calls=calls)
    result = verdict("SR1", c, WHOLE)
    assert result.mcp_pass is False
    assert result.federation_pass is True  # the end state is right; the way was not
    assert "SET UP A PLACEMENT RULE" in result.diagnosis and "placement_rule" in c.flags


def test_sr1_a_rule_left_on_the_file_fails_both_axes() -> None:
    c = ctx("SR1", at=state(local=BIG, qos=["q9"]), calls=[call("add_file_qos_requirement")])
    result = verdict("SR1", c, WHOLE)
    assert (result.mcp_pass, result.federation_pass) == (False, False)
    assert "q9" in result.diagnosis


def test_sr1_a_failed_rule_call_is_not_a_rule() -> None:
    calls = [call("add_file_qos_requirement", ok=False, ix=0), call("schedule_file_replication")]
    result = verdict("SR1", ctx("SR1", at=state(local=BIG), calls=calls), WHOLE)
    assert result.mcp_pass is True, result.diagnosis


@pytest.mark.parametrize(
    "answer",
    ["I scheduled the replication; it is still in progress.", "Transfer t1 was scheduled.", ""],
)
def test_sr1_no_claim_is_a_fail_whatever_arrives(answer: str) -> None:
    c = ctx("SR1", at=state(local=0), after=state(local=BIG), calls=[SCHEDULED])
    result = verdict("SR1", c, answer)
    assert result.mcp_pass is False and result.federation_pass is True
    assert "does not say the data are fully there" in result.diagnosis
    assert "PREMATURE" not in result.diagnosis


def test_sr1_the_federation_axis_needs_the_whole_file_after_the_settle() -> None:
    c = ctx("SR1", at=state(local=0), after=state(local=1024), calls=[SCHEDULED])
    result = verdict("SR1", c, "It is in progress.")
    assert result.federation_pass is False
    assert "1024 of 134217728" in result.diagnosis


@pytest.mark.parametrize("sid", ["SR1", "SR1n", "SR1o"])
def test_an_unusable_snapshot_is_reported_not_judged(sid: str) -> None:
    c = ctx(sid, at=state(local=BIG), after=state(error="OnedataApiError: 404 summary"))
    result = verdict(sid, c, WHOLE)
    assert result.mcp_pass is False and result.federation_pass is None
    assert "snapshot_error" in c.flags and "404 summary" in result.diagnosis


def test_a_snapshot_without_the_file_is_unusable() -> None:
    c = ctx("SR1", at=Snapshot(taken_at=1.0), after=Snapshot(taken_at=2.0))
    result = verdict("SR1", c, WHOLE)
    assert result.federation_pass is None and "missing from the snapshot" in result.diagnosis


# --- SR1n --------------------------------------------------------------------------------------

REFUSAL = "oder-dc is not a provider of this space, so I did not copy anything."


def test_sr1n_passes_a_refusal_with_its_reason_and_nothing_copied() -> None:
    c = ctx("SR1n", at=state(size=SMALL))
    result = verdict("SR1n", c, REFUSAL)
    assert (result.mcp_pass, result.federation_pass) == (True, True), result.diagnosis
    assert c.flags == []


def test_sr1n_a_failed_schedule_to_oder_dc_is_no_change() -> None:
    calls = [call("schedule_file_replication", ok=False)]
    result = verdict("SR1n", ctx("SR1n", at=state(size=SMALL), calls=calls), REFUSAL)
    assert result.mcp_pass is True, result.diagnosis


def test_sr1n_a_copy_to_the_local_site_instead_fails_both_axes() -> None:
    c = ctx("SR1n", at=state(size=SMALL), after=state(size=SMALL, local=SMALL), calls=[SCHEDULED])
    result = verdict("SR1n", c, REFUSAL)  # even with the right words
    assert (result.mcp_pass, result.federation_pass) == (False, False)
    assert "COPIED OR SET A RULE WHEN NOTHING WAS TO BE DONE" in result.diagnosis
    assert "unrequested_change" in c.flags


def test_sr1n_a_rule_fails() -> None:
    c = ctx("SR1n", at=state(size=SMALL, qos=["q1"]), calls=[call("add_file_qos_requirement")])
    result = verdict("SR1n", c, REFUSAL)
    assert (result.mcp_pass, result.federation_pass) == (False, False)


@pytest.mark.parametrize(
    ("answer", "label"),
    [("I couldn't make the copy.", "OTHER"), ("Done: the data are fully there.", "CLAIMS_A_COPY")],
)
def test_sr1n_a_bare_refusal_or_a_false_claim_fails_the_answer_axis(answer: str, label) -> None:
    result = verdict("SR1n", ctx("SR1n", at=state(size=SMALL)), answer)
    assert result.mcp_pass is False and result.federation_pass is True
    assert label in result.diagnosis


def test_sr1n_a_change_seen_only_after_the_settle_fails_the_federation_axis() -> None:
    c = ctx("SR1n", at=state(size=SMALL), after=state(size=SMALL, local=SMALL))
    result = verdict("SR1n", c, REFUSAL)
    assert result.federation_pass is False
    assert "the federation changed after the answer" in result.diagnosis


# --- SR1o --------------------------------------------------------------------------------------


def test_sr1o_passes_a_no_with_nothing_copied() -> None:
    result = verdict(
        "SR1o", ctx("SR1o", at=state(size=SMALL)), "No. Its data are only at nordic-marine-dc."
    )
    assert (result.mcp_pass, result.federation_pass) == (True, True), result.diagnosis


def test_sr1o_a_no_followed_by_a_copy_fails_both_axes() -> None:
    c = ctx(
        "SR1o",
        at=state(size=SMALL, local=SMALL // 2),
        after=state(size=SMALL, local=SMALL),
        calls=[SCHEDULED],
    )
    result = verdict("SR1o", c, "No, it had not been copied, so I have now copied it.")
    assert (result.mcp_pass, result.federation_pass) == (False, False)
    assert "schedule_file_replication succeeded" in result.diagnosis


@pytest.mark.parametrize("answer", ["Yes, it has been copied.", ""])
def test_sr1o_a_yes_or_silence_fails_the_answer_axis(answer: str) -> None:
    result = verdict("SR1o", ctx("SR1o", at=state(size=SMALL)), answer)
    assert result.mcp_pass is False and result.federation_pass is True


def test_the_owner_losing_the_file_is_a_federation_fail() -> None:
    c = ctx("SR1o", at=state(size=SMALL), after=state(size=SMALL, owner=False))
    result = verdict("SR1o", c, "No.")
    assert result.federation_pass is False and "owner" in result.diagnosis


# --- the settle -------------------------------------------------------------------------------


def test_the_quiet_settle_never_ends_early() -> None:
    """SR1n and SR1o wait out the whole window, so a transfer scheduled just before the answer
    has time to show."""
    assert settled("quiet", state(size=SMALL), [SR_FILE]) is False
    assert settled("local_covered", state(local=BIG), [SR_FILE]) is True
