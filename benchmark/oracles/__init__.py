"""Per-scenario oracle dispatch.

Each oracle takes (RunContext, AgentTrace) and returns OracleResult: a
two-axis judgement of (mcp_pass, federation_pass, diagnosis) per design/06.
The pass^k aggregator (#22) reduces over `mcp_pass`; the paper §7 Threats
narrative consumes `mcp_pass=True ∧ federation_pass=False` rates.

Format-tier oracles parse the agent's final_answer; federation_pass is None.
Static-tier oracles inspect federation post-state via the REST side-channel.
Dynamic-tier oracles poll federation state with deadlines.

Implementation conventions:
- Each verifier is async (federation reads are async).
- Verifiers MUST NOT call write tools — the harness verifies via tool-call
  log that the oracle's REST side-channel only made GETs.
- Verifiers should never raise on agent error; they return an OracleResult
  with mcp_pass=False and a diagnostic string.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext

# Each oracle has the same signature for uniform dispatch.
Oracle = Callable[[RunContext, AgentTrace], Awaitable[OracleResult]]


def get_oracle(scenario_id: str) -> Oracle:
    """Look up the oracle for a given scenario id. Raises KeyError if missing."""
    from benchmark.oracles import access, discovery, placement

    table: dict[str, Oracle] = {
        "D1": discovery.verify_d1,
        "D2": discovery.verify_d2,
        "D3": discovery.verify_d3,
        "D4": discovery.verify_d4,
        "D5": discovery.verify_d5,
        "D6": discovery.verify_d6,
        "A1": access.verify_a1,
        "A2": access.verify_a2,
        "A3": access.verify_a3,
        "A4": access.verify_a4,
        "A5": access.verify_a5,
        "A6": access.verify_a6,
        "P1": placement.verify_p1,
        "P2": placement.verify_p2,
        "P3": placement.verify_p3,
        "P4": placement.verify_p4,
        "P5": placement.verify_p5,
        "P6": placement.verify_p6,
    }
    return table[scenario_id]


def all_scenario_ids() -> frozenset[str]:
    """Convenience for tests."""
    return frozenset(
        {
            "D1",
            "D2",
            "D3",
            "D4",
            "D5",
            "D6",
            "A1",
            "A2",
            "A3",
            "A4",
            "A5",
            "A6",
            "P1",
            "P2",
            "P3",
            "P4",
            "P5",
            "P6",
        }
    )
