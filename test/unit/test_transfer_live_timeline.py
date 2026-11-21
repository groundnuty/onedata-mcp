"""The live transfer test's bookkeeping, without a federation: what it reports from its polls.

The live test itself (test/live/test_transfer_live.py) only runs against the local Compose
federation; its reading of the polls is checked here, so a wrong calibration figure is caught
before the first live run.
"""

from __future__ import annotations

from test.live.test_transfer_live import Poll, Timeline, _status


def test_completed_counts_as_before_whole_only_when_its_own_poll_was_not_covered() -> None:
    """Each poll reads the status first, then the block map: completed with the blocks not yet
    whole on the SAME poll means the status certainly came first."""
    timeline = Timeline(
        100,
        [
            Poll(0.5, "transferState=scheduled", 0, False),
            Poll(1.0, "transferState=completed", 40, False),
            Poll(1.5, "transferState=completed", 100, True),
        ],
    )

    assert [p.at for p in timeline.completed_before_whole] == [1.0]
    assert timeline.whole_at == 1.5
    assert [p.at for p in timeline.partial] == [1.0]
    report = "\n".join(timeline.report())
    assert "completed while NOT yet whole on the same poll: YES" in report


def test_completed_and_whole_on_one_poll_is_not_completed_first() -> None:
    """A small file copied before the first poll: one poll, completed and whole."""
    timeline = Timeline(100, [Poll(0.03, "transferState=completed", 100, True)])

    assert timeline.completed_before_whole == []
    assert timeline.first_completed is not None and timeline.whole_at == 0.03
    report = "\n".join(timeline.report())
    assert "completed while NOT yet whole on the same poll: no" in report
    assert "never seen partly copied" in report


def test_transfer_state_is_preferred_over_the_other_status_fields() -> None:
    both = {"replicationStatus": "completed", "transferState": "active"}
    assert _status(both) == "transferState=active"
    assert _status({"replicationStatus": "completed"}) == "replicationStatus=completed"
    assert _status({}) == "no status field"
