"""Specialise a scenario for a per-LLM space.

The scenarios in `benchmark/scenarios.py` were authored against the
shared `ppam_2026_mcp_tests` space. Per-LLM-space architecture
(2026-05-02) gives each panel LLM its own dedicated space; the harness
rewrites paths + brief at trial dispatch time so the agent's view of
the world is consistently in its own space.

Why string substitution rather than templates: scenarios.py uses
f-strings (already evaluated at module load), so the brief is a
literal string by the time we see it. A frozen-dataclass
`dataclasses.replace`-with-substitution gives us a new Scenario per
trial without disturbing the canonical scenario definitions.

Substitution scope:
- `Scenario.brief`             — visible to the agent
- `FileFixture.path` everywhere — affects fixture_runner's wipe + create
- `TransferFixtureHint.src_path` (P4 pre-stage)
- `Scenario.oracle_check`       — descriptive only; substituted for
                                   consistency in trial logs
"""

from __future__ import annotations

import dataclasses

from benchmark._scenario_types import (
    FileFixture,
    Fixture,
    Scenario,
    TransferFixtureHint,
)

# The string scenarios.py was authored against. Any occurrence in
# briefs, fixture paths, oracle_check is rewritten to the per-LLM
# space at trial dispatch time.
DEFAULT_SPACE = "ppam_2026_mcp_tests"


def _sub(value: str, new_space: str) -> str:
    if not isinstance(value, str):
        return value
    return value.replace(DEFAULT_SPACE, new_space)


def specialise_for_space(scenario: Scenario, space_name: str) -> Scenario:
    """Return a copy of `scenario` with all references to the default
    benchmark space rewritten to `space_name`.

    Idempotent: if `space_name == DEFAULT_SPACE`, returns the input
    unchanged.
    """
    if space_name == DEFAULT_SPACE:
        return scenario

    new_files = tuple(
        FileFixture(
            path=_sub(f.path, space_name),
            content=f.content,
            json_metadata=f.json_metadata,
            qos_expressions=f.qos_expressions,
        )
        for f in scenario.fixture.files
    )
    new_transfers = tuple(
        TransferFixtureHint(
            src_path=_sub(t.src_path, space_name),
            target_provider_name=t.target_provider_name,
            transfer_type=t.transfer_type,
        )
        for t in scenario.fixture.transfers
    )
    new_fixture = Fixture(
        files=new_files,
        transfers=new_transfers,
        notes=scenario.fixture.notes,
    )

    return dataclasses.replace(
        scenario,
        brief=_sub(scenario.brief, space_name),
        fixture=new_fixture,
        oracle_check=_sub(scenario.oracle_check, space_name),
    )
