"""Tests for extract_int's tolerance of parenthetical annotations between
key and value.

Surfaced 2026-05-03 in Granite K=1 v2 run T232042 D1: agent
thoughtfully labelled two federation-side `StefansSpace` entries as
`StefansSpace (first)` and `StefansSpace (duplicate)` to disambiguate.
The pre-fix extract_int separator class didn't include `(`, so the
annotation broke the lookup, FAILing what was actually a more careful
answer than other panel models produce. This test pins the tolerance.
"""

from __future__ import annotations

from benchmark.oracles._helpers import extract_int


def test_extract_int_basic_table_unchanged() -> None:
    """Baseline: simple `| name | N |` markdown row — must still work."""
    assert extract_int("| StefansSpace | 2 |", "StefansSpace") == 2


def test_extract_int_disambig_annotation_first() -> None:
    """Granite's actual T232042 D1 output: `(first)` between name and pipe."""
    assert extract_int("| StefansSpace (first) | 2 |", "StefansSpace") == 2


def test_extract_int_disambig_annotation_duplicate() -> None:
    """Same agent, second-row annotation."""
    assert extract_int("| StefansSpace (duplicate) | 2 |", "StefansSpace") == 2


def test_extract_int_disambig_annotation_other_space_name() -> None:
    """Granite v2 also produced `StefansSpace (OliversSpace) | 2 |`
    — annotation is itself a name."""
    assert extract_int("| StefansSpace (OliversSpace) | 2 |", "StefansSpace") == 2


def test_extract_int_disambig_test_data_first() -> None:
    """Same shape on TestData — federation has two."""
    assert extract_int("| TestData (first) | 0 |", "TestData") == 0
    assert extract_int("| TestData (second) | 0 |", "TestData") == 0


def test_extract_int_kv_equals_unchanged() -> None:
    """Non-table forms must still work (D3 size, A1 tagged)."""
    assert extract_int("size=57", "size") == 57
    assert extract_int("tagged=5", "tagged") == 5


def test_extract_int_kv_colon_unchanged() -> None:
    assert extract_int("count: 5", "count") == 5


def test_extract_int_backticked_key_unchanged() -> None:
    """Edge from D1 oracle's own fallback (backticked retry)."""
    assert extract_int("`Cloud-SK`: 3", "`Cloud-SK`") == 3


def test_extract_int_long_alphanum_token_skipped() -> None:
    """Existing safety: number followed by an alphanumeric word char
    doesn't match. The negative-lookahead `(?!\\w)` blocks `5abc`
    matching for key='X'."""
    # 5 followed by `abc` (alphanum) — blocked.
    assert extract_int("X=5abc", "X") is None
    # 5 followed by `_` (underscore — \w) — blocked.
    assert extract_int("X=5_", "X") is None
    # 5 followed by `-` (NOT \w — note: this DOES match. Documents the
    # current behavior. Hyphens, parens, dots and pipes terminate the
    # match correctly.)
    assert extract_int("X=5-something", "X") == 5


def test_extract_int_annotation_does_not_consume_value() -> None:
    """Sanity: number INSIDE the annotation doesn't get returned. e.g.
    `name (3 things)` should NOT return 3 if there's a real value after."""
    assert extract_int("StefansSpace (3 things) | 2 |", "StefansSpace") == 2


def test_extract_int_no_match_returns_none() -> None:
    assert extract_int("nothing here", "missing") is None


def test_extract_int_zero_value() -> None:
    """Earlier bug: `or` short-circuit on falsy 0. Ensure 0 is captured."""
    assert extract_int("| TestData | 0 |", "TestData") == 0
