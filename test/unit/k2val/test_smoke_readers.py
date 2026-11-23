"""Regression cases from the stable tools' smoke re-runs (design/16, "Results", 2026-11).

Each answer below is a real final answer from those runs, the control's run included (a long rule,
provider or transfer id is replaced by a made-up one of the same shape, and a space name by
`omcp-x`, which no reader looks at); design/16 names the trial of each. The readers misread
them: the judge read them rightly, and the trials went to NEEDS_REVIEW. Every group has paired
negatives, so that a fix does not open the opposite misreading (a rule's pending status with no
data claim is still "not done", and one not explained away beside a data claim is still "both";
a restated question with no "yes" is no verdict; a share after the list is not a site's).
"""

# ruff: noqa: E501

from __future__ import annotations

from types import SimpleNamespace

import pytest

from benchmark.k2val.oracles import read_v3o
from benchmark.k2val.reading import is_right, right_reading
from benchmark.k2val.rubrics import OTHER
from benchmark.k2val.stable_oracles import (
    CAN_ALL,
    CONFLICT,
    SAYS_DONE,
    SAYS_NOT_DONE,
    read_copies,
    read_rule_task,
    read_rules_answer,
    read_shares,
    read_single_copy,
    read_transfer_ids,
)

LOCAL, OWNER = "local", "owner"
SITES = SimpleNamespace(local_name=LOCAL, owner_name=OWNER, dir_id="", cited=())
RULE = "6a1852303547328067ce772fd33172cfch7975"
PROV = "3f0c9e1d2b4a5c6d7e8f90a1b2c3d4e5ch1111"
TID = "9a8b7c6d5e4f30211203f4e5d6c7b8a9ch2222"
NAMES = ("log_20200113.dat", "log_20200114.dat", "log_20200115.dat")

# --- ST1: the rule's status read as pending is not the data's -------------------------------------

ST1_DONE = [
    # ST1 #0 (the control): a pending status narrated, then fulfilled
    f"The placement rule is set up, and the file's data are now fully at your site, `local`.\n\n- **Rule:** QoS requirement `{RULE}` on `/omcp-x/logs-x/log_20200111.dat`. It has the expression `providerId={PROV}` (your site) and asks for 1 replica.\n- **Data:** `local` holds block [0, 262144], which is the whole 262144-byte file. The `owner` provider also still has a full copy.\n- **Status:** The rule showed `pending` on the first check and `fulfilled` on the second.",
    # ST1 #0: the summary "still says pending", the data all there
    f"The file is now fully on your site (`local`), so its data are all there.\n\n- **Rule:** QoS requirement `{RULE}` on `/omcp-x/logs-x/log_20200111.dat`, expression `providerId={PROV}` (local), 1 replica.\n- **Status:** the file reports `isFullyReplicatedLocally: true` with `localReplicationRate: 1`, and the distribution shows all 262,144 bytes at `local`. The QoS summary still says `pending`, which looks like a lag in the status field rather than missing data. It may update on its own.",
    # ST1 #2: "Its status still reads pending", "the data itself is complete"
    f"The data for `/omcp-x/logs-x/log_20200111.dat` is fully at your site (`local`). The file is 262,144 bytes, and it reports `isFullyReplicatedLocally: true` with a local replication rate of 1.\n\nThe placement rule is set: QoS requirement `{RULE}` with expression `providerId={PROV}` (your provider, `local`) and 1 replica. Its status still reads `pending` in the QoS summary, even though the file attributes already show the data present. That status should flip to `fulfilled` shortly; the data itself is complete.",
]

ST1_NOT_DONE = [
    # the status alone, with no data claim, is still "not done", explained or not
    "The placement rule is set up. Its status still reads pending in the QoS summary.",
    "The rule is in place, but the QoS summary still says pending.",
    "The QoS summary still says pending, which looks like a lag.",
    # the status explained away, the rest of the sentence still says not yet
    "The rule is set up; its status is still pending and the data are not yet fully there.",
    "The rule showed pending on the first check; the file is only partially here so far.",
]

