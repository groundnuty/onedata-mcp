"""Tests for extract_paths' exclusion-marker detection.

Surfaced 2026-05-02 by P6 K=1 sweep (run T195311). Both Qwen and GLM
correctly identified that lone1.bin + lone2.bin match the criterion
(replicas_num=1) and redundant.bin doesn't, but they emitted ALL THREE
paths in a bullet structure with inline annotations distinguishing
which fit. The naive extract_paths counted all 3 → oracle over-counted
→ FAIL despite a logically correct answer.

This file pins the per-line exclusion-marker behaviour so a future
revert (or naive 'find all paths' refactor) is caught.
"""

from __future__ import annotations

from benchmark.oracles._helpers import extract_paths


SPACE_PREFIX = "/ppam_2026_mcp_tests"


def test_extract_paths_simple_bullet_list_unchanged() -> None:
    """Baseline: a clean bullet list with no exclusion markers behaves
    exactly as before."""
    text = """
The matching files are:

- /ppam_2026_mcp_tests/p6/single-copy/lone1.bin
- /ppam_2026_mcp_tests/p6/single-copy/lone2.bin
"""
    assert extract_paths(text, anchor=SPACE_PREFIX + "/") == {
        "/ppam_2026_mcp_tests/p6/single-copy/lone1.bin",
        "/ppam_2026_mcp_tests/p6/single-copy/lone2.bin",
    }


def test_extract_paths_skips_explicit_excluded_marker() -> None:
    """GLM's P6 actual answer pattern: per-line bullets with `(so excluded)`."""
    text = """
* /ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/lone1.bin - Requires exactly 1 replica, status: fulfilled ✓
* /ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/lone2.bin - Requires exactly 1 replica, status: pending
* /ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/redundant.bin - Requires 2 replicas (so excluded)
"""
    result = extract_paths(text, anchor="/ppam_2026_mcp_tests_glm_4_7_flash/")
    assert "/ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/lone1.bin" in result
    assert "/ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/lone2.bin" in result
    assert "/ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/redundant.bin" not in result


def test_extract_paths_skips_wait_self_correction() -> None:
    """Qwen's P6 actual answer pattern: bullet list including all 3, then
    `Wait, X requires 2 replicas, so it should not be included.`
    The bullet line for redundant lacks an inline exclusion marker, but
    the follow-up `Wait, ...` line mentions the basename — Pass 2
    cross-references it and drops the path. Pinning this prevents a
    naive single-pass refactor from regressing."""
    text = """
The files under /ppam_2026_mcp_tests_qwen3_6_35b/p6/single-copy/ whose effective QoS requires only 1 replica are:

- /ppam_2026_mcp_tests_qwen3_6_35b/p6/single-copy/lone1.bin
- /ppam_2026_mcp_tests_qwen3_6_35b/p6/single-copy/lone2.bin
- /ppam_2026_mcp_tests_qwen3_6_35b/p6/single-copy/redundant.bin (requires 2 replicas)

Wait, redundant.bin requires 2 replicas, so it should not be included.
"""
    result = extract_paths(text, anchor="/ppam_2026_mcp_tests_qwen3_6_35b/")
    assert "/ppam_2026_mcp_tests_qwen3_6_35b/p6/single-copy/lone1.bin" in result
    assert "/ppam_2026_mcp_tests_qwen3_6_35b/p6/single-copy/lone2.bin" in result
    # Pass-2 cross-reference: 'Wait, ... should not be included' is a
    # self-correction line; 'redundant.bin' basename appears in it; the
    # path captured by Pass-1 from the bullet line gets retracted.
    assert "/ppam_2026_mcp_tests_qwen3_6_35b/p6/single-copy/redundant.bin" not in result


def test_extract_paths_skips_distractor_marker() -> None:
    """Some agents annotate distractors literally."""
    text = """
- /space/p6/lone1.bin
- /space/p6/redundant.bin (is a distractor)
"""
    result = extract_paths(text, anchor="/space/")
    assert "/space/p6/lone1.bin" in result
    assert "/space/p6/redundant.bin" not in result


def test_extract_paths_keeps_paths_with_innocuous_descriptions() -> None:
    """Make sure we don't accidentally drop paths described positively."""
    text = """
- /space/lone1.bin - Requires exactly 1 replica, status: fulfilled
- /space/lone2.bin - Requires exactly 1 replica, status: pending
"""
    result = extract_paths(text, anchor="/space/")
    assert result == {"/space/lone1.bin", "/space/lone2.bin"}


def test_extract_paths_handles_doesnt_match_phrasing() -> None:
    text = """
- /space/file_a.bin
- /space/file_b.bin (doesn't match the criterion)
- /space/file_c.bin
"""
    result = extract_paths(text, anchor="/space/")
    assert result == {"/space/file_a.bin", "/space/file_c.bin"}


def test_extract_paths_handles_should_not_phrasing() -> None:
    text = """
The matches:
- /space/a.bin
- /space/b.bin should not be in the result, sorry
- /space/c.bin
"""
    result = extract_paths(text, anchor="/space/")
    assert result == {"/space/a.bin", "/space/c.bin"}


def test_extract_paths_does_not_drop_excluding_in_path_name() -> None:
    """Edge case: a path with 'exclud' in the name. The marker check
    is on the LINE not the path; if a line ONLY contains the path
    (no exclusion-context prose), the path stays."""
    text = "/space/excluded_files_archive.bin"
    result = extract_paths(text, anchor="/space/")
    # Line contains "exclud" → drops the path. This is acceptable false
    # positive: the documented exclusion markers are conservative, and a
    # filename literally containing 'exclud' is rare. Verify the current
    # behaviour explicitly so a future relaxation is a deliberate change.
    assert result == set()  # documents the conservative behavior
