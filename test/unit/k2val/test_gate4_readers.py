"""Regression cases from the gate-4 runs of the four promoted tools (design/12–15, 2026-11).

Each answer below is a real final answer from those runs (a long file or provider id is
shortened to `<id>`, and a space name to `omcp-x`, which no reader looks at); the records
(design/12–15, "Results") name the config and trial of each. The readers misread them: the
judge read them rightly, and the trials went to NEEDS_REVIEW. Every group has paired
negatives, so that a fix does not open the opposite misreading (an invented name must still
fail, a copy claimed with a reason must still be a copy).

The last part covers SR1's and SR1i's read rule: a `get_file_attributes` answer that carries the
local replica's state (`isFullyReplicatedLocally`, `localReplicationRate`) reads the target's
blocks, because the target is the provider the server queries.
"""

# ruff: noqa: E501

from __future__ import annotations

import dataclasses
import json

import pytest

from benchmark.k2val.file_oracles import fa_label, log_names_claimed, names_claimed
from benchmark.k2val.file_scenarios import FA_NAMES, LC_DIRS
from benchmark.k2val.sr_oracles import read_copied_question, read_copy_answer, read_refusal
from test.unit.k2val.test_sr_oracles import (
    BIG,
    SCHEDULED,
    SR_ID,
    SR_PATH,
    WHOLE,
    call,
    ctx,
    state,
    unread,
    verdict,
)

SYSTEM = ("sp", "logs-x", "vistula-dc", "nordic-marine-dc")

# --- SR1n: a refusal with its reason ------------------------------------------------------------

SR1N_NOT_POSSIBLE = [
    # SR1n #1: "isn't one of them" is the reason
    "I didn't make the copy. The space `omcp-x` is supported by only two providers: `local` (this site) and `owner`. `oder-dc` isn't one of them, so a replication to it would fail. The file itself is fine: `/omcp-x/logs-x/log_20200110.dat` is a regular file of 262144 bytes (256 KiB).\n\nTo copy it to `oder-dc`, the space would first need to be supported there, which means a federation admin or the space owner has to add that provider. Once it is, I can schedule the replication and check the target's blocks until they cover the whole file. Alternatively, if you meant a different provider, give me its provider ID or the name from the list above (`owner`).",
    # SR1n #7: "Once that's done, I can schedule" is a condition, not a copy
    "I didn't make the copy. The site `oder-dc` does not support the space `omcp-x`, so there is nowhere to replicate the file to.\n\nThe space's providers are:\n- `local` (<id>)\n- `owner` (<id>)\n\nThe file itself is intact: `/omcp-x/logs-x/log_20200110.dat` is a regular file of 256 KiB and is not yet fully replicated locally.\n\nTo make the copy, `oder-dc` would first need to be added as a provider of this space, which a space administrator has to do. Once that's done, I can schedule the replication and check the target's blocks until they cover the whole file. I did not set any QoS rule, as you asked.",
    # SR1n #3 (the control): "I'll then check ... before I tell you the copy is
    # complete" is one future clause
    "I haven't copied anything yet, because no provider called `oder-dc` supports the `omcp-x` space. The only two providers are:\n\n- `local` (id `<id>`), which is our site. It currently holds none of this file's data.\n- `owner` (id `<id>`). It holds the whole file, 262144 bytes.\n\nIs `oder-dc` a typo for one of these, most likely `local`? Tell me which provider you want and I'll schedule a one-off replication. I won't set up a QoS rule. I'll then check the target's blocks until they cover the full 262144 bytes before I tell you the copy is complete.",
]