ST1_BOTH = [
    # a status not explained away, beside a data claim: the answer says both
    "The data are fully at local, but the rule is still pending.",
    "The file is fully at your site. The rule status is still pending.",
]


@pytest.mark.parametrize("answer", ST1_DONE)
def test_st1_a_pending_rule_status_explained_away_beside_a_data_claim_reads_done(
    answer: str,
) -> None:
    assert read_rule_task(answer, LOCAL, OWNER) == SAYS_DONE


@pytest.mark.parametrize("answer", ST1_NOT_DONE)
def test_st1_a_pending_rule_status_alone_or_with_data_not_there_reads_not_done(answer: str) -> None:
    assert read_rule_task(answer, LOCAL, OWNER) == SAYS_NOT_DONE


@pytest.mark.parametrize("answer", ST1_BOTH)
def test_st1_a_bare_pending_status_beside_a_data_claim_reads_other(answer: str) -> None:
    assert read_rule_task(answer, LOCAL, OWNER) == OTHER


# --- ST1, the second run: more ways to explain a pending status, and a done word about the future --

ST1_DONE_RUN2 = [
    # run 2, ST1 #0: the status sentence is explained by the next one ("I expect it to flip")
    f"The placement rule is set, and the data are already fully at your site (`local`), but the rule itself is still reporting `pending`.\n\n- **Rule:** `providerId={PROV}` with 1 replica, ID `{RULE}`.\n- **Data:** the file is 262,144 bytes. The distribution shows `local` holding blocks `[0, 262144]`, which is the whole file. The owner provider also holds the whole file.\n- **Status:** the QoS summary still says `pending` on two checks. I expect it to flip to `fulfilled` shortly, since the data are already there. I didn't wait for that.",
    # run 2, ST1 #1: "still lists the requirement as pending, so … may update shortly"
    f"The placement rule is in place, and the file's data are now fully at your site (`local`, provider `{PROV}`).\n\n- **Rule:** QoS requirement `{RULE}` with expression `providerId={PROV}` and 1 replica.\n- **Status:** The file reports `isFullyReplicatedLocally: true` and `localReplicationRate: 1` (262,144 bytes). The QoS summary still lists the requirement as `pending`, so the rule's own status may update shortly.",
    # run 2, ST1 #2: "so the rule's status may not have updated yet"
    f"The data for `/omcp-x/logs-x/log_20200111.dat` are now fully at your site (`local`).\n\n- **Rule added:** `providerId={PROV}` with 1 replica (ID `{RULE}`).\n- **Verified:** the local provider holds the whole file: block range [0, 262144] of the 262,144-byte file, and `isFullyReplicatedLocally` is true.\n- **Caveat:** the QoS summary still reports the requirement as `pending`, so the rule's status may not have updated yet. The data check above is what confirms the copy is complete.",
    # a done claim in the past beside "when" is still a claim
    "When I checked, the data were fully at your site.",
    "The data were fully there when I checked.",
]

ST1_NOT_DONE_RUN2 = [
    # run 2, ST1 #0 (the other model): "pending … hasn't completed yet", and "fulfilled" only in
    # "to verify when it reaches fulfilled status"
    f"Based on my analysis, I have successfully set up a placement rule that keeps the file `/omcp-x/logs-x/log_20200111.dat` at your local site. Here's what I did:\n\n1. Added a QoS requirement with the expression `providerId={PROV}` (which identifies your local provider)\n2. Set the replica count to 1 to ensure the file is maintained at your site\n\nThe system shows the requirement is currently in \"pending\" status, meaning the replication process has started but hasn't completed yet. The file is 262,144 bytes in size, and according to the distribution information, it appears that while there is a replica at your local provider, it doesn't yet hold the complete file.\n\nTo confirm when the data are fully replicated, you would need to monitor the file's status. The replication process might take some time depending on network conditions and system load. You can check the status again later using the same commands to verify when it reaches \"fulfilled\" status.",
    # the newly matched status phrasing, with no data claim, is still "not done", explained or not
    "The QoS summary still lists the requirement as pending.",
    "The summary still lists the requirement as pending, so it may not have updated yet.",
    "The rule is still pending. I expect it to flip to fulfilled once the copy ends.",
]

