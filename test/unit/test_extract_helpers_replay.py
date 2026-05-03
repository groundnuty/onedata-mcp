# ruff: noqa: E501  -- agent verbatim output is intentionally not wrapped
"""Tests for parser bug fixes pinned by VERBATIM agent output from
the K=8 sweep (run 20260503T002305_k8). False-negatives identified
mid-sweep that the existing oracle's `extract_int` / `extract_kv_lines`
couldn't handle. These tests use the actual agent answers as positive
cases — replay-as-test.

Bugs fixed (2026-05-03):
  1. extract_int case-sensitivity → 'Size: 57' didn't match key='size'
  2. extract_int strict separator class → 3-column markdown tables
     with intermediate hex spaceId broke the lookup
  3. extract_kv_lines couldn't parse JSON-shape answers
     (Qwen D5 emitted 'json: {"pipeline_stage": "raw", ...}')

These are the 3 patterns identified during the K=8 sweep that flipped
~5-7 trial outcomes from FAIL to PASS once corrected. See
`benchmark/rescore.py` for the sidecar rescore tool that re-evaluates
saved K=8 trials with the corrected parsers.
"""

from __future__ import annotations

from benchmark.oracles._helpers import extract_int, extract_kv_lines


# ---------------------------------------------------------------------------
# extract_int — case-insensitive (bug 1)
# ---------------------------------------------------------------------------


def test_extract_int_capitalised_size() -> None:
    """GLM D3 K=8 trial: agent emitted '**Size**: 57 bytes'.
    Pre-fix: case-sensitive lookup for 'size' missed 'Size'."""
    assert extract_int("**Size**: 57 bytes", "size") == 57


def test_extract_int_all_caps_key() -> None:
    """Some agents shout: 'SIZE = 57'. Should match key='size'."""
    assert extract_int("SIZE = 57", "size") == 57


def test_extract_int_mixed_case_kept() -> None:
    """Lookup with the canonical case still works."""
    assert extract_int("size: 57", "size") == 57
    assert extract_int("Size: 57", "Size") == 57


# ---------------------------------------------------------------------------
# extract_int — 3-column markdown tables (bug 2)
# ---------------------------------------------------------------------------


def test_extract_int_3_column_table_with_spaceid() -> None:
    """GLM D1 K=8 trial: agent added a spaceId column between name
    and provider_count.

    Pre-fix: separator class [=:|`-*\\s] didn't allow the hex
    chars between key and value. Now the parser tolerates arbitrary
    intermediate non-digit content within a single line.
    """
    text = "| CloudSKTest | ed529587d78a4b5493e85d430cb308cfch8c99 | 3 |"
    assert extract_int(text, "CloudSKTest") == 3


def test_extract_int_3_column_table_multiple_rows() -> None:
    """Same shape, multiple rows — each row should resolve to its own
    count, not bleed across rows."""
    text = """| CloudSKTest | ed529587d78a4b5493e85d430cb308cfch8c99 | 3 |
| CzeslawsSpace | e3ac22681862f55a809492fcff46e72bch3bca | 1 |
| ProductionSpace | bf2994a889b94e6720415b758e478fc6chd084 | 2 |"""
    assert extract_int(text, "CloudSKTest") == 3
    assert extract_int(text, "CzeslawsSpace") == 1
    assert extract_int(text, "ProductionSpace") == 2


def test_extract_int_does_not_cross_lines() -> None:
    """Critical: a key on line 1 must NOT match a number on line 2."""
    text = "name_without_value\n| OtherName | 42 |"
    assert extract_int(text, "name_without_value") is None


def test_extract_int_bold_formatting_around_value() -> None:
    """Markdown bold around the number doesn't block the match."""
    assert extract_int("Size: **57** bytes", "size") == 57
    assert extract_int("Size: `57` bytes", "size") == 57


# ---------------------------------------------------------------------------
# extract_kv_lines — JSON-format (bug 3)
# ---------------------------------------------------------------------------


def test_extract_kv_lines_inline_json_qwen_d5() -> None:
    """Qwen D5 K=8 trial: agent emitted metadata as inline JSON.

    Pre-fix: extract_kv_lines only handled 'k: v' lines. The json
    shape `json: {"pipeline_stage": "raw", ...}` matched on 'json'
    as the key, getting the entire string as value.

    Post-fix: parser detects '{...}' and json-loads it, expanding
    the contained k/v pairs.
    """
    text = 'json: {"pipeline_stage": "raw", "owner": "agent", "created": "2026-05-01"}'
    out = extract_kv_lines(text)
    assert out["pipeline_stage"] == "raw"
    assert out["owner"] == "agent"
    assert out["created"] == "2026-05-01"


def test_extract_kv_lines_bare_json_object() -> None:
    """Same fix — a bare '{...}' line without prefix also works."""
    text = '{"pipeline_stage": "raw", "rows": "10"}'
    out = extract_kv_lines(text)
    assert out["pipeline_stage"] == "raw"
    assert out["rows"] == "10"


def test_extract_kv_lines_kv_form_unchanged() -> None:
    """Existing 'key: value' parsing still works."""
    text = "pipeline_stage: raw\nowner: agent"
    out = extract_kv_lines(text)
    assert out["pipeline_stage"] == "raw"
    assert out["owner"] == "agent"


def test_extract_kv_lines_kv_with_bullet_unchanged() -> None:
    """Bullet-prefixed kv lines still parsed."""
    text = "- pipeline_stage: raw\n* owner: agent"
    out = extract_kv_lines(text)
    assert out["pipeline_stage"] == "raw"
    assert out["owner"] == "agent"


def test_extract_kv_lines_malformed_json_falls_back() -> None:
    """If a line LOOKS like json but isn't valid, fall through to
    the kv-line parser. e.g. 'tag: {malformed' should not crash;
    'tag' will get the value '{malformed' under the kv-form rule."""
    text = "tag: {malformed json without closing brace"
    out = extract_kv_lines(text)
    assert out.get("tag") == "{malformed json without closing brace"


def test_extract_kv_lines_mixed_lines() -> None:
    """A doc with both JSON and kv lines should merge."""
    text = """name: hello
json: {"pipeline_stage": "raw"}
owner: bob"""
    out = extract_kv_lines(text)
    assert out["name"] == "hello"
    assert out["pipeline_stage"] == "raw"
    assert out["owner"] == "bob"