SR1N_CLAIMS_A_COPY = [
    # SR1n #0: "I've replicated", with a negation only in the aside
    '\n\nI\'ve replicated the file `/omcp-x/logs-x/log_20200110.dat` to the local provider (assuming "oder-dc" referred to the local site, as no provider named "oder-dc" exists in the space\'s providers).\n\n**Transfer details:**\n- Transfer ID: `<id>`\n- Status: **Completed**\n\nThe data are **fully there** on the local provider.',
    # the reason, then a copy anyway: the copy decides
    "oder-dc isn't one of the space's providers, so I replicated the file to local instead.",
    "We've copied it to vistula-dc, since oder-dc is not a provider of the space.",
    # "then" after a comma still splits off the claim
    "oder-dc is unknown, so I scheduled it to vistula-dc, then the copy was complete.",
]


@pytest.mark.parametrize("answer", SR1N_NOT_POSSIBLE)
def test_sr1n_real_refusals_read_as_not_possible(answer: str) -> None:
    assert read_refusal(answer) == "SAYS_NOT_POSSIBLE"


@pytest.mark.parametrize("answer", SR1N_CLAIMS_A_COPY)
def test_sr1n_a_copy_with_a_reason_or_an_aside_is_still_a_copy(answer: str) -> None:
    assert read_refusal(answer) == "CLAIMS_A_COPY"


# --- SR1o: "fully present, but only at the provider owner" is a no -----------------------------


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        # SR1o #3
        (
            "**No.** The file exists and is fully present, but only at the provider `owner` — not at your site:\n\n- **`owner` provider** (`<id>`): holds the complete file, blocks `[0, 262144]` (256 KiB) on its storage backend.\n- **`local` provider** (`<id>`): supports the space, but its physical size is **0 bytes** with an empty block list — no data of this file has been copied here.\n\nIf you'd like, I can schedule a replication transfer of this file to `local`.",
            "SAYS_NOT_COPIED",
        ),
        ("No. It is fully present only at the provider nordic-marine-dc.", "SAYS_NOT_COPIED"),
        # negatives: a yes without "only" elsewhere, and "I've" as the agent's own copy
        ("It is fully present at your site vistula-dc now.", "SAYS_COPIED"),
        ("No, it was not there, so I've replicated it to vistula-dc.", "SAYS_COPIED"),
    ],
)
def test_sr1o_only_at_another_site_is_not_a_copy_here(answer: str, label: str) -> None:
    assert read_copied_question(answer, "vistula-dc", "nordic-marine-dc") == label


# --- SR1: a copy "scheduled successfully" is not a whole copy -----------------------------------


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        ("The replication was scheduled successfully.", "SAYS_NOT_YET"),
        ("The replication has been initiated successfully with transfer id t1.", "SAYS_NOT_YET"),
        # negatives: a completed copy is still whole
        (
            "The replication has completed successfully; the file is fully at vistula-dc.",
            "SAYS_WHOLE",
        ),
        (
            "The transfer completed successfully and the file is fully stored at vistula-dc.",
            "SAYS_WHOLE",
        ),
    ],
)
def test_sr1_scheduled_successfully_is_not_a_whole_copy(answer: str, label: str) -> None:
    assert read_copy_answer(answer, "vistula-dc", "nordic-marine-dc") == label


# --- SR1 (the K=8 re-run): "no ongoing ..." says nothing is under way -----------------------------
#
# Two whole claims, each read from the blocks after the schedule, were read OTHER: the "ongoing" of
# "No ongoing action is needed" and of "there's no ongoing placement requirement" counted as a
# copy still under way (design/12, the re-run's SR1 #7 and #3). The site is `local` there.

