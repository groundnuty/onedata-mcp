"""Runtime types passed between the fixture runner, the agent harness,
and the oracle dispatcher. See `design/05-federation-reset-protocol.md`
for the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Time-cap defaults from paper §4¶5; will be tightened post the dbsync
# calibration sweep (TODO marker in paper.tex line 252).
RESET_SOFT_CAP_SECONDS = 60.0
RESET_HARD_CAP_SECONDS = 120.0
CONVERGENCE_POLL_INTERVAL = 5.0


class FixtureResetTimeout(RuntimeError):
    """Raised when convergence does not complete within RESET_HARD_CAP_SECONDS.
    The harness handles this by marking the trial RESET_FAIL and re-queueing.
    """


@dataclass(frozen=True)
class RunContext:
    """Per-trial state passed from fixture_runner → harness → oracle.

    The agent never sees this. The oracle uses it to know what was set up,
    what fileIds materialised to, and (for P4) which transferId is the
    expected ground truth.
    """

    scenario_id: str
    # logical_path → fileId mapping captured during materialisation.
    # Empty for scenarios with no fixture files (D1, D4, D6).
    fixture_paths: dict[str, str] = field(default_factory=dict)
    # P4 only: the transferId captured by the fixture runner during
    # pre-staging. None for every other scenario.
    captured_transfer_id: str | None = None
    # Wall-clock epochs. fixture_started_at is when the runner started
    # phase 1 (wipe); fixture_ready_at is when phase 4 (convergence) declared
    # ready. Dynamic oracles use ready_at as the deadline anchor.
    fixture_started_at: float = 0.0
    fixture_ready_at: float = 0.0
    # Snapshot of the user's visible spaces at fixture-prepare time, so
    # D1 / D4 / D6 oracles compare against a stable ground truth instead
    # of re-querying (the federation can churn between the agent's call
    # and the oracle's call). See research/empirical-findings #18.
    spaces_snapshot: tuple[dict, ...] = ()


@dataclass(frozen=True)
class AgentTrace:
    """What the agent did during the trial — the harness captures this and
    hands it to the oracle alongside the agent's natural-language answer.

    Tool-call list lets format-tier oracles cross-check that the agent
    actually invoked the required tools (catches answer-from-thin-air).
    """

    final_answer: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation logged by the harness.

    `result` is the tool's response content (truncated, utf-8 string),
    captured so oracles can replay the agent's actual view of federation
    state without re-querying. See research/empirical-findings #18 for
    why this matters (oracle race window).
    """

    tool_name: str
    arguments: dict
    succeeded: bool
    error: str | None = None
    result: str | None = None


@dataclass(frozen=True)
class OracleResult:
    """Two-axis trial outcome — see design/06-oracle-philosophy.md.

    `mcp_pass`: did the agent issue the right MCP call sequence (and, for
        format-tier scenarios, return the right answer)? This is the axis
        pass^k aggregates over (we measure agent capability).
    `federation_pass`: did the federation post-state match the expected
        outcome? `None` for format-tier scenarios where there's no separate
        federation check. The paper §7 *Threats* narrative consumes the
        `mcp_pass=True ∧ federation_pass=False` rate as an Onedata-side
        divergence signal.
    `diagnosis`: short human-readable summary, especially when the axes
        disagree. Surfaces in trial logs and the per-scenario CSV.
    """

    mcp_pass: bool
    federation_pass: bool | None
    diagnosis: str = ""

    @classmethod
    def format_only(cls, mcp_pass: bool, diagnosis: str = "") -> OracleResult:
        """Constructor for format-tier oracles: federation_pass not applicable."""
        return cls(mcp_pass=mcp_pass, federation_pass=None, diagnosis=diagnosis)
