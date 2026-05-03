"""Tests pinning D3's head-substring encoding tolerance.

Background: D3 prompts the agent to read /<space>/d3/manifest.txt and
report 'size=N; head=<chars>'. Agents typically render the file's
newlines as the 2-char escape sequence '\\n' (backslash + n) rather
than emitting real LF bytes. They then truncate the rendered head to
~N visible chars. With a 50-char expected_head, every newline shifts
the agent's emission boundary by 1 char — at 2 newlines in the first
50 chars of D3, the agent's normalized answer can fall 2 chars short
of the expected head boundary even though the agent read the file
correctly. Witnessed 2026-05-02 in run 20260502T190757 (Sonnet:
final_answer ended with 'ppam_2026_m' but expected ended with 'ppam_2026_mcp').

Fix: oracle uses first 30 source chars (covers 2 complete lines of
the manifest), well below the truncation slop window. This file
pins the fix so a future regression to a longer expected_head
(re-introducing the encoding-edge sensitivity) is caught.
"""

from __future__ import annotations

import pytest

from benchmark._runtime_types import AgentTrace, RunContext, ToolCall
from benchmark.oracles.discovery import verify_d3
from benchmark.scenarios import D3 as D3_SCENARIO


def _ctx(space_name: str = "ppam_2026_mcp_tests") -> RunContext:
    manifest_path = f"/{space_name}/d3/manifest.txt"
    return RunContext(
        scenario_id="D3",
        fixture_paths={manifest_path: "manifest-file-id"},
        space_name=space_name,
    )


def _trace(final_answer: str) -> AgentTrace:
    return AgentTrace(
        final_answer=final_answer,
        tool_calls=(
            ToolCall(
                tool_name="download_file",
                arguments={"file_id_or_path": "/ppam_2026_mcp_tests/d3/manifest.txt"},
                succeeded=True,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_d3_passes_with_real_lf_in_answer() -> None:
    """Baseline: agent emits the head with real LF bytes (as the file
    contains). Should pass."""
    expected_size = len(D3_SCENARIO.fixture.files[0].content.encode("utf-8"))
    answer = (
        f"size={expected_size}; head=MANIFEST v1\nbuild=46-g14b5bda7\nspace=ppam_2026_mcp_tests"
    )
    result = await verify_d3(_ctx(), _trace(answer))
    assert result.mcp_pass is True


@pytest.mark.asyncio
async def test_d3_passes_with_escape_rendered_newlines_truncated() -> None:
    """The 2026-05-02 Sonnet failure mode replayed verbatim. Agent
    rendered LF as the 2-char '\\n' escape and truncated to ~50 visible
    chars. With the 30-char expected_head this passes cleanly."""
    expected_size = len(D3_SCENARIO.fixture.files[0].content.encode("utf-8"))
    # Verbatim Sonnet final_answer from run 20260502T190757:
    answer = f"size={expected_size}; head=MANIFEST v1\\nbuild=46-g14b5bda7\\nspace=ppam_2026_m"
    result = await verify_d3(_ctx(), _trace(answer))
    assert result.mcp_pass is True


@pytest.mark.asyncio
async def test_d3_fails_when_size_wrong() -> None:
    """Loosening must not paper over real fails. Wrong byte size still
    fails the size axis even if head looks right."""
    answer = "size=99; head=MANIFEST v1\nbuild=46-g14b5bda7"
    result = await verify_d3(_ctx(), _trace(answer))
    assert result.mcp_pass is False
    assert "size mismatch" in result.diagnosis


@pytest.mark.asyncio
async def test_d3_fails_when_head_completely_wrong() -> None:
    """Loosening must not paper over real fails. Head from a different
    file fails the substring check."""
    expected_size = len(D3_SCENARIO.fixture.files[0].content.encode("utf-8"))
    answer = f"size={expected_size}; head=Random unrelated text content"
    result = await verify_d3(_ctx(), _trace(answer))
    assert result.mcp_pass is False
    assert "head substring not present" in result.diagnosis