ST1_OTHER_RUN2 = [
    # run 2, ST1 #2 (the other model): "To check when the data are fully replicated … I'll need to
    # monitor" was read as done; it says neither (the judge: not done; FAIL either way)
    f"I've successfully set up a placement rule for the file `/omcp-x/logs-x/log_20200111.dat` to keep it at your site (provider ID: {PROV}). \n\nThe QoS requirement has been created with ID: {RULE}\n\nTo check when the data are fully replicated and available at your site, I'll need to monitor the QoS status. The system will automatically replicate the file to your provider until the requirement is fulfilled. You can check the status using the QoS summary for this file.\n\nWould you like me to check the current status of this requirement to see if it's already fulfilled?",
    "Once the rule is fulfilled, the data are fully there.",
    # a next sentence that does not say the status will change explains nothing: both
    "The file is fully at your site. The rule status is still pending. I did not wait for it.",
    # nor does a status said NOT to change
    "The data are fully at local, but the rule is still pending and will not change.",
    "The data are fully at local, but the rule is still pending and may not update.",
]


@pytest.mark.parametrize("answer", ST1_DONE_RUN2)
def test_st1_run2_an_explained_status_or_a_past_when_beside_a_data_claim_reads_done(
    answer: str,
) -> None:
    assert read_rule_task(answer, LOCAL, OWNER) == SAYS_DONE


@pytest.mark.parametrize("answer", ST1_NOT_DONE_RUN2)
def test_st1_run2_a_status_with_no_data_claim_or_a_future_fulfilled_reads_not_done(
    answer: str,
) -> None:
    assert read_rule_task(answer, LOCAL, OWNER) == SAYS_NOT_DONE


@pytest.mark.parametrize("answer", ST1_OTHER_RUN2)
def test_st1_run2_a_future_done_word_or_an_unexplained_status_is_not_done(answer: str) -> None:
    assert read_rule_task(answer, LOCAL, OWNER) == OTHER


# --- ST3: a numbered item's name, then its count after a dash ----------------------------------

# ST3 #0, #1, #2 (all three alike)
ST3_ITEMS = "Based on the QoS summaries I've checked, I can now identify which files have a placement rule asking for only one copy:\n\n1. `log_20200113.dat` - Has a requirement with replicas_num = 1\n2. `log_20200114.dat` - Has a requirement with replicas_num = 2\n3. `log_20200115.dat` - Has a requirement with replicas_num = 1\n\nThe files that have a placement rule asking for only one copy are:\n- log_20200113.dat\n- log_20200115.dat"


@pytest.mark.parametrize(
    ("answer", "names"),
    [
        (ST3_ITEMS, {"log_20200113.dat", "log_20200115.dat"}),
        # the items alone decide, by their own counts
        (
            "1. log_20200113.dat - replicas_num = 1\n2. log_20200114.dat - replicas_num = 2\n3. log_20200115.dat - replicas_num = 2",
            {"log_20200113.dat"},
        ),
        # an item with no count after its name is left to the clauses: its count elsewhere decides
        (
            "1. log_20200114.dat - checked\n2. log_20200113.dat - checked\nlog_20200114.dat asks for two copies; log_20200113.dat asks for one copy.",
            {"log_20200113.dat"},
        ),
        # a clause about the other files is not carried to the one the line names
        (
            "log_20200113.dat asks for one copy; the other two ask for two copies.",
            {"log_20200113.dat"},
        ),
        # an item whose text holds both counts is left to the clauses
        (
            "1. log_20200114.dat - asks for two copies, not one\n2. log_20200115.dat - asks for one copy",
            {"log_20200115.dat"},
        ),
    ],
)
def test_st3_an_items_count_after_a_dash_belongs_to_its_name(answer: str, names) -> None:
    assert read_single_copy(answer, NAMES) == frozenset(names)