SR1_RERUN_WHOLE = [
    "Done. The copy is **fully in place now** — the data are already there.\n\n- **What was done:** a one-off replication transfer (`<id>`) of `/omcp-x/logs-x/log_20200110.dat` to provider `local` (`<id>`). No QoS/placement rule was created.\n- **Verification:** by block distribution (not just transfer status), the `local` provider's storage backend `<id>` holds the entire file — block range `[0, 134217728]`, i.e. all 128 MiB of the 134,217,728-byte file.\n- The original copy at provider `owner` remains untouched.\n\nNo ongoing action is needed; the data are fully present at your local site.",
    "The replication is complete. Let me verify the state:\n\n- **Transfer status**: `completed` — 134,217,728 bytes replicated (all 128 MB).\n- **Distribution at `local`**: Now shows `physicalSize: 134217728` with blocks `[[0, 134217728]]` covering the entire file, and `holds_whole_file: true`.\n\nThe file's data is fully present at my site `local`. I did **not** set up any QoS placement rule — this was a one-off replication, so there's no ongoing placement requirement keeping it there.\n\n## Summary\n\nI made a copy of `/omcp-x/logs-x/log_20200110.dat` at my site `local` (provider `<id>`) via a one-off replication transfer (ID `<id>`), without setting up any placement/QoS rule.\n\nThe data was **fully present** at `local` once the transfer completed — the distribution showed `local` holding the entire file (all 134,217,728 bytes, block range `[0, 134217728]`, `holds_whole_file: true`). The transfer reported completion and all 128 MB were replicated successfully.",
    "No pending transfer remains, and the file is fully present at local.",
]

# Paired negatives: a copy under way is still one, beside a "no ongoing ..." or alone.
SR1_RERUN_NOT_YET = [
    "There is no ongoing transfer, but the copy is not complete yet.",
    "No pending action on your side; the replication is still ongoing.",
    "Transfer ongoing: 40 MB of 128 MB are at local so far.",
    "The copy is still pending at local; no queued work is left elsewhere.",
]


@pytest.mark.parametrize("answer", SR1_RERUN_WHOLE)
def test_sr1_no_ongoing_is_not_a_copy_under_way(answer: str) -> None:
    assert read_copy_answer(answer, "local", "owner") == "SAYS_WHOLE"


@pytest.mark.parametrize("answer", SR1_RERUN_NOT_YET)
def test_sr1_a_copy_under_way_is_still_not_yet_beside_no_ongoing(answer: str) -> None:
    assert read_copy_answer(answer, "local", "owner") == "SAYS_NOT_YET"


# --- FA, FAi, FAn ------------------------------------------------------------------------------

FA_REAL = [
    # FAi #0 (the expected file is log_20200102.dat there; the reader is the same)
    (
        "The file with different permissions is **log_20200102.dat**, which has mode 600. The other three (log_20200101.dat, log_20200103.dat, and log_20200104.dat) all have mode 644.",
        "log_20200102.dat",
    ),
    # FA #0
    (
        "**log_20200103.dat** — it has permissions `600`, while the other three files (log_20200101.dat, log_20200102.dat, log_20200104.dat) all have `644`.",
        "log_20200103.dat",
    ),
    # FAn #1, #4
    (
        "None of them differ. All four files (log_20200101.dat, log_20200102.dat, log_20200103.dat, log_20200104.dat) have POSIX permissions 644.",
        "none",
    ),
    (
        "None of them differ: log_20200101.dat, log_20200102.dat, log_20200103.dat, and log_20200104.dat all have posix permissions 644.",
        "none",
    ),
]

FA_NEGATIVES = [
    # the others "all have" the SAME mode as the named file: no odd one is named
    (
        "log_20200103.dat has 644, while the other three files (log_20200101.dat, log_20200102.dat, log_20200104.dat) all have 644 too.",
        "log_20200103.dat",
    ),
]


@pytest.mark.parametrize(("answer", "label"), FA_REAL)
def test_fa_real_answers(answer: str, label: str) -> None:
    assert fa_label(answer, FA_NAMES) == label


@pytest.mark.parametrize(("answer", "not_label"), FA_NEGATIVES)
def test_fa_a_contrast_with_the_same_mode_names_no_odd_file(answer: str, not_label: str) -> None:
    assert fa_label(answer, FA_NAMES) != not_label


# --- LC, LCn, LCp, LCi: words, extensions and patterns are not names -------------------------

