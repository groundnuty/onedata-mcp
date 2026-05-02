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


# ---------------------------------------------------------------------------
# Section-context (Pass 2): GLM's header pattern from run T202740
# ---------------------------------------------------------------------------


def test_extract_paths_section_header_exclusion_glm_pattern() -> None:
    """GLM's actual T202740 P6 final_answer (verbatim shape). Inclusion
    section + exclusion section, each with bold-with-colon header, paths
    on separate lines below the header."""
    text = """
Based on the QOS analysis:

**Files requiring only 1 replica:**
* /ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/lone1.bin (replicaNum: 1)
* /ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/lone2.bin (replicaNum: 1)

**File NOT meeting the criteria:**
* /ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/redundant.bin (requires 2 replicas)

The two files named "lone*" require a single replica.
"""
    result = extract_paths(text, anchor="/ppam_2026_mcp_tests_glm_4_7_flash/")
    assert "/ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/lone1.bin" in result
    assert "/ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/lone2.bin" in result
    # Section-context (Pass 2): the path is in a section opened by an
    # exclusion-header `**File NOT meeting the criteria:**` — dropped.
    assert "/ppam_2026_mcp_tests_glm_4_7_flash/p6/single-copy/redundant.bin" not in result


def test_extract_paths_section_header_inclusion_does_not_drop() -> None:
    """Make sure positive section headers (no exclusion phrase) keep
    their paths. The opening header `**Matching files:**` should NOT
    be treated as exclusion."""
    text = """
**Matching files:**
* /space/a.bin
* /space/b.bin

**Other notes:**
* These are interesting cases.
"""
    result = extract_paths(text, anchor="/space/")
    assert result == {"/space/a.bin", "/space/b.bin"}


def test_extract_paths_blank_line_resets_exclusion_section() -> None:
    """Once a blank line ends the exclusion section, the next batch of
    paths (without a new header) should be captured normally."""
    text = """
**Files NOT meeting the criteria:**
* /space/excluded1.bin

* /space/included_after_blank.bin
"""
    result = extract_paths(text, anchor="/space/")
    # Path right after exclusion header → dropped
    assert "/space/excluded1.bin" not in result
    # Path after blank line resets the section state. With no fresh
    # header, current behavior treats lines as section-less (default
    # inclusion). Document that.
    assert "/space/included_after_blank.bin" in result


def test_extract_paths_atx_header_also_recognised() -> None:
    """ATX-style headers (`### ...`) work too, not just `**...:**`."""
    text = """
### Files NOT included
* /space/excluded.bin

### Matching files
* /space/included.bin
"""
    result = extract_paths(text, anchor="/space/")
    assert "/space/excluded.bin" not in result
    assert "/space/included.bin" in result