# --- ST4: a labelled provider id, and the conclusion on the line before the id ------------------

# ST4 #0
ST4_CONCLUDED = f'The transfer ID `{TID}` is for the file `/omcp-x/logs-x/log_20200116.dat`, which matches the file we are looking for. The replicating provider ID is `{PROV}`. \n\nNow I need to verify that this is indeed the transfer to the local site. However, since the problem states "to your site local", I assume that the local site is the one identified by the provider ID `{PROV}`. If needed, I can double-check the provider information, but based on the context, this seems to be the correct transfer ID.\n\nTherefore, the ID of the transfer that copied the file to the local site is:\n\n`{TID}`'
OTHER_TID = "1b2c3d4e5f60718293a4b5c6d7e8f901ch3333"


@pytest.mark.parametrize(
    ("answer", "ids"),
    [
        (ST4_CONCLUDED, {TID}),
        # "provider ID is" labels the provider's id; the transfer's stays
        (f"Transfer {TID} replicated it. The replicating provider ID is {PROV}.", {TID}),
        # "the file is <id>" has no id word: not a label
        (f"The id of the transfer that copied the file is {TID}.", {TID}),
        # ids on lines of their own with no conclusion before the last: all of them
        (f"I found two transfers:\n\n{OTHER_TID}\n\n{TID}", {OTHER_TID, TID}),
        # a conclusion before the last id, across a blank line
        (f"Candidates: {OTHER_TID}, {TID}.\n\nThe answer is:\n\n{TID}", {TID}),
    ],
)
def test_st4_a_labelled_provider_id_and_a_conclusion_across_a_blank_line(answer: str, ids) -> None:
    assert read_transfer_ids(answer, SITES) == frozenset(ids)


# --- ST5: "Number of Copies Requested: 2", and a restated question answered "Yes" ---------------

# ST5 #1, #2 (alike)
ST5_FIELDS = f"The file `/omcp-x/logs-x/log_20200119.dat` has two placement rules applied:\n\n1. **Rule 1:**\n   - **Expression:** `(providerId={OWNER}|providerId={PROV})`\n   - **Number of Copies Requested:** 2\n   - **Status:** Fulfilled\n\n2. **Rule 2:**\n   - **Expression:** `providerId={PROV}`\n   - **Number of Copies Requested:** 1\n   - **Status:** Fulfilled\n\nBoth rules are currently fulfilled, meaning that the required number of copies can be met at the same time."
# ST5 #0 (the control)
ST5_YES = f"Two QoS rules apply to `/omcp-x/logs-x/log_20200119.dat`:\n\n1. **Rule `acdaf852…7975`** asks for **1 copy**, on provider `local` (`{PROV}`). Its status is fulfilled.\n2. **Rule `90573642…7975`** asks for **2 copies**, on either `owner` or `local`. Its status is fulfilled.\n\n**Can they all be met at once?** Yes. Both rules are reported as fulfilled right now, and the file's overall QoS status is also fulfilled.\n\nThe space is supported by only two providers, `owner` and `local`. Two copies, one on each provider, satisfy both rules together. The `local` copy counts toward both rules, and the `owner` copy supplies the second copy that the 2-copy rule needs.\n\nI didn't check the file's block distribution. The fulfilled status comes from the QoS summary."


@pytest.mark.parametrize(
    ("answer", "read"),
    [
        (ST5_FIELDS, (CAN_ALL, {"1", "2"})),
        (ST5_YES, (CAN_ALL, {"1", "2"})),
        (
            "Rule A, copies required: 2. Rule B, copies required: 1. They do not conflict.",
            (CAN_ALL, {"1", "2"}),
        ),
        # the question restated, answered "No", or not answered: no "can all be met"
        (
            "Rule A asks for 1 copy, rule B for 2 copies. Can they all be met at the same time? No. The two rules conflict.",
            (CONFLICT, {"1", "2"}),
        ),
        (
            "Rule A asks for 1 copy, rule B for 2 copies. Can they all be met at once?",
            (OTHER, {"1", "2"}),
        ),
        # "Yes" after a question that is not about being met is no verdict
        ("Rule A asks for 1 copy, rule B for 2 copies. Did I read both? Yes.", (OTHER, {"1", "2"})),
    ],
)
def test_st5_a_requested_count_field_and_a_restated_question(answer: str, read) -> None:
    label, counts = read_rules_answer(answer)
    assert (label, set(counts)) == (read[0], set(read[1]))