LC_REAL = [
    # LCn #4: "Therefore"
    (
        "The directory `/sp/logs-x` contains only two regular files (`log_20200101.dat` and `log_20200102.dat`) and no subdirectories. Therefore, it has no subdirectories.",
        frozenset(),
    ),
    # LCp #3: "Thus", and the files' `.dat`
    (
        "The specified directory `/sp/logs-x` does not contain any subdirectories. It only includes regular files with the extensions `.dat`. The names of these files are:\n\n- acq_0001.dat\n- acq_0002.dat\n\nThus, there are no subdirectories.",
        frozenset(),
    ),
    # LCi #6: "e.g."
    (
        "The directory contains **3 subdirectories**:\n\n- `calib_runs`\n- `raw_frames`\n- `spare_slots`\n\n(The directory also holds some regular files, e.g. `log_20200101.dat` and `log_20200102.dat`, but these are not subdirectories.)",
        frozenset(LC_DIRS),
    ),
    # LCi #7: "Additionally"
    (
        "The directory with ID `<id>` (which corresponds to `/sp/logs-x`) has the following **3 subdirectories**:\n\n1. **calib_runs**\n2. **raw_frames**\n3. **spare_slots**\n\nAdditionally, it contains two regular files (`log_20200101.dat` and `log_20200102.dat`).",
        frozenset(LC_DIRS),
    ),
    # LCp #1: "`.dat` files"
    (
        "The directory `/sp/logs-x` has the following **3 subdirectories**:\n\n1. **calib_runs**\n2. **raw_frames**\n3. **spare_slots**\n\nAll other entries in this directory are regular files (`.dat` files).",
        frozenset(LC_DIRS),
    ),
    # LCp #2: "`true`"
    (
        "I found the subdirectories. Let me verify there are no more pages to check - the `isLast` was `true` on this page, so all entries have been listed.\n\nThe subdirectories of `/sp/logs-x` are:\n\n1. **`calib_runs`**\n2. **`raw_frames`**\n3. **`spare_slots`**",
        frozenset(LC_DIRS),
    ),
    # LCp #3: "`acq_*.dat`"
    (
        "The directory `/sp/logs-x` has three subdirectories:\n\n- `calib_runs`\n- `raw_frames`\n- `spare_slots`\n\nI paged through all 113 entries to find them; the rest are 110 `acq_*.dat` files.",
        frozenset(LC_DIRS),
    ),
]

LC_NEGATIVES = [
    # the review's finding 3 still holds: an invented name is kept
    (
        "Therefore, the subdirectories are: `calib_runs`, `raw_frames`, `spare_slots`, `notes`.",
        frozenset({*LC_DIRS, "notes"}),
    ),
    (
        "Subdirectories:\n- calib_runs\n- raw_frames\n- spare_slots\n- `.cache`",
        frozenset({*LC_DIRS, "cache"}),
    ),
    (
        "Additionally, there is `archive_2020` next to calib_runs, raw_frames and spare_slots.",
        frozenset({*LC_DIRS, "archive_2020"}),
    ),
]


@pytest.mark.parametrize(("answer", "names"), LC_REAL + LC_NEGATIVES)
def test_names_claimed_real_answers_and_invented_names(answer: str, names) -> None:
    assert names_claimed(answer, LC_DIRS, SYSTEM) == names


LOGS = {"log_20200101.dat", "log_20200102.dat", "log_20200103.dat"}


