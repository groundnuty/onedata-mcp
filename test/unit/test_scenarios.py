"""Drift / sanity tests for the 18-task PPAM 2026 scenario set."""

from __future__ import annotations

from collections import Counter

from benchmark.scenarios import SCENARIOS


def test_exactly_18_scenarios() -> None:
    assert len(SCENARIOS) == 18


def test_each_band_has_six_scenarios() -> None:
    counts = Counter(s.band for s in SCENARIOS)
    assert counts["discovery"] == 6
    assert counts["access"] == 6
    assert counts["placement"] == 6


def test_oracle_tier_distribution_matches_paper_table_3() -> None:
    """8 format / 8 static / 2 dynamic per paper Table 3."""
    counts = Counter(s.oracle_tier for s in SCENARIOS)
    assert counts["format"] == 8
    assert counts["static"] == 8
    assert counts["dynamic"] == 2


def test_scenario_ids_are_unique_and_match_paper_naming() -> None:
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"
    expected = {f"{b}{i}" for b in "DAP" for i in range(1, 7)}
    assert set(ids) == expected


def test_only_p3_and_p4_are_dynamic_tier() -> None:
    """Per paper Table 3 the dynamic-tier oracles are exactly P3 (replication
    polling) and P4 (most-recent migration). Lock that down so future
    scenario edits can't silently drift the tier mix."""
    dynamic_ids = sorted(s.id for s in SCENARIOS if s.oracle_tier == "dynamic")
    assert dynamic_ids == ["P3", "P4"]


def test_required_tools_subset_of_minimal_allowlist() -> None:
    """Every required tool must be exposed in that scenario's minimal mode —
    otherwise the scenario can't be solved at minimal context."""
    for s in SCENARIOS:
        missing = s.required_tools - s.allowed_tools_minimal
        assert not missing, (
            f"{s.id}: required_tools not in allowed_tools_minimal: {sorted(missing)}"
        )


def test_every_scenario_has_oracle_check_text() -> None:
    """Oracle implementations live in the next workstream (#21) — but the
    *check description* must be authored alongside the scenario brief so
    the ground truth is unambiguous before any model touches it."""
    for s in SCENARIOS:
        assert s.oracle_check.strip(), f"{s.id}: empty oracle_check"


def test_fixture_paths_anchored_in_benchmark_space() -> None:
    """Every fixture path must live under /ppam_2026_mcp_tests/<id>/ —
    keeps scenarios isolated for federation reset."""
    for s in SCENARIOS:
        for f in s.fixture.files:
            assert f.path.startswith(f"/ppam_2026_mcp_tests/{s.id.lower()}/"), (
                f"{s.id} fixture path escapes per-scenario subdir: {f.path}"
            )


def test_static_and_dynamic_tiers_have_oracle_check_state_assertion_keywords() -> None:
    """Sanity: static/dynamic oracles inspect federation state via REST
    side-channel, so their oracle_check should reference that. Catches
    the trap of describing a static oracle as if it were a format parser."""
    for s in SCENARIOS:
        if s.oracle_tier in ("static", "dynamic"):
            assert any(
                kw in s.oracle_check
                for kw in ("REST", "side-channel", "federation", "polling", "Within")
            ), (
                f"{s.id} ({s.oracle_tier}): oracle_check should describe "
                f"federation-state inspection. Got: {s.oracle_check[:80]!r}"
            )