def test_st2_reads_the_requested_count_field_too() -> None:
    assert read_copies("- **Number of Copies Requested:** 2") == "TWO"
    assert read_copies("Replicas requested: 1") == "ONE"


# --- ST6: the share on a sub-item under the item that names its site ----------------------------

# ST6 #0
ST6_ITEMS = "The file `/omcp-x/logs-x/log_20200118.dat` is distributed across two sites:\n\n1. **Site: local**\n   - **Physical Size:** 65,536 bytes\n   - **Percentage of Data:** 25%\n\n2. **Site: owner**\n   - **Physical Size:** 131,072 bytes\n   - **Percentage of Data:** 50%\n\nThe remaining 25% of the data is not explicitly accounted for in the provided distribution, possibly due to other storage backends or locations not detailed in this response."
# ST6 #2
ST6_ITEMS_2 = "The file `/omcp-x/logs-x/log_20200118.dat` is distributed across two sites:\n\n1. **local**:\n   - Physical Size: 65,536 bytes\n   - Percentage of total data: 25%\n\n2. **owner**:\n   - Physical Size: 131,072 bytes\n   - Percentage of total data: 50%\n\nThe remaining 25% of the data is not accounted for in the distribution provided, possibly due to the file being split into blocks or other storage considerations. However, based on the information available, the percentages for the two sites are as stated above."


@pytest.mark.parametrize(
    ("answer", "pairs"),
    [
        (ST6_ITEMS, {"local=25", "owner=50"}),
        (ST6_ITEMS_2, {"local=25", "owner=50"}),
        # a paragraph after the list is not the last site's
        ("1. owner\n   - 50%\n\nThe remaining 25% is held by nobody.", {"owner=50"}),
        # one share per site item: the first
        ("1. local\n   - Share: 25%\n   - Rate: 100%", {"local=25"}),
        # a line naming both sites ends the item
        ("1. local\n- local and owner hold parts\n   - 40%", None),
        # a line that is not a list item ends the item
        ("Site: local\nIt holds 25% of the data.", None),
    ],
)
def test_st6_a_share_under_its_sites_item(answer: str, pairs) -> None:
    got = read_shares(answer, LOCAL, OWNER)
    assert got == (frozenset(pairs) if pairs is not None else None)


def test_st6_and_st7_right_readings_depend_on_the_federations_site_names() -> None:
    assert right_reading("ST6") is None and right_reading("ST7") is None
    assert is_right("ST6", frozenset({"local=25", "owner=50"})) is None
    assert is_right("ST7", frozenset({"local", "owner"})) is None


# --- V3o: "the requirement specifies this provider ... the file is indeed kept at" --------------

V3O_RULE = [
    # V3o #2
    f'The provider associated with the provider ID `{PROV}` is named "local". Since the QoS requirement for the file specifies this provider, it means that the file is indeed kept at the local site.',
    "Because the requirement names your provider, the file is kept at local.",
]
V3O_NOT_RULE = [
    "The requirement targets the owner, so the file is not kept at the local site.",
    "There is no placement requirement; the file is stored at local only because it was written there.",
]


@pytest.mark.parametrize("answer", V3O_RULE)
def test_v3o_a_requirement_then_the_file_kept_at_the_site_says_rule(answer: str) -> None:
    assert read_v3o(answer) == "SAYS_RULE"


@pytest.mark.parametrize("answer", V3O_NOT_RULE)
def test_v3o_a_denied_keeping_or_no_requirement_is_not_rule(answer: str) -> None:
    assert read_v3o(answer) != "SAYS_RULE"
