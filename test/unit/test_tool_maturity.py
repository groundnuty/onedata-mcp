"""Tests for the per-tool maturity classification + launch-time tool selection."""

from __future__ import annotations

import asyncio

import pytest

from onedata_mcp import tool_maturity as tm
from onedata_mcp.main import mcp


def _registered() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


# --- classification integrity ---------------------------------------------


def test_stable_contains_the_benchmark_headline() -> None:
    """Every tool the earlier benchmark evaluated is stable; STABLE may grow beyond it
    by the promotion protocol (design/10-tool-maturity.md)."""
    from benchmark.tool_allowlist import HEADLINE

    assert HEADLINE <= tm.STABLE


def test_all_known_covers_exactly_the_registered_surface() -> None:
    """Every registered tool is classified; nothing classified is unregistered."""
    registered = _registered()
    assert registered == tm.ALL_KNOWN, (
        f"unclassified (registered, not in ALL_KNOWN): {sorted(registered - tm.ALL_KNOWN)}; "
        f"stale (classified, not registered): {sorted(tm.ALL_KNOWN - registered)}"
    )


def test_stable_and_experimental_are_disjoint() -> None:
    assert not (tm.STABLE & tm.EXPERIMENTAL)


def test_upstream_is_subset_of_known() -> None:
    """UPSTREAM may name inherited tools that sit behind the preview flag (FLAG_GATED)."""
    assert tm.UPSTREAM <= tm.ALL_KNOWN | tm.FLAG_GATED


def test_maturity_and_origin_helpers() -> None:
    assert tm.maturity_of("list_user_spaces") == "stable"
    assert tm.maturity_of("schedule_file_replication") == "experimental"
    assert tm.maturity_of("totally_unknown_tool") == "experimental"  # safe default
    assert tm.origin_of("list_user_spaces") == "upstream"
    assert tm.origin_of("move_file") == "ours"
    # flag-gated tools keep their labels: the six moved off the default list are
    # inherited, so upstream-origin, and never validated, so experimental
    assert tm.maturity_of("get_file_id") == "experimental"
    assert tm.origin_of("get_file_id") == "upstream"
    assert tm.origin_of("totally_unknown_tool") == "ours"


def test_the_genuine_distrust_set_is_upstream_experimental() -> None:
    """The inherited-and-never-swept tools: 2 on the default list, 6 behind the flag."""
    assert {"get_file_attributes", "list_children"} == tm.EXPERIMENTAL & tm.UPSTREAM
    assert {
        "get_file_id",
        "get_harvester_index_schema",
        "grep_file_content",
        "list_marketplace_spaces",
        "list_user_harvesters",
        "query_harvester_index",
    } == tm.FLAG_GATED & tm.UPSTREAM


# --- selection logic -------------------------------------------------------


def test_no_env_keeps_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tm.MATURITY_ENV, raising=False)
    monkeypatch.delenv(tm.TOOLS_ENV, raising=False)
    assert tm.selected_tools() is None
    assert tm.tools_to_remove() == set()


def test_maturity_stable_keeps_only_the_16(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tm.TOOLS_ENV, raising=False)
    monkeypatch.setenv(tm.MATURITY_ENV, "stable")
    assert tm.selected_tools() == set(tm.STABLE)
    assert tm.tools_to_remove() == set(tm.EXPERIMENTAL)


def test_maturity_both_tiers_keeps_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tm.TOOLS_ENV, raising=False)
    monkeypatch.setenv(tm.MATURITY_ENV, "stable,experimental")
    assert tm.tools_to_remove() == set()


def test_unknown_tier_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tm.TOOLS_ENV, raising=False)
    monkeypatch.setenv(tm.MATURITY_ENV, "banana")
    # All-unknown tier keeps everything rather than silently hiding the surface.
    assert tm.selected_tools() is None
    assert tm.tools_to_remove() == set()


def test_explicit_tools_wins_over_maturity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tm.MATURITY_ENV, "stable")
    monkeypatch.setenv(tm.TOOLS_ENV, "list_user_spaces, schedule_file_replication")
    keep = tm.selected_tools()
    assert keep == {"list_user_spaces", "schedule_file_replication"}
    # everything else pruned, incl. other stable tools
    assert "download_file" in tm.tools_to_remove()


def test_explicit_unknown_names_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tm.MATURITY_ENV, raising=False)
    monkeypatch.setenv(tm.TOOLS_ENV, "list_user_spaces, nope_not_a_tool")
    assert tm.selected_tools() == {"list_user_spaces"}


# --- end-to-end against the live server ------------------------------------


def test_default_server_exposes_all_23() -> None:
    assert len(_registered()) == len(tm.ALL_KNOWN) == 23


def test_readiness_tools_are_experimental_and_ours() -> None:
    """The readiness tools are new: not panel-validated, not from upstream."""
    for name in ("check_readiness", "ensure_readiness"):
        assert name in tm.EXPERIMENTAL
        assert tm.maturity_of(name) == "experimental"
        assert tm.origin_of(name) == "ours"
        assert name not in tm.UPSTREAM