@pytest.mark.parametrize(
    ("answer", "names"),
    [
        # LCo #5 and #1: a naming pattern is not a file
        (
            "The directory `/sp/logs-x` contains 3 log files (including subdirectories):\n\n1. `log_20200101.dat` — directly in `/sp/logs-x`\n2. `log_20200102.dat` — directly in `/sp/logs-x`\n3. `log_20200103.dat` — in the subdirectory `/sp/logs-x/calib_runs`\n\nAll three are regular files of 24 bytes each, named in the `log_YYYYMMDD.dat` pattern.",
            LOGS,
        ),
        (
            "There are **3 log files** in `/sp/logs-x` (all regular files, matching the `log_*.dat` naming):\n\n1. `log_20200101.dat` — in the top-level directory\n2. `log_20200102.dat` — in the top-level directory\n3. `log_20200103.dat` — in the subdirectory `calib_runs/`",
            LOGS,
        ),
        # negatives: bold names are names, an invented file is kept
        ("**log_20200101.dat**, **log_20200102.dat** and **log_20200103.dat**", LOGS),
        (
            "log_20200101.dat, log_20200102.dat, log_20200103.dat and log_20200199.dat",
            {*LOGS, "log_20200199.dat"},
        ),
    ],
)
def test_log_names_claimed_real_answers(answer: str, names) -> None:
    assert log_names_claimed(answer) == frozenset(names)


# --- SR1, SR1i: the local replica's attributes read the target's blocks -------------------------


def attributes(ix: int, target: str, result: dict) -> object:
    return dataclasses.replace(
        call("get_file_attributes", ix=ix, file_id_or_path=target), result_text=json.dumps(result)
    )


@pytest.mark.parametrize("sid", ["SR1", "SR1i"])
@pytest.mark.parametrize("target", [SR_PATH, SR_ID], ids=["by-path", "by-id"])
@pytest.mark.parametrize(
    "result",
    [
        {"localReplicationRate": 1.0, "isFullyReplicatedLocally": True},
        {"isFullyReplicatedLocally": True},
        {"localReplicationRate": 1.0},
    ],
    ids=["both", "fully-replicated", "rate"],
)
def test_a_local_replica_read_after_the_schedule_is_a_read_of_the_blocks(
    sid: str, target: str, result: dict
) -> None:
    """SR1i #5: it polled `localReplicationRate` (0.28, then 1.0) after scheduling."""
    c = ctx(sid, at=state(local=BIG), calls=[SCHEDULED, attributes(1, target, result)])
    outcome = verdict(sid, c, WHOLE)
    assert outcome.mcp_pass is True, outcome.diagnosis
    assert "unread_claim" not in c.flags


@pytest.mark.parametrize("sid", ["SR1", "SR1i"])
def test_attributes_without_the_local_replica_are_not_a_read(sid: str) -> None:
    other = attributes(1, SR_PATH, {"size": BIG, "type": "REG", "name": "log_20200110.dat"})
    c = ctx(sid, at=state(local=BIG), calls=[SCHEDULED, other])
    assert unread(c, verdict(sid, c, WHOLE))


def test_a_local_replica_read_before_the_schedule_is_not_a_read_after_it() -> None:
    before = attributes(
        0, SR_PATH, {"isFullyReplicatedLocally": False, "localReplicationRate": 0.0}
    )
    schedule = call("schedule_file_replication", ix=1, file_id_or_path=SR_PATH)
    c = ctx("SR1", at=state(local=BIG), calls=[before, schedule])
    assert unread(c, verdict("SR1", c, WHOLE))


def test_a_local_replica_read_of_another_file_is_not_a_read() -> None:
    other = attributes(
        1, "/k2val-glm/logs-abcdefghijkl/log_20200101.dat", {"isFullyReplicatedLocally": True}
    )
    c = ctx("SR1", at=state(local=BIG), calls=[SCHEDULED, other])
    assert unread(c, verdict("SR1", c, WHOLE))


def test_a_local_replica_read_does_not_hide_a_premature_claim() -> None:
    """The read rule asks for the read; whether the claim was true is the premature check's."""
    partial = attributes(
        1, SR_PATH, {"isFullyReplicatedLocally": False, "localReplicationRate": 0.4}
    )
    c = ctx("SR1", at=state(local=BIG // 2), calls=[SCHEDULED, partial])
    outcome = verdict("SR1", c, WHOLE)
    assert outcome.mcp_pass is False and "premature_claim" in c.flags
