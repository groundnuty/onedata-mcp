"""Tests for the per-tool maturity classification + launch-time tool selection."""

from __future__ import annotations

import asyncio

import pytest

from onedata_mcp import tool_maturity as tm
from onedata_mcp.main import mcp


def _registered() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


# --- classification integrity ---------------------------------------------


def test_stable_matches_benchmark_headline() -> None:
    """The 'stable' set must stay identical to the benchmark HEADLINE-16."""
    from benchmark.tool_allowlist import HEADLINE

    assert tm.STABLE == HEADLINE


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
    assert tm.UPSTREAM <= tm.ALL_KNOWN


def test_maturity_and_origin_helpers() -> None:
    assert tm.maturity_of("list_user_spaces") == "stable"
    assert tm.maturity_of("schedule_file_replication") == "experimental"
    assert tm.maturity_of("totally_unknown_tool") == "experimental"  # safe default
    assert tm.origin_of("list_user_spaces") == "upstream"
    assert tm.origin_of("move_file") == "ours"
    assert tm.origin_of("totally_unknown_tool") == "ours"


def test_the_genuine_distrust_set_is_upstream_experimental() -> None:
    """Sanity-check the 8 inherited-and-never-swept tools."""
    distrust = tm.EXPERIMENTAL & tm.UPSTREAM
    assert distrust == {
        "get_file_attributes",
        "get_file_id",
        "get_harvester_index_schema",
        "grep_file_content",
        "list_children",
        "list_marketplace_spaces",
        "list_user_harvesters",
        "query_harvester_index",
    }


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


def test_default_server_exposes_all_27() -> None:
    assert len(_registered()) == len(tm.ALL_KNOWN) == 27
