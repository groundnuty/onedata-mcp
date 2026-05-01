"""Dataclasses for the PPAM 2026 benchmark scenario set.

Scenario data is purely declarative — no oracle implementation lives here.
The materialiser (`benchmark/fixtures_runner.py`, task #21) reads these
dataclasses to set up federation pre-state before each trial; the oracle
runner reads `oracle_check` to dispatch the right verifier.

Scenario ids follow paper Table 3:
- D1..D6  Discovery band       (read-only metadata queries)
- A1..A6  Multi-step Access    (chained read/write workflows)
- P1..P6  Placement-introspection (QoS / distribution reasoning)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Band = Literal["discovery", "access", "placement"]
OracleTier = Literal["format", "static", "dynamic"]


@dataclass(frozen=True)
class FileFixture:
    """A file to materialise in the benchmark space before a trial.

    `path` is logical (e.g. '/ppam_2026_mcp_tests/datasets/raw/sample01.txt').
    `content` is text — the runner will utf-8 encode. Binary fixtures aren't
    needed for any of the 18 PPAM scenarios.
    `json_metadata`, if set, is applied via set_file_metadata after create.
    `qos_expressions` is a tuple of (expression, replicas_num) pairs;
    each is materialised via add_file_qos_requirement.
    """

    path: str
    content: str = ""
    json_metadata: dict | None = None
    qos_expressions: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class TransferFixtureHint:
    """Pre-staged transfer activity for P4 (most-recent-migration).

    The materialiser triggers a migration by adding a temporary QoS rule
    that forces the file to a specific provider, waits for the transfer to
    appear in `list_space_transfers`, captures the transferId, and releases
    the rule. The oracle then asserts the agent identifies that transferId.
    """

    src_path: str
    target_provider_name: Literal["cloud-pl", "Cloud-SK"]
    transfer_type: Literal["replication", "migration", "eviction"] = "migration"


@dataclass(frozen=True)
class Fixture:
    files: tuple[FileFixture, ...] = ()
    transfers: tuple[TransferFixtureHint, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class Scenario:
    id: str
    band: Band
    brief: str
    oracle_tier: OracleTier
    # required_tools: tools the agent MUST invoke (the harness logs tool
    # calls and the oracle can cross-check completeness if needed).
    required_tools: frozenset[str]
    # allowed_tools_minimal: tools exposed to the agent under
    # tool_context_mode='minimal' for this scenario. Subset of
    # benchmark.tool_allowlist.HEADLINE.
    allowed_tools_minimal: frozenset[str]
    fixture: Fixture = field(default_factory=Fixture)
    # oracle_check: human-readable description of what success looks like.
    # Authoritative; the oracle implementation in task #21 must match this.
    oracle_check: str = ""
